"""Shared test helpers: an in-process WSGI client and a ready-made organisation fixture."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zylker_pm.app import App  # noqa: E402
from zylker_pm.config import Config  # noqa: E402
from zylker_pm.db import init_db  # noqa: E402
from zylker_pm.notify import RecordingNotifier  # noqa: E402
from zylker_pm.services import users  # noqa: E402

# A Friday. Fixed so overdue / health / burndown results are deterministic.
FIXED_NOW = datetime(2026, 10, 16, 9, 0, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, status, headers, body):
        self.status = int(status.split()[0])
        self.headers = dict(headers)
        self.body = body

    @property
    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self):
        return self.body.decode("utf-8")

    @property
    def error_code(self):
        return self.json["error"]["code"]


class Client:
    def __init__(self, app, token=None):
        self.app, self.token = app, token

    def request(self, method, path, body=None, *, query=None, raw=None, content_type=None, headers=None, token=...):
        if raw is None and body is not None:
            raw, content_type = json.dumps(body).encode("utf-8"), content_type or "application/json"
        raw = raw or b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": urlencode(query or {}),
            "SERVER_NAME": "test",
            "SERVER_PORT": "80",
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(raw),
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": content_type or "",
        }
        tok = self.token if token is ... else token
        if tok:
            environ["HTTP_AUTHORIZATION"] = "Bearer " + tok
        for k, v in (headers or {}).items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
        captured = {}

        def start_response(status, response_headers):
            captured["status"], captured["headers"] = status, response_headers

        body_out = b"".join(self.app(environ, start_response))
        return Response(captured["status"], captured["headers"], body_out)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def patch(self, path, body, **kw):
        return self.request("PATCH", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


class CaptureServer:
    """A throw-away local HTTP server that records what the notifier posts."""

    def __init__(self, status=200):
        outer = self
        self.requests, self.status = [], status

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/api/v2/channelsbyname/pm/message?zapikey=SECRETKEY" % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ApiTestCase(unittest.TestCase):
    """Creates a temp DB, an App with a fixed clock, and the users: admin, alice (manager),
    bob/carol (members) and vera (viewer)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(db_path=os.path.join(self.tmp, "test.db"))
        init_db(self.config.db_path)
        self.now = FIXED_NOW
        self.notifier = RecordingNotifier()
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.tokens = {}
        for username, role in (("admin", "admin"), ("alice", "manager"), ("bob", "member"), ("carol", "member"), ("vera", "viewer")):
            self.make_user(username, role)
        self.admin = self.client("admin")

    def make_user(self, username, role, **extra):
        with self.app.session() as ctx:
            user, token = users.create_user(
                ctx, {"username": username, "display_name": username.capitalize(), "role": role, **extra}
            )
        self.tokens[username] = token
        return user

    def client(self, username=None, token=None):
        return Client(self.app, token or self.tokens.get(username))

    def ok(self, response, status=200):
        self.assertEqual(response.status, status, response.text)
        return response.json if response.body else None

    def make_project(self, key="WEB", members=("bob", "carol"), **extra):
        body = {"key": key, "name": "Project " + key, "members": list(members), **extra}
        return self.ok(self.client("alice").post("/api/projects", body), 201)

    def make_task(self, key="WEB", title="A task", as_user="alice", **fields):
        return self.ok(self.client(as_user).post("/api/projects/%s/tasks" % key, {"title": title, **fields}), 201)

    def set_status(self, task_key, status, as_user="alice"):
        return self.client(as_user).patch("/api/tasks/" + task_key, {"status": status})

    def advance_days(self, days):
        from datetime import timedelta

        self.now = self.now + timedelta(days=days)
