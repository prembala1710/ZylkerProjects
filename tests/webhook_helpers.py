"""Test doubles for the webhook tests: a scriptable local receiver, a scripted sender and a ready-made fixture."""

from __future__ import annotations

import dataclasses
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ApiTestCase

from zylker_pm.app import App
from zylker_pm.db import connect
from zylker_pm.webhook_http import SendResult
from zylker_pm.webhook_worker import WebhookWorker

OK = SendResult(True, 200, None, "ok", 3)
FAIL_500 = SendResult(False, 500, "http_500", "boom", 4)


class Receiver:
    """A local HTTP endpoint that records every POST and answers as scripted."""

    def __init__(self, status=200, body=b"ok", delay=0.0, headers=()):
        outer = self
        self.requests = []
        self.status, self.body, self.delay, self.extra_headers = status, body, delay, list(headers)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "raw": raw})
                if outer.delay:
                    time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    for name, value in outer.extra_headers:
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    self.wfile.write(outer.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        self.url = "http://127.0.0.1:%d/hooks/zylker?key=abc" % self.port
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSender:
    """Stands in for ``webhook_http.post``: replays scripted results (the last one repeats) and records calls."""

    def __init__(self, *results):
        self.results = list(results) or [OK]
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, body, headers, timeout=10.0, allow_private=False):
        with self.lock:
            self.calls.append({"url": url, "body": body, "headers": dict(headers), "timeout": timeout, "allow_private": allow_private})
            result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


class WebhookCase(ApiTestCase):
    """ApiTestCase whose config lets webhooks reach 127.0.0.1, plus helpers for reading the queue."""

    def setUp(self):
        super().setUp()
        self.use_config(webhook_allow_private=True)
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)

    def use_config(self, **changes):
        self.config = dataclasses.replace(self.config, **changes)
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.admin = self.client("admin")

    def worker(self, sender=None, **kwargs):
        kwargs.setdefault("concurrency", 1)
        return WebhookWorker(self.config, clock=lambda: self.now, sender=sender or FakeSender(), **kwargs)

    def make_hook(self, **fields):
        body = {"name": "Hook", "url": self.receiver.url, "events": ["*"], **fields}
        return self.ok(self.admin.post("/api/webhooks", body), 201)

    def rows(self, sql, *args):
        conn = connect(self.config.db_path)
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def queue(self, hook_id=None):
        sql = "SELECT * FROM webhook_deliveries"
        rows = self.rows(sql + (" WHERE webhook_id = ?" if hook_id else "") + " ORDER BY id", *([hook_id] if hook_id else []))
        return rows

    def events_queued(self, hook_id=None):
        return [r["event"] for r in self.queue(hook_id)]

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()
