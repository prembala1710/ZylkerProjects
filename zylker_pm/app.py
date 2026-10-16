"""WSGI application: routing, auth, per-request transactions, security headers, static files."""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from . import auth, notify
from .config import Config, get_tz
from .context import Context
from .db import connect, transaction
from .domain import ROLE_RANK
from .errors import (
    ApiError,
    BadRequest,
    Forbidden,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    UnsupportedMediaType,
)

log = logging.getLogger("zylker.access")

STATIC_DIR = Path(__file__).parent / "static"
# Only these paths are ever served from disk - there is no path-based file lookup.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    ),
]

STATUS_TEXT = {
    200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
    415: "Unsupported Media Type", 422: "Unprocessable Entity", 500: "Internal Server Error",
}  # fmt: skip


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _loggable(text, limit=200):
    """PATH_INFO arrives percent-decoded, so %0A would otherwise forge extra log lines."""
    return _CONTROL.sub("?", str(text))[:limit]


class Raw:
    """A non-JSON response body (CSV export)."""

    def __init__(self, body: bytes, content_type: str, status: int = 200, headers=()):
        self.body, self.content_type, self.status, self.headers = body, content_type, status, list(headers)


class Request:
    def __init__(self, environ, max_body):
        self.method = environ["REQUEST_METHOD"].upper()
        self.path = environ.get("PATH_INFO") or "/"
        self.query = dict(parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True))
        self.content_type = (environ.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
        self.authorization = environ.get("HTTP_AUTHORIZATION")
        self._environ = environ
        self._limit = max_body
        self._body = None
        try:
            self.length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            raise BadRequest("Invalid Content-Length") from None

    def check_size(self, limit):
        self._limit = limit
        if self.length > limit:
            raise PayloadTooLarge("Request body exceeds %d bytes" % limit)

    def body(self) -> bytes:
        if self._body is None:
            self._body = self._environ["wsgi.input"].read(self.length) if self.length else b""
        return self._body

    def json(self):
        if self.content_type != "application/json":
            raise UnsupportedMediaType("Content-Type must be application/json")
        raw = self.body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("Malformed JSON body") from None

    def text(self) -> str:
        if self.content_type not in ("text/csv", "text/plain"):
            raise UnsupportedMediaType("Content-Type must be text/csv")
        try:
            return self.body().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise BadRequest("Body must be UTF-8 text") from None


class Route:
    def __init__(self, method, pattern, handler, role, public, body_limit):
        self.method, self.pattern, self.handler = method, pattern, handler
        self.role, self.public, self.body_limit = role, public, body_limit
        parts = re.split(r"(\{\w+\})", pattern)
        self.regex = re.compile(
            "^" + "".join("(?P<%s>[^/]+)" % p[1:-1] if p.startswith("{") else re.escape(p) for p in parts) + "$"
        )


ROUTES: list[Route] = []


def route(method, pattern, role="viewer", public=False, body_limit=None):
    """Register a handler ``fn(ctx, req, **path_params)`` with a minimum role."""

    def decorator(fn):
        ROUTES.append(Route(method, pattern, fn, role, public, body_limit))
        return fn

    return decorator


class App:
    def __init__(self, config: Config, clock=None, notifier=None):
        from . import routes  # noqa: F401 - registers handlers

        config.validate()
        self.config = config
        self.tz = get_tz(config.timezone)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.notifier = notifier if notifier is not None else notify.build_notifier(config)

    # -- helpers shared by the CLI and tests -------------------------------------------

    @contextmanager
    def session(self, user=None, write=True):
        """Yield a Context inside a transaction; notifications are sent after commit."""
        conn = connect(self.config.db_path)
        try:
            with transaction(conn, write):
                ctx = Context(conn, user, self.clock(), self.tz, self.config)
                yield ctx
            notify.dispatch(self.notifier, ctx.events, self.config.public_url)
        finally:
            conn.close()

    # -- WSGI --------------------------------------------------------------------------

    def __call__(self, environ, start_response):
        started = time.monotonic()
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        try:
            status, headers, body = self.dispatch(Request(environ, self.config.max_body_bytes))
        except ApiError as exc:
            status, headers, body = self.json_response(exc.status, exc.to_dict(), list(exc.headers.items()))
        except Exception:  # noqa: BLE001
            log.exception("unhandled error on %s %s", _loggable(method), _loggable(path))
            status, headers, body = self.json_response(
                500, {"error": {"code": "internal_error", "message": "Internal server error"}}
            )
        if method == "HEAD":
            body = b""
        headers = headers + SECURITY_HEADERS + [("Content-Length", str(len(body)))]
        log.info("%s %s -> %d (%.0f ms)", _loggable(method), _loggable(path), status, (time.monotonic() - started) * 1000)
        start_response("%d %s" % (status, STATUS_TEXT.get(status, "Status")), headers)
        return [body]

    @staticmethod
    def json_response(status, payload, extra_headers=()):
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")]
        return status, headers + list(extra_headers), body

    def static(self, path):
        filename, content_type = STATIC_FILES[path]
        body = (STATIC_DIR / filename).read_bytes()
        return 200, [("Content-Type", content_type), ("Cache-Control", "no-cache")], body

    def dispatch(self, req: Request):
        if req.method in ("GET", "HEAD") and req.path in STATIC_FILES:
            return self.static(req.path)
        if req.path == "/favicon.ico":
            return 204, [("Cache-Control", "max-age=86400")], b""
        matches = [(r, r.regex.match(req.path)) for r in ROUTES]
        matches = [(r, m) for r, m in matches if m]
        wanted = "GET" if req.method == "HEAD" else req.method
        for r, m in matches:
            if r.method == wanted:
                return self.run(r, m.groupdict(), req)
        if matches:
            allow = ", ".join(sorted({r.method for r, _ in matches}))
            raise MethodNotAllowed("Method %s not allowed here" % req.method, headers={"Allow": allow})
        raise NotFound("No such endpoint: %s" % req.path, code="endpoint_not_found")

    def run(self, r: Route, params, req: Request):
        limit = getattr(self.config, r.body_limit) if r.body_limit else self.config.max_body_bytes
        req.check_size(limit)
        conn = connect(self.config.db_path)
        try:
            user = None
            if not r.public:
                user = auth.authenticate(conn, req.authorization)
                if ROLE_RANK[user["role"]] < ROLE_RANK[r.role]:
                    raise Forbidden(
                        "This action requires the %r role or higher (you are %r)" % (r.role, user["role"]),
                        code="insufficient_role",
                    )
            with transaction(conn, write=req.method not in ("GET", "HEAD")):
                ctx = Context(conn, user, self.clock(), self.tz, self.config)
                result = r.handler(ctx, req, **params)
            notify.dispatch(self.notifier, ctx.events, self.config.public_url)
        finally:
            conn.close()
        if isinstance(result, Raw):
            headers = [("Content-Type", result.content_type), ("Cache-Control", "no-store")] + result.headers
            return result.status, headers, result.body
        if isinstance(result, tuple):
            return self.json_response(result[0], result[1])
        return self.json_response(200, result)
