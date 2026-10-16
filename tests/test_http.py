"""HTTP-level hardening: headers, static file whitelist, size limits, error hygiene, concurrency,
and one pass through the real threaded HTTP server."""

import json
import logging
import threading
import urllib.error
import urllib.request
from unittest import mock

from helpers import ApiTestCase

from zylker_pm import app as app_module
from zylker_pm.cli import make_threaded_server


class HeaderTests(ApiTestCase):
    def test_security_headers_on_api_and_static_responses(self):
        for response in (self.admin.get("/api/me"), self.client().get("/api/health"), self.client().get("/"), self.client().get("/nope")):
            h = response.headers
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")
            self.assertEqual(h["X-Frame-Options"], "DENY")
            self.assertEqual(h["Referrer-Policy"], "no-referrer")
            csp = h["Content-Security-Policy"]
            self.assertIn("default-src 'self'", csp)
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("unsafe-eval", csp)
            self.assertIn("frame-ancestors 'none'", csp)

    def test_api_responses_are_never_cached_and_declare_utf8_json(self):
        r = self.admin.get("/api/me")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["Content-Type"], "application/json; charset=utf-8")


class RoutingTests(ApiTestCase):
    def test_unknown_paths_and_wrong_methods(self):
        r = self.admin.get("/api/nothing/here")
        self.assertEqual((r.status, r.error_code), (404, "endpoint_not_found"))
        r = self.admin.delete("/api/me")
        self.assertEqual((r.status, r.error_code), (405, "method_not_allowed"))
        self.assertEqual(r.headers["Allow"], "GET")
        r = self.admin.request("PUT", "/api/tasks/WEB-1", {})
        self.assertEqual(r.status, 405)
        self.assertEqual(sorted(r.headers["Allow"].split(", ")), ["DELETE", "GET", "PATCH"])

    def test_head_returns_headers_without_a_body(self):
        r = self.admin.request("HEAD", "/api/me")
        self.assertEqual((r.status, r.body), (200, b""))
        self.assertEqual(self.client().request("HEAD", "/").status, 200)

    def test_trailing_slashes_and_odd_keys_do_not_crash(self):
        for path in ("/api/projects/", "/api/tasks//", "/api/tasks/%00", "/api/tasks/" + "A" * 5000, "/api/users/abc", "/api/comments/-1"):
            self.assertLess(self.admin.get(path).status, 500, path)


class StaticTests(ApiTestCase):
    def test_only_whitelisted_files_are_served(self):
        for path, kind in (("/", "text/html"), ("/static/app.js", "text/javascript"), ("/static/styles.css", "text/css")):
            r = self.client().get(path)
            self.assertEqual(r.status, 200, path)
            self.assertTrue(r.headers["Content-Type"].startswith(kind), path)

    def test_path_traversal_never_reaches_the_filesystem(self):
        for path in (
            "/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fapp.py", "/static/app.js/../../config.py",
            "/static/", "/static", "/static/index.html", "/../etc/passwd", "/static/styles.css/", "//static/app.js",
        ):  # fmt: skip
            r = self.client().get(path)
            self.assertEqual(r.status, 404, path)
            self.assertNotIn("import", r.text)  # no Python source leaked

    def test_static_files_cannot_be_written_or_deleted(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertIn(self.admin.request(method, "/static/app.js", {}).status, (404, 405), method)


class LimitTests(ApiTestCase):
    def test_oversized_bodies_are_refused_without_being_read(self):
        env_body = b"x" * 10
        r = self.admin.request("POST", "/api/projects", raw=env_body, content_type="application/json", headers={})
        self.assertNotEqual(r.status, 413)
        big = mock.MagicMock()
        big.read.side_effect = AssertionError("body must not be read")
        captured = {}
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/projects", "QUERY_STRING": "", "CONTENT_LENGTH": str(2_000_000),
            "CONTENT_TYPE": "application/json", "wsgi.input": big, "HTTP_AUTHORIZATION": "Bearer " + self.tokens["alice"],
        }  # fmt: skip
        out = b"".join(self.app(environ, lambda status, headers: captured.update(status=status)))
        self.assertTrue(captured["status"].startswith("413"))
        self.assertEqual(json.loads(out)["error"]["code"], "payload_too_large")
        big.read.assert_not_called()

    def test_import_allows_larger_bodies_than_the_json_limit(self):
        self.make_project("IMP")
        row = "a" * 150 + "\n"
        text = "summary\n" + row * 8000  # ~1.2 MB: over the JSON limit, under the import limit
        r = self.client("alice").request("POST", "/api/projects/IMP/import", raw=text.encode(), content_type="text/csv", query={"dry_run": "true"})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.json["errors"][0]["message"].split(";")[0][:9], "row limit")  # 5,000-row cap, not a 413

    def test_bad_content_length_is_a_400(self):
        captured = {}
        environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/projects", "CONTENT_LENGTH": "banana", "wsgi.input": None}
        out = b"".join(self.app(environ, lambda s, h: captured.update(status=s)))
        self.assertTrue(captured["status"].startswith("400"), out)


class ErrorHygieneTests(ApiTestCase):
    def test_unexpected_errors_return_a_generic_500_with_no_internals(self):
        route = next(r for r in app_module.ROUTES if r.pattern == "/api/me")
        original = route.handler

        def boom(ctx, req):
            raise RuntimeError("secret internal detail /srv/app/config.py")

        route.handler = boom
        try:
            with self.assertLogs("zylker.access", level="ERROR"):
                r = self.admin.get("/api/me")
        finally:
            route.handler = original
        self.assertEqual((r.status, r.error_code), (500, "internal_error"))
        self.assertNotIn("secret", r.text)
        self.assertNotIn("Traceback", r.text)
        self.assertEqual(self.admin.get("/api/me").status, 200)  # and the server carries on (connection released)

    def test_a_failed_request_leaves_no_partial_writes(self):
        self.make_project("WEB", members=("bob",))
        route = next(r for r in app_module.ROUTES if r.pattern == "/api/projects/{key}/tasks")
        original = route.handler

        def create_then_crash(ctx, req, key):
            original(ctx, req, key)
            raise RuntimeError("late failure")

        route.handler = create_then_crash
        try:
            with self.assertLogs("zylker.access", level="ERROR"):
                r = self.client("alice").post("/api/projects/WEB/tasks", {"title": "ghost"})
        finally:
            route.handler = original
        self.assertEqual(r.status, 500)
        self.assertEqual(self.ok(self.admin.get("/api/tasks"))["total"], 0)  # rolled back
        self.assertEqual(self.make_task("WEB", "real")["key"], "WEB-1")  # and the number was not burned

    def test_a_hostile_path_cannot_forge_access_log_lines(self):
        with self.assertLogs("zylker.access", level="INFO") as logs:
            self.admin.get("/api/nope\nINFO zylker.access forged line -> 200\r\x00")
        self.assertEqual(len(logs.output), 1)
        record = logs.records[0].getMessage()
        self.assertNotRegex(record, r"[\r\n\x00]")
        self.assertIn("forged line", record)  # still visible for forensics, just harmless

    def test_notifications_are_not_sent_for_a_request_that_fails_after_emitting_them(self):
        self.make_project("WEB", members=("bob",))
        route = next(r for r in app_module.ROUTES if r.pattern == "/api/projects/{key}/tasks")
        original = route.handler

        def announce_then_crash(ctx, req, key):
            original(ctx, req, key)  # creates the task and queues a "task assigned" event
            assert ctx.events, "precondition: an event is pending"
            raise RuntimeError("late failure")

        route.handler = announce_then_crash
        try:
            with self.assertLogs("zylker.access", level="ERROR"):
                r = self.client("alice").post("/api/projects/WEB/tasks", {"title": "x", "assignee": "bob"})
        finally:
            route.handler = original
        self.assertEqual(r.status, 500)
        self.assertEqual(self.notifier.messages, [])  # nobody was told about work that never happened
        self.make_task("WEB", "ok", assignee="bob")
        self.assertEqual(len(self.notifier.messages), 1)  # while a successful request still notifies

    def test_tokens_are_never_echoed_or_logged(self):
        bogus = "zpm_bogus-value-for-the-log-test"  # not a credential: proves a presented token is never echoed or logged
        with self.assertLogs("zylker.access", level="INFO") as logs:
            r = self.client().get("/api/me", token=bogus)
        self.assertEqual(r.status, 401)
        self.assertNotIn("bogus-value", r.text)
        self.assertNotIn("bogus-value", " ".join(logs.output))
        for line in logs.output:
            self.assertRegex(line, r"GET /api/me -> 401")


class ConcurrencyTests(ApiTestCase):
    def test_parallel_writers_get_unique_gapless_task_numbers(self):
        self.make_project("WEB", members=("bob", "carol"))
        results, errors = [], []

        def worker(user):
            c = self.client(user)
            for i in range(6):
                r = c.post("/api/projects/WEB/tasks", {"title": "%s-%d" % (user, i)})
                (results if r.status == 201 else errors).append(r.json["key"] if r.status == 201 else r.text)

        threads = [threading.Thread(target=worker, args=(u,)) for u in ("alice", "bob", "carol", "alice", "bob")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results, key=lambda k: int(k.split("-")[1])), ["WEB-%d" % i for i in range(1, 31)])

    def test_readers_are_not_blocked_by_a_writer(self):
        self.make_project("WEB")
        seen = []

        def reader():
            for _ in range(10):
                seen.append(self.client("vera").get("/api/projects/WEB/summary").status)

        t = threading.Thread(target=reader)
        t.start()
        for i in range(10):
            self.make_task("WEB", "t%d" % i)
        t.join()
        self.assertEqual(set(seen), {200})


class LiveServerTests(ApiTestCase):
    """The real wsgiref stack (what ``zylker-pm serve`` runs), over a socket."""

    def setUp(self):
        super().setUp()
        self.server = make_threaded_server(self.app, "127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        logging.getLogger("zylker.access").setLevel(logging.WARNING)

    def call(self, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_json_round_trip_over_a_socket(self):
        status, _, body = self.call("GET", "/api/health")
        self.assertEqual((status, json.loads(body)["status"]), (200, "ok"))
        status, _, _ = self.call("POST", "/api/projects", {"key": "NET", "name": "Over the wire"})
        self.assertEqual(status, 401)
        status, headers, body = self.call("POST", "/api/projects", {"key": "NET", "name": "Over the wire ✓"}, self.tokens["alice"])
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["name"], "Over the wire ✓")  # UTF-8 survives the trip
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, _, body = self.call("POST", "/api/projects/NET/tasks", {"title": "Task via HTTP"}, self.tokens["alice"])
        self.assertEqual((status, json.loads(body)["key"]), (201, "NET-1"))
        status, _, body = self.call("GET", "/api/projects/NET/export.csv", token=self.tokens["alice"])
        self.assertIn(b"Task via HTTP", body)

    def test_static_ui_is_served(self):
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
