"""Outbound webhooks: signing, the network guard, the outbox, delivery with retries, and the admin API."""

import contextlib
import dataclasses
import hmac
import io
import json
import os
import socket
import sqlite3
import threading
import time
import unittest
from datetime import timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import ApiTestCase

from zylker_pm import cli, db, netguard
from zylker_pm.app import App
from zylker_pm.config import Config
from zylker_pm.db import init_db
from zylker_pm.netguard import SendResult
from zylker_pm.services import activity, webhooks

PUBLIC_URL = "https://hooks.example.com/zylker"


class FakeSender:
    """Stands in for the network: records every call and answers from a script (then 200)."""

    def __init__(self, *results):
        self.calls, self.results, self.lock = [], list(results), threading.Lock()

    def __call__(self, url, body, headers, timeout=10):
        with self.lock:
            self.calls.append({"url": url, "body": body, "headers": headers, "json": json.loads(body)})
            result = self.results.pop(0) if self.results else SendResult(200, None, 5)
        return result(url) if callable(result) else result


class HookServer:
    """A throw-away local receiver that keeps the raw request so signatures can be checked."""

    def __init__(self, status=200, delay=0.0, location=None):
        outer = self
        self.requests, self.status, self.delay, self.location = [], status, delay, location

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if outer.delay:
                    time.sleep(outer.delay)
                with contextlib.suppress(OSError):
                    self.send_response(outer.status)
                    if outer.location:
                        self.send_header("Location", outer.location)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        self.url = "http://127.0.0.1:%d/hook" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def resolver_for(*addresses):
    return lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]  # noqa: A002


# --------------------------------------------------------------------------------------- signing


class SigningTests(unittest.TestCase):
    secret = "whsec_unit-test-secret"
    body = b'{"event":"task.created"}'

    def test_the_signature_is_an_hmac_over_timestamp_dot_body(self):
        expected = hmac.new(self.secret.encode(), b"1700000000." + self.body, sha256).hexdigest()
        self.assertEqual(webhooks.sign(self.secret, 1700000000, self.body), "v1=" + expected)

    def test_a_genuine_request_verifies(self):
        sig = webhooks.sign(self.secret, 1700000000, self.body)
        self.assertTrue(webhooks.verify_signature(self.secret, "1700000000", sig, self.body, now=1700000100))

    def test_tampering_a_wrong_secret_and_garbage_are_refused(self):
        sig = webhooks.sign(self.secret, 1700000000, self.body)
        verify = lambda **kw: webhooks.verify_signature(**{"secret": self.secret, "timestamp_header": "1700000000", "signature_header": sig, "body": self.body, "now": 1700000000, **kw})  # noqa: E731
        self.assertTrue(verify())
        self.assertFalse(verify(body=self.body + b" "))
        self.assertFalse(verify(secret="whsec_other"))
        self.assertFalse(verify(signature_header="v1=" + "0" * 64))
        self.assertFalse(verify(signature_header=None))
        self.assertFalse(verify(signature_header="not a signature \u00e9"))
        self.assertFalse(verify(timestamp_header="soon"))
        self.assertFalse(verify(timestamp_header=None))

    def test_a_captured_request_cannot_be_replayed_later(self):
        sig = webhooks.sign(self.secret, 1700000000, self.body)
        for now, expected in ((1700000300, True), (1700000301, False), (1699999700, True), (1699999699, False)):
            self.assertEqual(webhooks.verify_signature(self.secret, "1700000000", sig, self.body, now=now), expected, now)

    def test_secrets_are_long_random_and_never_repeat(self):
        secrets = {webhooks.new_secret() for _ in range(50)}
        self.assertEqual(len(secrets), 50)
        self.assertTrue(all(s.startswith("whsec_") and len(s) > 40 for s in secrets))


# --------------------------------------------------------------------------------------- network guard


class UrlValidationTests(unittest.TestCase):
    def test_good_urls(self):
        t = netguard.validate_url("https://hooks.example.com/a/b?x=1")
        self.assertEqual((t.scheme, t.host, t.port, t.path), ("https", "hooks.example.com", 443, "/a/b?x=1"))
        self.assertEqual(netguard.validate_url("https://hooks.example.com:8443").port, 8443)
        self.assertEqual(netguard.validate_url("https://hooks.example.com").path, "/")
        self.assertEqual(netguard.validate_url("https://93.184.216.34/x").host, "93.184.216.34")

    def test_bad_urls_say_why(self):
        cases = {
            "": "must be a URL",
            "ftp://example.com/x": "https",
            "javascript:alert(1)": "https",
            "//example.com/x": "https",
            "http://example.com/x": "https",
            "https://": "host",
            "https://user:pw@example.com/": "user name or password",
            "https://@example.com/": "user name or password",
            "https://example.com/a b": "spaces",
            "https://example.com\\@evil.test/": "backslashes",
            "https://example.com/#frag": "fragment",
            "https://example.com:0/": "port",
            "https://example.com:99999/": "not a valid URL",
            "https://exa_mple.com/": "host name",
            "https://caf\u00e9.example/": "host name",
            "https://localhost/x": "local host name",
            "https://printer.local/x": "local host name",
            "https://db.internal/x": "local host name",
            "https://127.0.0.1/x": "private",
            "https://10.1.2.3/x": "private",
            "https://192.168.0.10/x": "private",
            "https://169.254.169.254/latest/meta-data/": "private",
            "https://[::1]/x": "private",
            "https://[fd00::1]/x": "private",
            "https://[::ffff:10.0.0.1]/x": "private",
            "https://" + "a" * 2000 + ".com/": "at most",
        }
        for url, fragment in cases.items():
            with self.assertRaises(ValueError, msg=url) as cm:
                netguard.validate_url(url)
            self.assertIn(fragment, str(cm.exception), url)

    def test_the_opt_out_allows_plain_http_and_private_addresses(self):
        for url in ("http://127.0.0.1:9000/x", "http://localhost/x", "https://10.0.0.5/x", "http://intranet.local/x", "http://[::1]:8/x"):
            self.assertEqual(netguard.validate_url(url, allow_private=True).scheme, url.split(":")[0])
        with self.assertRaises(ValueError):
            netguard.validate_url("ftp://127.0.0.1/", allow_private=True)  # the opt-out is not "anything goes"

    def test_which_addresses_count_as_public(self):
        public = ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"]
        private = [
            "127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
            "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:0a00:0001::1", "64:ff9b::a00:1", "not an address",
        ]  # fmt: skip
        self.assertEqual([a for a in public if not netguard.is_public(a)], [])
        self.assertEqual([a for a in private if netguard.is_public(a)], [])
        self.assertTrue(netguard.is_public("::ffff:93.184.216.34"))


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.server = HookServer()
        self.addCleanup(self.server.close)

    def post(self, url=None, **kw):
        kw.setdefault("allow_private", True)
        return netguard.send(url or self.server.url, b'{"a":1}', {"Content-Type": "application/json", "X-Test": "yes"}, kw.pop("timeout", 5), **kw)

    def test_a_delivery_reaches_the_receiver_with_its_headers_and_body(self):
        r = self.post()
        self.assertEqual((r.status, r.error, r.ok), (200, None, True))
        [req] = self.server.requests
        self.assertEqual((req["path"], req["body"], req["headers"]["x-test"]), ("/hook", b'{"a":1}', "yes"))

    def test_private_addresses_are_refused_by_default_and_nothing_is_sent(self):
        r = self.post(allow_private=False)
        self.assertEqual((r.status, r.error), (None, "invalid_url"))  # plain http to loopback is rejected before any lookup
        self.assertEqual(self.server.requests, [])

    def test_a_public_looking_name_that_resolves_privately_is_refused(self):
        for answers in (["10.0.0.5"], ["93.184.216.34", "127.0.0.1"], ["::1"], ["169.254.169.254"]):
            r = netguard.send("https://hooks.example.com/x", b"{}", {}, 2, resolver=resolver_for(*answers))
            self.assertEqual((r.status, r.error), (None, "blocked_address"), answers)

    def test_it_connects_to_the_address_it_vetted_not_to_the_name(self):
        # the name would resolve elsewhere; the connection goes to the answer we got and checked
        r = netguard.send("http://hooks.example.test:%d/hook" % self.server.port, b"{}", {}, 5, allow_private=True, resolver=resolver_for("127.0.0.1"))
        self.assertTrue(r.ok)
        self.assertEqual(len(self.server.requests), 1)

    def test_dns_failure_and_invalid_urls_are_reported_as_codes(self):
        def failing(host, port, type=0):  # noqa: A002
            raise socket.gaierror("no such host")

        self.assertEqual(netguard.send("https://nope.example.com/", b"{}", {}, 2, resolver=failing).error, "dns_failure")
        self.assertEqual(netguard.send("https://nope.example.com/", b"{}", {}, 2, resolver=resolver_for()).error, "dns_failure")
        self.assertEqual(netguard.send("gopher://x/", b"{}", {}, 2).error, "invalid_url")

    def test_redirects_are_not_followed(self):
        other = HookServer()
        self.addCleanup(other.close)
        self.server.status, self.server.location = 302, other.url
        r = self.post()
        self.assertEqual((r.status, r.ok), (302, False))
        self.assertEqual(other.requests, [])

    def test_failures_become_short_codes(self):
        self.assertEqual(self.post("http://127.0.0.1:%d/x" % closed_port()).error, "connection_refused")
        self.server.delay = 1.5
        self.assertEqual(self.post(timeout=0.3).error, "timeout")
        self.server.delay, self.server.status = 0, 503
        r = self.post()
        self.assertEqual((r.status, r.error, r.ok), (503, None, False))

    def test_a_tls_server_that_is_not_there_does_not_raise(self):
        r = netguard.send("https://127.0.0.1:%d/x" % self.server.port, b"{}", {}, 2, allow_private=True)  # plain-http server, TLS client
        self.assertIsNone(r.status)
        self.assertIn(r.error, {"tls_error", "network_error", "protocol_error", "timeout", "connection_failed", "tls_certificate"})


# --------------------------------------------------------------------------------------- the API and worker


class WebhookCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        for user in ("admin", "alice", "bob", "carol", "vera"):  # first requests start sessions (audit events); get those out of the way
            self.client(user).get("/api/me")

    def hook(self, url=PUBLIC_URL, events=("task.*",), **extra):
        body = {"name": extra.pop("name", "Zylker hook"), "url": url, "events": list(events), **extra}
        return self.ok(self.admin.post("/api/webhooks", body), 201)

    def run_due(self, sender=None, now=None, **kw):
        return webhooks.run_due(self.config.db_path, now or self.now, sender or FakeSender(), **kw)

    def deliveries(self, hook_id, **query):
        return self.ok(self.admin.get("/api/webhooks/%d/deliveries" % hook_id, query=query))["items"]

    def detail(self, hook_id, delivery_id):
        return self.ok(self.admin.get("/api/webhooks/%d/deliveries/%d" % (hook_id, delivery_id)))

    def get_hook(self, hook_id):
        return self.ok(self.admin.get("/api/webhooks/%d" % hook_id))

    def sql(self, query, *args):
        conn = db.connect(self.config.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(query, args)
            rows = cur.fetchall() if query.lstrip().upper().startswith("SELECT") else None
            conn.execute("COMMIT")
            return rows
        finally:
            conn.close()

    def log_events(self, n, action="task.created", project="WEB"):
        with self.app.session() as ctx:
            for i in range(n):
                activity.log(ctx, action, project_key=project, task_key="%s-%d" % (project, i + 1), details={"i": i})


class ManagementTests(WebhookCase):
    def test_creating_a_webhook_shows_the_secret_once(self):
        h = self.hook(events=["task.created", "project.*"])
        self.assertTrue(h["secret"].startswith("whsec_"))
        self.assertEqual((h["active"], h["events"], h["project"], h["failure_streak"]), (True, ["task.created", "project.*"], None, 0))
        self.assertEqual(h["secret_hint"], "whsec_..." + h["secret"][-4:])
        for view in (self.get_hook(h["id"]), self.ok(self.admin.get("/api/webhooks"))["items"][0]):
            self.assertNotIn("secret", view)
            self.assertNotIn(h["secret"], json.dumps(view))

    def test_the_list_describes_what_can_be_subscribed_to(self):
        info = self.ok(self.admin.get("/api/webhooks"))
        self.assertEqual(info["items"], [])
        self.assertIn("task", info["categories"])
        self.assertNotIn("webhook", info["categories"])
        self.assertEqual(info["limits"]["max_attempts"], 6)
        self.assertEqual(info["limits"]["retry_delays_seconds"], [60, 300, 1800, 7200, 21600])

    def test_only_admins_can_touch_webhooks(self):
        h = self.hook()
        calls = [("get", "/api/webhooks"), ("post", "/api/webhooks"), ("get", "/api/webhooks/%d" % h["id"]), ("patch", "/api/webhooks/%d" % h["id"]),
                 ("delete", "/api/webhooks/%d" % h["id"]), ("post", "/api/webhooks/%d/rotate-secret" % h["id"]), ("post", "/api/webhooks/%d/test" % h["id"]),
                 ("get", "/api/webhooks/%d/deliveries" % h["id"]), ("get", "/api/webhooks/%d/deliveries/1" % h["id"]),
                 ("post", "/api/webhooks/%d/deliveries/1/redeliver" % h["id"])]  # fmt: skip
        for user in ("alice", "bob", "vera"):
            for method, path in calls:
                args = ({},) if method in ("post", "patch") else ()
                self.assertEqual(getattr(self.client(user), method)(path, *args).status, 403, (user, method, path))
        self.assertEqual(self.client().get("/api/webhooks", token=None).status, 401)

    def test_bad_input_is_reported_field_by_field(self):
        for body, field in (
            ({"url": PUBLIC_URL, "events": ["task.*"]}, "name"),
            ({"name": "x", "events": ["task.*"]}, "url"),
            ({"name": "x", "url": PUBLIC_URL}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": []}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": "task.*"}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["bogus.created"]}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["task.*", "task"]}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["webhook.*"]}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["task.Created"]}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": [5]}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["*"] * 31}, "events"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["*"], "project": "NOPE"}, "project"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["*"], "project": "bad key!"}, "project"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["*"], "secret": "short"}, "secret"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["*"], "secret": "\u00e9" * 20}, "secret"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["*"], "active": "yes"}, "active"),
            ({"name": "x", "url": PUBLIC_URL, "events": ["*"], "colour": "red"}, "colour"),
            ({"name": "bad\x07name", "url": PUBLIC_URL, "events": ["*"]}, "name"),
            ({"name": "n" * 61, "url": PUBLIC_URL, "events": ["*"]}, "name"),
        ):
            r = self.admin.post("/api/webhooks", body)
            self.assertEqual(r.status, 422, body)
            self.assertIn(field, r.json["error"]["details"], body)
        self.assertEqual(self.ok(self.admin.get("/api/webhooks"))["items"], [])

    def test_urls_that_would_reach_internal_services_are_refused(self):
        for url in ("http://example.com/x", "https://127.0.0.1/x", "https://10.0.0.1/", "https://169.254.169.254/latest/meta-data/", "https://[::1]/",
                    "https://localhost/x", "https://user:pw@example.com/", "https://example.com/a b", "ftp://example.com", "javascript:alert(1)"):  # fmt: skip
            r = self.admin.post("/api/webhooks", {"name": "x", "url": url, "events": ["*"]})
            self.assertEqual((r.status, "url" in r.json["error"]["details"]), (422, True), url)

    def test_an_installation_that_opts_in_may_use_internal_receivers(self):
        self.app = App(dataclasses.replace(self.config, webhook_allow_private=True), clock=lambda: self.now, notifier=self.notifier)
        self.admin = self.client("admin")
        h = self.hook(url="http://127.0.0.1:9000/internal")
        self.assertEqual(h["url"], "http://127.0.0.1:9000/internal")

    def test_names_are_unique_ignoring_case_and_the_count_is_limited(self):
        self.hook(name="Alpha")
        self.assertEqual(self.admin.post("/api/webhooks", {"name": "ALPHA", "url": PUBLIC_URL, "events": ["*"]}).error_code, "webhook_name_taken")
        for i in range(19):
            self.hook(name="hook %d" % i)
        r = self.admin.post("/api/webhooks", {"name": "one too many", "url": PUBLIC_URL, "events": ["*"]})
        self.assertEqual((r.status, r.error_code), (409, "webhook_limit_reached"))

    def test_a_supplied_secret_is_used_and_a_project_scope_is_checked(self):
        self.make_project("WEB")
        h = self.hook(secret="my-own-secret-123456", project="web")
        self.assertEqual((h["secret"], h["project"]), ("my-own-secret-123456", "WEB"))

    def test_updating_changes_only_what_is_sent(self):
        h = self.hook()
        r = self.ok(self.admin.patch("/api/webhooks/%d" % h["id"], {"events": ["project.*"], "name": "Renamed"}))
        self.assertEqual((r["events"], r["name"], r["url"]), (["project.*"], "Renamed", PUBLIC_URL))
        self.assertEqual(self.admin.patch("/api/webhooks/%d" % h["id"], {"url": "http://127.0.0.1/x"}).status, 422)
        self.assertEqual(self.admin.patch("/api/webhooks/%d" % h["id"], {"secret": "x" * 20}).status, 422)  # only rotate-secret changes it
        self.assertEqual(self.admin.patch("/api/webhooks/%d" % h["id"], {"events": ["nope.*"]}).status, 422)
        other = self.hook(name="Other")
        self.assertEqual(self.admin.patch("/api/webhooks/%d" % other["id"], {"name": "renamed"}).error_code, "webhook_name_taken")
        self.assertEqual(self.ok(self.admin.patch("/api/webhooks/%d" % h["id"], {"name": "RENAMED"}))["name"], "RENAMED")  # own name, new case: fine

    def test_switching_off_cancels_what_is_queued_and_switching_on_is_a_fresh_start(self):
        h = self.hook()
        self.log_events(3)
        self.assertEqual(len(self.deliveries(h["id"], status="pending")), 3)
        self.sql("UPDATE webhooks SET failure_streak = 9, disabled_reason = 'too_many_failures', disabled_at = 'x' WHERE id = ?", h["id"])
        off = self.ok(self.admin.patch("/api/webhooks/%d" % h["id"], {"active": False}))
        self.assertFalse(off["active"])
        self.assertEqual([d["status"] for d in self.deliveries(h["id"])], ["cancelled"] * 3)
        self.assertEqual({d["error"] for d in self.deliveries(h["id"])}, {"webhook_disabled"})
        self.log_events(1)
        self.assertEqual(len(self.deliveries(h["id"], status="pending")), 0)  # an inactive webhook queues nothing
        on = self.ok(self.admin.patch("/api/webhooks/%d" % h["id"], {"active": True}))
        self.assertEqual((on["active"], on["failure_streak"], on["disabled_reason"], on["disabled_at"]), (True, 0, None, None))

    def test_a_project_scope_can_be_set_and_cleared(self):
        self.make_project("WEB")
        h = self.hook()
        self.assertEqual(self.ok(self.admin.patch("/api/webhooks/%d" % h["id"], {"project": "WEB"}))["project"], "WEB")
        self.assertEqual(self.ok(self.admin.patch("/api/webhooks/%d" % h["id"], {"project": None}))["project"], None)
        self.assertEqual(self.admin.patch("/api/webhooks/%d" % h["id"], {"project": "ZZZ"}).status, 422)

    def test_unknown_webhooks_are_404(self):
        for method, path in (("get", "/api/webhooks/999"), ("get", "/api/webhooks/abc"), ("patch", "/api/webhooks/999"), ("delete", "/api/webhooks/999"),
                             ("post", "/api/webhooks/999/test"), ("post", "/api/webhooks/999/rotate-secret"), ("get", "/api/webhooks/999/deliveries")):  # fmt: skip
            args = ({},) if method in ("post", "patch") else ()
            self.assertEqual(getattr(self.admin, method)(path, *args).status, 404, path)
        h = self.hook()
        self.assertEqual(self.admin.get("/api/webhooks/%d/deliveries/999" % h["id"]).error_code, "delivery_not_found")
        self.assertEqual(self.admin.get("/api/webhooks/%d/deliveries/abc" % h["id"]).status, 404)

    def test_deleting_removes_the_webhook_and_its_log(self):
        h = self.hook()
        self.log_events(2)
        self.assertEqual(self.admin.delete("/api/webhooks/%d" % h["id"]).status, 204)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM webhook_deliveries")[0][0], 0)
        self.assertEqual(self.admin.get("/api/webhooks/%d" % h["id"]).status, 404)

    def test_rotating_the_secret_changes_it_and_signs_new_requests_with_it(self):
        h = self.hook()
        new = self.ok(self.admin.post("/api/webhooks/%d/rotate-secret" % h["id"]))
        self.assertNotEqual(new["secret"], h["secret"])
        self.assertEqual(new["secret_hint"], "whsec_..." + new["secret"][-4:])
        self.log_events(1)
        sender = FakeSender()
        self.run_due(sender)
        [call] = sender.calls
        args = (call["headers"]["X-Zylker-Timestamp"], call["headers"]["X-Zylker-Signature"], call["body"])
        self.assertTrue(webhooks.verify_signature(new["secret"], *args, now=self.now.timestamp()))
        self.assertFalse(webhooks.verify_signature(h["secret"], *args, now=self.now.timestamp()))

    def test_management_is_audited_without_secrets_or_url_paths(self):
        h = self.hook(url="https://hooks.example.com/services/T0KEN-IN-THE-PATH?key=SEKRET")
        self.admin.patch("/api/webhooks/%d" % h["id"], {"url": "https://hooks.example.com/services/ANOTHER-TOKEN"})
        self.admin.post("/api/webhooks/%d/rotate-secret" % h["id"])
        self.admin.delete("/api/webhooks/%d" % h["id"])
        events = self.ok(self.admin.get("/api/audit", query={"category": "webhook", "limit": 50}))["items"]
        self.assertEqual(sorted(e["action"] for e in events), ["webhook.created", "webhook.deleted", "webhook.secret_rotated", "webhook.updated"])
        text = json.dumps(events)
        for leak in ("T0KEN-IN-THE-PATH", "SEKRET", "ANOTHER-TOKEN", h["secret"]):
            self.assertNotIn(leak, text)
        by_action = {e["action"]: e for e in events}
        self.assertEqual(by_action["webhook.created"]["details"]["host"], "hooks.example.com")
        self.assertEqual(by_action["webhook.deleted"]["severity"], "notice")
        self.assertEqual(by_action["webhook.secret_rotated"]["actor"]["username"], "admin")


class OutboxTests(WebhookCase):
    def test_a_matching_change_is_queued_with_a_complete_payload(self):
        h = self.hook(events=["task.created"])
        self.make_project("WEB")
        self.make_task("WEB", "Ship it", assignee="bob", priority="high")
        [d] = self.deliveries(h["id"])
        self.assertEqual((d["event"], d["status"], d["attempts"], d["next_attempt_at"]), ("task.created", "pending", 0, "2026-10-16T09:00:00Z"))
        p = self.detail(h["id"], d["id"])["payload"]
        self.assertEqual((p["event"], p["project"], p["task"], p["occurred_at"]), ("task.created", "WEB", "WEB-1", "2026-10-16T09:00:00Z"))
        self.assertEqual((p["actor"]["username"], p["actor"]["display_name"]), ("alice", "Alice"))
        self.assertEqual(p["event_id"], d["event_id"])
        self.assertEqual(p["details"]["title"], "Ship it")

    def test_subscriptions_match_exact_wildcard_and_everything(self):
        exact = self.hook(name="exact", events=["task.created"])
        category = self.hook(name="category", events=["task.*"])
        everything = self.hook(name="all", events=["*"])
        nothing = self.hook(name="other", events=["milestone.*", "user.updated"])
        self.make_project("WEB")
        self.make_task("WEB", "A")
        self.set_status("WEB-1", "in_progress")
        seen = lambda h: sorted(d["event"] for d in self.deliveries(h["id"]))  # noqa: E731
        self.assertEqual(seen(exact), ["task.created"])
        self.assertEqual(seen(category), ["task.created", "task.status_changed"])
        self.assertEqual(seen(everything), ["project.created", "task.created", "task.status_changed"])
        self.assertEqual(seen(nothing), [])

    def test_a_project_scope_only_sees_its_own_project(self):
        self.make_project("WEB")
        self.make_project("OPS")
        scoped = self.hook(name="scoped", events=["task.*"], project="WEB")
        self.make_task("WEB", "in scope")
        self.make_task("OPS", "out of scope")
        self.log_events(1, action="user.updated", project=None)
        self.assertEqual([d["event"] for d in self.deliveries(scoped["id"])], ["task.created"])

    def test_a_request_that_rolls_back_announces_nothing(self):
        h = self.hook(events=["*"])
        with self.assertRaises(RuntimeError), self.app.session() as ctx:
            activity.log(ctx, "task.created", project_key="WEB", task_key="WEB-1", details={})
            raise RuntimeError("boom")
        self.assertEqual(self.deliveries(h["id"]), [])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM activity WHERE action = 'task.created'")[0][0], 0)

    def test_a_failed_api_request_announces_nothing(self):
        h = self.hook(events=["*"])
        self.make_project("WEB")
        before = len(self.deliveries(h["id"]))
        self.assertEqual(self.client("bob").post("/api/projects/WEB/tasks", {"title": ""}).status, 422)
        self.assertEqual(len(self.deliveries(h["id"])), before)

    def test_webhook_events_are_never_delivered_so_there_are_no_loops(self):
        h = self.hook(events=["*"])
        self.admin.patch("/api/webhooks/%d" % h["id"], {"name": "renamed"})
        self.admin.post("/api/webhooks/%d/rotate-secret" % h["id"])
        self.assertEqual(self.deliveries(h["id"]), [])

    def test_security_events_can_be_subscribed_to_and_repeats_are_not_repeated(self):
        h = self.hook(events=["auth.*"])
        for _ in range(3):
            self.assertEqual(self.client().get("/api/me", token="zpm_wrong").status, 401)
        items = self.deliveries(h["id"])
        self.assertEqual([d["event"] for d in items], ["auth.failed"])  # the audit log merges repeats; so does the outbox
        p = self.detail(h["id"], items[0]["id"])["payload"]
        self.assertEqual((p["actor"], p["project"], p["details"]["reason"]), (None, None, "invalid_token"))

    def test_an_oversized_event_is_trimmed_not_dropped(self):
        h = self.hook(events=["task.*"])
        with self.app.session() as ctx:
            activity.log(ctx, "task.updated", project_key="WEB", task_key="WEB-1", details={"blob": "x" * 70_000})
        [d] = self.deliveries(h["id"])
        self.assertEqual(self.detail(h["id"], d["id"])["payload"]["details"], {"truncated": True})

    def test_a_dead_receiver_cannot_grow_the_queue_without_bound(self):
        h = self.hook(events=["task.*"])
        with mock.patch.object(webhooks, "MAX_PENDING", 3):
            self.log_events(5)
        by = {}
        for d in self.deliveries(h["id"]):
            by.setdefault((d["status"], d["error"]), 0)
            by[(d["status"], d["error"])] += 1
        self.assertEqual(by, {("pending", None): 3, ("failed", "queue_full"): 2})

    def test_the_secret_is_never_in_a_payload_or_the_log(self):
        h = self.hook(events=["*"])
        self.make_project("WEB")
        self.make_task("WEB", "Something")
        everything = json.dumps([self.detail(h["id"], d["id"]) for d in self.deliveries(h["id"])]) + json.dumps(self.ok(self.admin.get("/api/audit")))
        self.assertNotIn(h["secret"], everything)


class DeliveryTests(WebhookCase):
    def setUp(self):
        super().setUp()
        self.h = self.hook(events=["task.*"])
        self.make_project("WEB")

    def only_delivery(self):
        [d] = self.deliveries(self.h["id"])
        return self.detail(self.h["id"], d["id"])

    def test_a_successful_delivery_is_signed_and_recorded(self):
        self.make_task("WEB", "First")
        sender = FakeSender(SendResult(204, None, 12))
        report = self.run_due(sender)
        self.assertEqual((report["delivered"], report["retrying"], report["failed"]), (1, 0, 0))
        [call] = sender.calls
        d = self.only_delivery()
        self.assertEqual(call["url"], PUBLIC_URL)
        h = call["headers"]
        self.assertEqual((h["X-Zylker-Event"], h["X-Zylker-Delivery"], h["X-Zylker-Attempt"], h["Content-Type"]), ("task.created", str(d["id"]), "1", "application/json"))
        self.assertTrue(h["User-Agent"].startswith("ZylkerProjects-Webhooks/"))
        self.assertEqual(h["X-Zylker-Timestamp"], str(int(self.now.timestamp())))
        self.assertTrue(webhooks.verify_signature(self.h["secret"], h["X-Zylker-Timestamp"], h["X-Zylker-Signature"], call["body"], now=self.now.timestamp()))
        body = call["json"]
        self.assertEqual((body["delivery_id"], body["attempt"], body["webhook"], body["event"], body["task"]), (d["id"], 1, {"id": self.h["id"], "name": "Zylker hook"}, "task.created", "WEB-1"))
        self.assertEqual((d["status"], d["attempts"], d["response_status"], d["error"], d["duration_ms"], d["finished_at"]), ("delivered", 1, 204, None, 12, "2026-10-16T09:00:00Z"))
        self.assertEqual(d["attempt_log"], [{"at": "2026-10-16T09:00:00Z", "attempt": 1, "status": 204, "error": None, "ms": 12}])
        hook = self.get_hook(self.h["id"])
        self.assertEqual((hook["last_status"], hook["last_success_at"], hook["failure_streak"], hook["deliveries"]["delivered"]), (204, "2026-10-16T09:00:00Z", 0, 1))

    def test_nothing_is_sent_twice(self):
        self.make_task("WEB", "First")
        sender = FakeSender()
        self.run_due(sender)
        self.assertEqual(self.run_due(sender)["delivered"], 0)
        self.assertEqual(len(sender.calls), 1)

    def test_failures_are_retried_on_a_widening_schedule_then_given_up_on(self):
        self.make_task("WEB", "First")
        sender = FakeSender(*[SendResult(500, None, 3)] * 6)
        t = self.now
        waits = [60, 300, 1800, 7200, 21600]
        self.run_due(sender, t)
        for attempt, wait in enumerate(waits, start=1):
            d = self.only_delivery()
            self.assertEqual((d["status"], d["attempts"], d["error"], d["response_status"]), ("pending", attempt, "http_500", 500))
            self.assertEqual(d["next_attempt_at"], (t + timedelta(seconds=wait)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertEqual(self.run_due(sender, t + timedelta(seconds=wait - 1))["retrying"], 0)  # too early: untouched
            self.assertEqual(len(sender.calls), attempt)
            t += timedelta(seconds=wait)
            self.run_due(sender, t)
        d = self.only_delivery()
        self.assertEqual((d["status"], d["attempts"], d["next_attempt_at"], d["finished_at"]), ("failed", 6, None, t.strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.assertEqual([a["attempt"] for a in d["attempt_log"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual({c["headers"]["X-Zylker-Delivery"] for c in sender.calls}, {str(d["id"])})  # one id across every attempt
        self.assertEqual([c["headers"]["X-Zylker-Attempt"] for c in sender.calls], ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(self.run_due(sender, t + timedelta(days=1))["failed"], 0)
        hook = self.get_hook(self.h["id"])
        self.assertEqual((hook["active"], hook["failure_streak"], hook["last_error"]), (True, 6, "http_500"))

    def test_a_network_error_is_recorded_as_its_code(self):
        self.make_task("WEB", "First")
        self.run_due(FakeSender(SendResult(None, "timeout", 10000)))
        d = self.only_delivery()
        self.assertEqual((d["status"], d["error"], d["response_status"]), ("pending", "timeout", None))
        self.assertEqual(self.get_hook(self.h["id"])["last_error"], "timeout")

    def test_recovery_resets_the_failure_streak(self):
        self.make_task("WEB", "First")
        self.run_due(FakeSender(SendResult(502, None, 1)))
        self.assertEqual(self.get_hook(self.h["id"])["failure_streak"], 1)
        self.run_due(FakeSender(), self.now + timedelta(seconds=60))
        hook = self.get_hook(self.h["id"])
        self.assertEqual((hook["failure_streak"], hook["last_error"], self.only_delivery()["status"]), (0, None, "delivered"))

    def test_a_receiver_that_keeps_failing_is_switched_off_and_the_admin_is_told(self):
        self.make_task("WEB", "First")
        self.make_task("WEB", "Second")
        self.sql("UPDATE webhooks SET failure_streak = 14 WHERE id = ?", self.h["id"])
        report = self.run_due(FakeSender(SendResult(500, None, 1), SendResult(500, None, 1)))
        hook = self.get_hook(self.h["id"])
        self.assertEqual((hook["active"], hook["disabled_reason"], hook["disabled_at"]), (False, "too_many_failures", "2026-10-16T09:00:00Z"))
        self.assertEqual([x["reason"] for x in report["disabled"]], ["too_many_failures"])
        statuses = {d["status"] for d in self.deliveries(self.h["id"])}
        self.assertTrue(statuses <= {"cancelled", "failed"}, statuses)  # nothing is left queued to hammer a dead receiver
        [event] = self.ok(self.admin.get("/api/audit", query={"action": "webhook.disabled"}))["items"]
        self.assertEqual((event["severity"], event["actor"], event["details"]["reason"], event["details"]["name"]), ("warning", None, "too_many_failures", "Zylker hook"))
        later = FakeSender()
        self.run_due(later, self.now + timedelta(days=1))
        self.assertEqual(later.calls, [])

    def test_410_gone_switches_it_off_at_once(self):
        self.make_task("WEB", "First")
        self.run_due(FakeSender(SendResult(410, None, 1)))
        hook = self.get_hook(self.h["id"])
        self.assertEqual((hook["active"], hook["disabled_reason"]), (False, "gone"))
        self.assertEqual(self.only_delivery()["status"], "failed")

    def test_other_client_errors_are_retried_like_any_failure(self):
        self.make_task("WEB", "First")
        self.run_due(FakeSender(SendResult(404, None, 1)))
        self.assertEqual((self.only_delivery()["status"], self.get_hook(self.h["id"])["active"]), ("pending", True))

    def test_a_delivery_to_a_switched_off_webhook_is_cancelled_not_sent(self):
        self.make_task("WEB", "First")
        self.sql("UPDATE webhooks SET active = 0 WHERE id = ?", self.h["id"])
        sender = FakeSender()
        report = self.run_due(sender)
        self.assertEqual((sender.calls, report["cancelled"], self.only_delivery()["status"]), ([], 1, "cancelled"))

    def test_a_crashed_worker_does_not_strand_a_delivery(self):
        self.make_task("WEB", "First")
        d = self.only_delivery()
        self.sql("UPDATE webhook_deliveries SET status = 'sending', attempts = 1, claimed_at = ? WHERE id = ?", "2026-10-16T08:58:00Z", d["id"])
        sender = FakeSender()
        self.run_due(sender)
        self.assertEqual(sender.calls, [])  # claimed two minutes ago: still presumed in flight
        self.run_due(sender, self.now + timedelta(minutes=10))
        self.assertEqual(len(sender.calls), 1)
        done = self.only_delivery()
        self.assertEqual((done["status"], done["attempts"]), ("delivered", 2))

    def test_deleting_a_webhook_while_it_is_being_called_is_harmless(self):
        self.make_task("WEB", "First")

        def vanish(url):
            self.sql("DELETE FROM webhooks WHERE id = ?", self.h["id"])
            return SendResult(200, None, 1)

        report = self.run_due(FakeSender(vanish))
        self.assertEqual(report["delivered"], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM webhook_deliveries")[0][0], 0)

    def test_old_finished_deliveries_are_pruned_and_unfinished_ones_are_kept(self):
        self.make_task("WEB", "First")
        self.make_task("WEB", "Second")
        self.run_due(FakeSender(SendResult(200, None, 1), SendResult(500, None, 1)), workers=1)  # one delivered, one waiting to retry
        self.assertEqual(self.run_due(FakeSender(SendResult(500, None, 1)), self.now + timedelta(days=29))["pruned"], 0)
        report = self.run_due(FakeSender(SendResult(500, None, 1)), self.now + timedelta(days=31))
        self.assertEqual(report["pruned"], 1)  # the day-0 delivery; the one still retrying is not finished, so it stays
        self.assertEqual([d["status"] for d in self.deliveries(self.h["id"])], ["pending"])

    def test_many_workers_never_deliver_the_same_event_twice(self):
        self.log_events(40)
        sender = FakeSender()

        def work():
            for _ in range(30):
                self.run_due(sender, limit=7, workers=2)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = [c["headers"]["X-Zylker-Delivery"] for c in sender.calls]
        self.assertEqual(len(ids), 40)
        self.assertEqual(len(set(ids)), 40)
        self.assertEqual({d["status"] for d in self.deliveries(self.h["id"], limit=100)}, {"delivered"})

    def test_a_batch_is_sent_in_parallel_without_losing_results(self):
        self.log_events(8)
        gate = threading.Barrier(4, timeout=5)

        def rendezvous(url):
            gate.wait()  # only passes if four sends really are in flight at once
            return SendResult(200, None, 1)

        report = self.run_due(FakeSender(*[rendezvous] * 4), limit=4, workers=4)
        self.assertEqual(report["delivered"], 4)

    def test_test_and_redeliver(self):
        ping = self.ok(self.admin.post("/api/webhooks/%d/test" % self.h["id"]), 202)
        self.assertEqual((ping["event"], ping["status"], ping["event_id"]), ("webhook.ping", "pending", None))
        sender = FakeSender()
        self.run_due(sender)
        self.assertEqual(sender.calls[0]["headers"]["X-Zylker-Event"], "webhook.ping")  # not subscribed to it, delivered anyway
        self.assertEqual(sender.calls[0]["json"]["actor"]["username"], "admin")
        again = self.ok(self.admin.post("/api/webhooks/%d/deliveries/%d/redeliver" % (self.h["id"], ping["id"])), 202)
        self.assertEqual((again["redelivery_of"], again["status"], again["attempts"]), (ping["id"], "pending", 0))
        self.assertNotEqual(again["id"], ping["id"])
        self.run_due(sender)
        self.assertEqual(len(sender.calls), 2)
        self.assertEqual(sender.calls[1]["json"]["delivery_id"], again["id"])
        self.assertEqual(self.detail(self.h["id"], ping["id"])["status"], "delivered")  # the original stays as it was

    def test_testing_or_redelivering_needs_an_active_webhook(self):
        d = self.ok(self.admin.post("/api/webhooks/%d/test" % self.h["id"]), 202)
        self.admin.patch("/api/webhooks/%d" % self.h["id"], {"active": False})
        for path in ("/api/webhooks/%d/test" % self.h["id"], "/api/webhooks/%d/deliveries/%d/redeliver" % (self.h["id"], d["id"])):
            r = self.admin.post(path)
            self.assertEqual((r.status, r.error_code), (409, "webhook_inactive"))

    def test_another_webhooks_deliveries_are_not_reachable_through_this_one(self):
        self.make_task("WEB", "First")
        [d] = self.deliveries(self.h["id"])
        other = self.hook(name="other", events=["milestone.*"])
        self.assertEqual(self.admin.get("/api/webhooks/%d/deliveries/%d" % (other["id"], d["id"])).status, 404)
        self.assertEqual(self.admin.post("/api/webhooks/%d/deliveries/%d/redeliver" % (other["id"], d["id"])).status, 404)

    def test_the_delivery_log_can_be_filtered_and_paged(self):
        self.log_events(7)
        self.log_events(1, action="task.deleted")
        self.run_due(FakeSender(*[SendResult(200, None, 1)] * 3 + [SendResult(500, None, 1)] * 5), limit=8)
        h = self.h["id"]
        self.assertEqual(len(self.deliveries(h, limit=100)), 8)
        self.assertEqual(len(self.deliveries(h, status="delivered")), 3)
        self.assertEqual(len(self.deliveries(h, status="pending,failed")), 5)
        self.assertEqual([d["event"] for d in self.deliveries(h, event="task.deleted")], ["task.deleted"])
        first = self.ok(self.admin.get("/api/webhooks/%d/deliveries" % h, query={"limit": 3}))
        self.assertEqual(len(first["items"]), 3)
        second = self.ok(self.admin.get("/api/webhooks/%d/deliveries" % h, query={"limit": 3, "before": first["next_before"]}))
        self.assertEqual([d["id"] for d in second["items"]], [d["id"] for d in self.deliveries(h, limit=100)][3:6])
        for query in ({"status": "bogus"}, {"limit": "0"}, {"limit": "101"}, {"limit": "x"}, {"before": "x"}, {"event": "Not An Event"}):
            self.assertEqual(self.admin.get("/api/webhooks/%d/deliveries" % h, query=query).status, 422, query)


class EndToEndTests(WebhookCase):
    """Real sockets: the app, the worker, the network layer and a receiver that verifies the signature."""

    def setUp(self):
        super().setUp()
        self.server = HookServer()
        self.addCleanup(self.server.close)
        self.config = dataclasses.replace(self.config, webhook_allow_private=True)
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.admin = self.client("admin")
        self.sender = netguard.make_sender(allow_private=True)

    def test_a_real_receiver_gets_a_signed_event_it_can_verify(self):
        h = self.hook(url=self.server.url, events=["task.created"])
        self.make_project("WEB")
        self.make_task("WEB", "Over the wire")
        report = self.run_due(self.sender)
        self.assertEqual(report["delivered"], 1)
        [req] = self.server.requests
        hdr = req["headers"]
        self.assertTrue(webhooks.verify_signature(h["secret"], hdr["x-zylker-timestamp"], hdr["x-zylker-signature"], req["body"], now=self.now.timestamp()))
        body = json.loads(req["body"])
        self.assertEqual((hdr["x-zylker-event"], body["event"], body["task"], body["details"]["title"]), ("task.created", "task.created", "WEB-1", "Over the wire"))
        self.assertEqual(hdr["content-type"], "application/json")

    def test_a_receiver_that_errors_then_recovers_gets_the_same_delivery_again(self):
        self.hook(url=self.server.url, events=["task.created"])
        self.make_project("WEB")
        self.make_task("WEB", "Retry me")
        self.server.status = 500
        self.assertEqual(self.run_due(self.sender)["retrying"], 1)
        self.server.status = 200
        self.assertEqual(self.run_due(self.sender, self.now + timedelta(seconds=60))["delivered"], 1)
        first, second = self.server.requests
        self.assertEqual((first["headers"]["x-zylker-delivery"], first["headers"]["x-zylker-attempt"]), (second["headers"]["x-zylker-delivery"], "1"))
        self.assertEqual(second["headers"]["x-zylker-attempt"], "2")

    def test_a_slow_receiver_does_not_hold_up_the_others(self):
        slow = HookServer(delay=1.0)
        self.addCleanup(slow.close)
        self.hook(name="slow", url=slow.url, events=["task.created"])
        self.hook(name="fast", url=self.server.url, events=["task.created"])
        self.make_project("WEB")
        self.make_task("WEB", "Both")
        started = time.monotonic()
        report = self.run_due(self.sender, workers=4)
        self.assertEqual(report["delivered"], 2)
        self.assertLess(time.monotonic() - started, 1.9)  # in parallel: about one second, not two

    def test_the_default_configuration_never_calls_an_internal_address_even_if_one_gets_in(self):
        h = self.hook(url=self.server.url, events=["task.created"])
        self.make_project("WEB")
        self.make_task("WEB", "Sneaky")
        strict = netguard.make_sender(allow_private=False)
        self.run_due(strict)
        self.assertEqual(self.server.requests, [])
        [d] = self.deliveries(h["id"])
        self.assertEqual((d["status"], d["error"]), ("pending", "invalid_url"))

    def test_the_command_delivers_the_queue(self):
        self.hook(url=self.server.url, events=["task.created"])
        self.make_project("WEB")
        self.make_task("WEB", "From the CLI")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZYLKER_")}
        env["ZYLKER_WEBHOOK_ALLOW_PRIVATE"] = "1"
        out = io.StringIO()
        real_now = webhooks.run_due
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            with mock.patch.object(webhooks, "run_due", lambda path, now, send, **kw: real_now(path, self.now, send, **kw)):
                code = cli.main(["--db", self.config.db_path, "webhook-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["delivered"], 1)
        self.assertEqual(len(self.server.requests), 1)

    def test_the_background_loop_delivers_and_survives_a_failing_pass(self):
        h = self.hook(url=self.server.url, events=["task.created"])
        self.make_project("WEB")
        self.make_task("WEB", "In the background")
        passes = {"n": 0}
        real = webhooks.run_due

        def flaky(path, now, send, **kw):
            passes["n"] += 1
            if passes["n"] == 1:
                raise RuntimeError("database is having a moment")
            return real(path, self.now, send, **kw)

        stop = mock.Mock()
        stop.wait.side_effect = [False, False, True]
        with mock.patch.object(webhooks, "run_due", flaky), self.assertLogs("zylker.webhooks", "ERROR"):
            cli._webhook_loop(self.config, self.sender, 2, stop)
        self.assertEqual(passes["n"], 2)
        self.assertEqual([d["status"] for d in self.deliveries(h["id"])], ["delivered"])


class OperationsTests(WebhookCase):
    def v4_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        for version in (1, 2, 3):
            db._migrate(conn, version)
        conn.close()

    def tables(self, path):
        conn = sqlite3.connect(path)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
            return names, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_a_v4_database_upgrades_and_matches_a_fresh_one(self):
        old, fresh = os.path.join(self.tmp, "v4.db"), os.path.join(self.tmp, "fresh5.db")
        self.v4_database(old)
        self.assertNotIn("webhooks", self.tables(old)[0])
        init_db(old)
        init_db(fresh)
        self.assertEqual(self.tables(old), self.tables(fresh))
        self.assertTrue({"webhooks", "webhook_deliveries", "idx_wd_due"} <= self.tables(old)[0])
        self.assertEqual(self.tables(old)[1], db.SCHEMA_VERSION)

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v4b.db")
        self.v4_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE webhook_deliveries (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        names, version = self.tables(path)
        self.assertEqual(version, 4)
        self.assertNotIn("webhooks", names)

    def test_settings(self):
        self.assertEqual((Config().webhook_allow_private, Config().webhook_interval_seconds), (False, 0))
        env = Config.from_env({})
        self.assertEqual((env.webhook_allow_private, env.webhook_interval_seconds), (False, 2))
        for value in ("1", "true", "YES", "on"):
            self.assertTrue(Config.from_env({"ZYLKER_WEBHOOK_ALLOW_PRIVATE": value}).webhook_allow_private, value)
        for value in ("", "0", "no", "off", "maybe"):
            self.assertFalse(Config.from_env({"ZYLKER_WEBHOOK_ALLOW_PRIVATE": value}).webhook_allow_private, value)
        self.assertEqual(Config.from_env({"ZYLKER_WEBHOOK_INTERVAL_SECONDS": "0"}).webhook_interval_seconds, 0)
        for bad in ("-1", "3601", "x", ""):
            with self.assertRaises(ValueError, msg=bad):
                Config.from_env({"ZYLKER_WEBHOOK_INTERVAL_SECONDS": bad})

    def test_webhooks_are_a_category_in_the_audit_log(self):
        self.hook()
        cats = self.ok(self.admin.get("/api/audit/summary"))["by_category"]
        self.assertEqual(next(c["count"] for c in cats if c["category"] == "webhook"), 1)
        self.assertEqual(self.ok(self.admin.get("/api/audit", query={"category": "webhook"}))["total"], 1)

    def test_the_demo_and_existing_features_are_unaffected_when_no_webhook_exists(self):
        self.make_project("WEB")
        self.make_task("WEB", "No hooks")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM webhook_deliveries")[0][0], 0)


if __name__ == "__main__":
    unittest.main()
