"""WSGI application: routing, auth, per-request transactions, security headers, static files."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
import uuid
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
    TooManyRequests,
    Unauthorized,
    UnsupportedMediaType,
)
from .services import audit
from .throttle import Throttle
from .util import iso

log = logging.getLogger("zylker.access")

SESSION_GAP_SECONDS = 1800  # a user's next request after this much quiet is recorded as a new session
MAX_TRACKED_USERS = 10_000

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
    415: "Unsupported Media Type", 422: "Unprocessable Entity", 429: "Too Many Requests", 500: "Internal Server Error",
}  # fmt: skip


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _loggable(text, limit=200):
    """PATH_INFO arrives percent-decoded, so %0A would otherwise forge extra log lines."""
    return _CONTROL.sub("?", str(text))[:limit]


class Raw:
    """A non-JSON response body (CSV export)."""

    def __init__(self, body: bytes, content_type: str, status: int = 200, headers=()):
        self.body, self.content_type, self.status, self.headers = body, content_type, status, list(headers)


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def client_ip(environ, hops):
    """The address to record for a request.

    ``REMOTE_ADDR`` cannot be forged but behind a reverse proxy it is always the proxy. With ``hops`` trusted
    proxies in front, the client is the ``hops``-th entry from the right of ``X-Forwarded-For`` (each proxy
    appends the address it saw). Anything that is not a valid IP address is dropped, which also keeps junk
    out of the log.
    """
    if hops > 0:
        parts = [p.strip() for p in (environ.get("HTTP_X_FORWARDED_FOR") or "").split(",") if p.strip()]
        if len(parts) >= hops:
            try:
                return str(ipaddress.ip_address(parts[-hops]))
            except ValueError:
                pass
    try:
        return str(ipaddress.ip_address(environ.get("REMOTE_ADDR") or ""))
    except ValueError:
        return None


class RequestInfo:
    """What the audit log may know about one HTTP request: who asked, from where, for what. Never the token or body."""

    def __init__(self, environ, hops=0):
        offered = environ.get("HTTP_X_REQUEST_ID") or ""
        self.id = offered if _REQUEST_ID_RE.match(offered) else uuid.uuid4().hex[:16]
        self.ip = client_ip(environ, hops)
        self.method = environ.get("REQUEST_METHOD", "GET")
        self.path = environ.get("PATH_INFO", "/")
        self.user_agent = audit.clean(environ.get("HTTP_USER_AGENT"), 200)
        self.had_token = auth.bearer_token(environ.get("HTTP_AUTHORIZATION")) is not None
        self.user = None  # set once the token has been verified
        self.rate = None  # (limit, remaining) once the user's request budget has been charged
        self.route = None  # the route pattern, e.g. /api/tasks/{key}: bounded, unlike the raw path


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
        self.throttle = Throttle(config.auth_failure_limit, config.rate_limit_per_minute)
        self._seen = {}  # user id -> when we last recorded that user's session; keeps the hot path free of DB writes
        self._seen_lock = threading.Lock()

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

    # -- audit -------------------------------------------------------------------------
    # Auditing must never break or slow the request it describes: every write below happens after the request's
    # own transaction has finished (committed or rolled back), on its own short connection, and any failure is
    # logged and swallowed.

    def _audit_write(self, action, info, *, actor_id=None, details=None, merge_on=()):
        try:
            conn = connect(self.config.db_path)
            try:
                with transaction(conn, write=True):
                    audit.record(
                        conn, iso(self.clock()), action, actor_id=actor_id, ip=info.ip, request_id=info.id,
                        details=details, merge_on=merge_on,
                    )  # fmt: skip
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            log.warning("could not write audit event %s [%s]", action, info.id, exc_info=True)

    def _audit_refusal(self, info, exc):
        """401 (bad or missing token) and 403 (not allowed) are the events an administrator watches for."""
        if exc.status == 401:
            self._audit_write(
                "auth.failed", info,
                details={"reason": "invalid_token" if info.had_token else "missing_token", "method": info.method, "path": audit.clean(info.path), "user_agent": info.user_agent},
                merge_on=("reason",),
            )  # fmt: skip
        elif exc.status == 429:
            self._audit_throttle(info, exc)
        elif exc.status == 403:
            self._audit_write(
                "auth.denied", info, actor_id=info.user["id"] if info.user is not None else None,
                details={"code": exc.code, "method": info.method, "route": info.route or audit.clean(info.path), "message": audit.clean(exc.message)},
                merge_on=("code", "route"),
            )  # fmt: skip

    def _audit_throttle(self, info, exc):
        self._audit_write(
            "auth.throttled", info, actor_id=info.user["id"] if info.user is not None else None,
            details={
                "scope": exc.details["scope"], "reason": "still_blocked" if exc.code == "too_many_failures" else "rate_limited",
                "method": info.method, "route": info.route or audit.clean(info.path),
            },
            merge_on=("scope", "reason"),
        )  # fmt: skip

    def _audit_crash(self, info, exc):
        self._audit_write(
            "system.error", info, actor_id=info.user["id"] if info.user is not None else None,
            details={"route": info.route or audit.clean(info.path), "method": info.method, "error": type(exc).__name__},
            merge_on=("route", "error"),
        )  # fmt: skip

    def _audit_success(self, info, user, ctx):
        """Events a successful request queued itself, plus the start of a session for the signed-in user."""
        for action, details in ctx.audit_events:
            self._audit_write(action, info, actor_id=user["id"] if user is not None else None, details=details)
        if user is None:
            return
        now = self.clock()
        with self._seen_lock:
            last = self._seen.get(user["id"])
            if last is not None and (now - last).total_seconds() < SESSION_GAP_SECONDS:
                return
            if len(self._seen) >= MAX_TRACKED_USERS:
                self._seen.clear()
            self._seen[user["id"]] = now
        self._audit_write("auth.session_started", info, actor_id=user["id"], details={"user_agent": info.user_agent})

    # -- throttling --------------------------------------------------------------------

    def _on_auth_failure(self, info):
        """Called when a request fails to authenticate. Raises 429 while the address is blocked; the failure that
        starts a block is still answered with the honest 401 (and gets its own audit event)."""
        verdict = self.throttle.on_failure(info.ip, self.clock().timestamp())
        if verdict.outcome == "blocked":
            wait = verdict.retry_after
            raise TooManyRequests(
                "Too many failed sign-in attempts from this address. Try again in %d seconds." % wait,
                code="too_many_failures", details={"scope": "ip", "retry_after": wait}, headers={"Retry-After": str(wait)},
            )  # fmt: skip
        if verdict.outcome == "blocked_now":
            self._audit_write(
                "auth.throttled", info,
                details={"scope": "ip", "reason": "blocked", "seconds": verdict.retry_after, "strikes": verdict.strikes, "failures": verdict.failures},
            )  # fmt: skip

    def _charge_request(self, info, user):
        allowed, wait, limit, remaining = self.throttle.take(user["id"], self.clock().timestamp())
        if not limit:
            return
        info.rate = (limit, remaining)
        if not allowed:
            raise TooManyRequests(
                "Request limit reached (%d per minute). Try again in %d seconds." % (limit, wait),
                code="rate_limited", details={"scope": "user", "retry_after": wait}, headers={"Retry-After": str(wait)},
            )  # fmt: skip

    # -- WSGI --------------------------------------------------------------------------

    def __call__(self, environ, start_response):
        started = time.monotonic()
        info = RequestInfo(environ, self.config.proxy_hops)
        method, path = info.method, info.path
        try:
            status, headers, body = self.dispatch(Request(environ, self.config.max_body_bytes), info)
        except ApiError as exc:
            self._audit_refusal(info, exc)
            status, headers, body = self.json_response(exc.status, exc.to_dict(), list(exc.headers.items()))
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error on %s %s [%s]", _loggable(method), _loggable(path), info.id)
            self._audit_crash(info, exc)
            status, headers, body = self.json_response(
                500, {"error": {"code": "internal_error", "message": "Internal server error", "request_id": info.id}}
            )
        if method == "HEAD":
            body = b""
        if info.rate is not None:
            headers = headers + [("X-RateLimit-Limit", str(info.rate[0])), ("X-RateLimit-Remaining", str(info.rate[1]))]
        headers = headers + SECURITY_HEADERS + [("X-Request-Id", info.id), ("Content-Length", str(len(body)))]
        log.info("%s %s -> %d (%.0f ms) [%s]", _loggable(method), _loggable(path), status, (time.monotonic() - started) * 1000, info.id)
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

    def dispatch(self, req: Request, info: RequestInfo):
        if req.method in ("GET", "HEAD") and req.path in STATIC_FILES:
            return self.static(req.path)
        if req.path == "/favicon.ico":
            return 204, [("Cache-Control", "max-age=86400")], b""
        matches = [(r, r.regex.match(req.path)) for r in ROUTES]
        matches = [(r, m) for r, m in matches if m]
        wanted = "GET" if req.method == "HEAD" else req.method
        for r, m in matches:
            if r.method == wanted:
                return self.run(r, m.groupdict(), req, info)
        if matches:
            allow = ", ".join(sorted({r.method for r, _ in matches}))
            raise MethodNotAllowed("Method %s not allowed here" % req.method, headers={"Allow": allow})
        raise NotFound("No such endpoint: %s" % req.path, code="endpoint_not_found")

    def run(self, r: Route, params, req: Request, info: RequestInfo):
        info.route = r.pattern
        limit = getattr(self.config, r.body_limit) if r.body_limit else self.config.max_body_bytes
        req.check_size(limit)
        conn = connect(self.config.db_path)
        try:
            user = None
            if not r.public:
                try:
                    user = auth.authenticate(conn, req.authorization)
                except Unauthorized:
                    self._on_auth_failure(info)
                    raise
                info.user = user
                self._charge_request(info, user)
                if ROLE_RANK[user["role"]] < ROLE_RANK[r.role]:
                    raise Forbidden(
                        "This action requires the %r role or higher (you are %r)" % (r.role, user["role"]),
                        code="insufficient_role",
                    )
            with transaction(conn, write=req.method not in ("GET", "HEAD")):
                ctx = Context(conn, user, self.clock(), self.tz, self.config, request=info, throttle=self.throttle)
                result = r.handler(ctx, req, **params)
            notify.dispatch(self.notifier, ctx.events, self.config.public_url)
            self._audit_success(info, user, ctx)
        finally:
            conn.close()
        if isinstance(result, Raw):
            headers = [("Content-Type", result.content_type), ("Cache-Control", "no-store")] + result.headers
            return result.status, headers, result.body
        if isinstance(result, tuple):
            return self.json_response(result[0], result[1])
        return self.json_response(200, result)
