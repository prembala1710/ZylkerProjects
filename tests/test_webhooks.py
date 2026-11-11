"""Outbound webhooks: the admin API, the transactional outbox, signing and the delivery worker."""

import json
import os
import sqlite3
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from helpers import FIXED_NOW, Client
from webhook_helpers import FAIL_500, OK, FakeSender, WebhookCase

from zylker_pm import webhook_http
from zylker_pm.app import App
from zylker_pm.config import Config
from zylker_pm.db import SCHEMA_VERSION, connect, init_db
from zylker_pm.services import activity, users, webhooks
from zylker_pm.util import iso
from zylker_pm.webhook_http import SendResult

URL = "https://hooks.example.com/zylker?token=t0k3n"
STAMP = int(FIXED_NOW.timestamp())


def table_names(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def audited_actions():
    """Every action name the code hands to ``activity.log``, read from the source (names held in a variable are
    resolved through the assignments in the same function)."""
    import ast

    def literals(expr):  # "a.b", or the two branches of `"a.b" if cond else "c.d"`
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.IfExp):
            return literals(expr.body) | literals(expr.orelse)
        return set()

    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "zylker_pm")
    found = set()
    for folder, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                assigned = {}
                for node in ast.walk(function):
                    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                        assigned.setdefault(node.targets[0].id, []).append(node.value)
                for node in ast.walk(function):
                    is_log = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "log"
                    if is_log and isinstance(node.func.value, ast.Name) and node.func.value.id == "activity" and len(node.args) >= 2:
                        arg = node.args[1]
                        for expr in assigned.get(arg.id, []) if isinstance(arg, ast.Name) else [arg]:
                            found |= literals(expr)
    return found


class WebhookApiTests(WebhookCase):
    def invalid(self, body, *fields, method="POST", path="/api/webhooks"):
        r = self.admin.request(method, path, body)
        self.assertEqual(r.status, 422, r.text)
        for field in fields:
            self.assertIn(field, r.json["error"]["details"], r.text)
        return r.json["error"]["details"]

    def test_create_returns_the_secret_exactly_once(self):
        self.make_project("WEB")
        body = {"name": " CI bot ", "url": URL, "events": ["task.created", "task.*", "task.created", "comment.added"], "projects": ["web", "WEB"]}
        created = self.ok(self.admin.post("/api/webhooks", body), 201)
        secret = created.pop("secret")
        self.assertTrue(secret.startswith("whsec_") and len(secret) >= 40, secret)
        self.assertEqual(created["secret_hint"], "\u2026" + secret[-4:])
        self.assertEqual(
            {k: created[k] for k in ("name", "url", "events", "projects", "active", "created_by", "pending_deliveries", "created_at")},
            {"name": "CI bot", "url": URL, "events": ["task.created", "task.*", "comment.added"], "projects": ["WEB"], "active": True,
             "created_by": "admin", "pending_deliveries": 0, "created_at": "2026-10-16T09:00:00Z"},
        )  # fmt: skip
        self.assertEqual(created["health"], {"last_delivery_at": None, "last_status": None, "consecutive_failures": 0})
        for response in (self.admin.get("/api/webhooks/%d" % created["id"]), self.admin.get("/api/webhooks")):
            self.assertEqual(response.status, 200)
            self.assertNotIn(secret, response.text)
            self.assertNotIn('"secret"', response.text)
        self.assertNotEqual(self.make_hook()["secret"], secret)

    def test_only_admins_can_use_the_webhook_api(self):
        hook = self.make_hook()
        ping = self.ok(self.admin.post("/api/webhooks/%d/test" % hook["id"]), 202)
        base = "/api/webhooks/%d" % hook["id"]
        calls = [
            ("GET", "/api/webhooks", None), ("POST", "/api/webhooks", {"name": "x", "url": URL, "events": ["*"]}),
            ("GET", "/api/webhooks/events", None), ("GET", base, None), ("PATCH", base, {"name": "x"}), ("DELETE", base, None),
            ("POST", base + "/rotate-secret", {}), ("POST", base + "/test", {}), ("GET", base + "/deliveries", None),
            ("GET", "%s/deliveries/%d" % (base, ping["id"]), None), ("POST", "%s/deliveries/%d/redeliver" % (base, ping["id"]), {}),
        ]  # fmt: skip
        for user in ("alice", "bob", "vera"):
            for method, path, body in calls:
                r = self.client(user).request(method, path, body)
                self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), "%s %s as %s" % (method, path, user))
        for method, path, body in calls:
            self.assertEqual(self.client().request(method, path, body).status, 401, "%s %s without a token" % (method, path))
        after = self.ok(self.admin.get(base))
        self.assertEqual((after["name"], after["updated_at"]), ("Hook", hook["updated_at"]))  # nothing above changed anything
        self.assertEqual(len(self.ok(self.admin.get("/api/webhooks"))["items"]), 1)

    def test_create_validation(self):
        self.make_project("WEB")
        good = {"name": "x", "url": URL, "events": ["*"]}
        self.invalid({}, "name", "url", "events")
        for name in ("", "   ", "n" * 81, 5):
            self.invalid({**good, "name": name}, "name")
        for url in ("", 5, "ftp://example.com/x", "https://", "https://user:pw@example.com/x", "https://exa mple.com/", "javascript:alert(1)",
                    "//example.com/x", "https://example.com/" + "a" * 2000, "http://example.com:99999/"):  # fmt: skip
            self.invalid({**good, "url": url}, "url")
        for events in ([], "task.created", None, ["task.exploded"], ["user.created"], ["webhook.ping"], [7], ["task.*", "nope"], ["TASK.created"]):
            self.invalid({**good, "events": events}, "events")
        for projects in (["NOPE"], "WEB", [1], ["WEB", "NOPE"], {"WEB": 1}):
            self.invalid({**good, "projects": projects}, "projects")
        self.invalid({**good, "active": "yes"}, "active")
        self.invalid({**good, "secret": "chosen-by-caller"}, "secret")  # secrets are always generated server-side
        self.assertEqual(self.admin.post("/api/webhooks", [1]).status, 400)
        self.assertEqual(self.admin.request("POST", "/api/webhooks", raw=b"{nope", content_type="application/json").status, 400)
        self.assertEqual(self.admin.request("POST", "/api/webhooks", raw=b"x", content_type="text/plain").status, 415)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM webhooks")[0]["n"], 0)

    def test_optional_fields_and_normalisation(self):
        self.make_project("WEB")
        hook = self.ok(self.admin.post("/api/webhooks", {"name": "x", "url": URL, "events": ["task.created", "*"], "projects": None}), 201)
        self.assertEqual((hook["events"], hook["projects"], hook["active"]), (["*"], [], True))
        paused = self.ok(self.admin.post("/api/webhooks", {"name": "y", "url": URL, "events": ["task.*"], "active": False}), 201)
        self.assertFalse(paused["active"])

    def test_private_destinations_are_refused_unless_the_operator_allows_them(self):
        self.use_config(webhook_allow_private=False)
        urls = ["http://127.0.0.1/x", "http://localhost:8080/x", "http://[::1]/x", "http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/x",
                "http://192.168.0.1/x", "http://172.16.0.1/x", "http://100.64.0.1/x", "http://0.0.0.0/x", "http://2130706433/x",
                "http://127.1/x", "http://[::ffff:127.0.0.1]/x", "http://intranet.localhost/x"]  # fmt: skip
        for url in urls:
            self.invalid({"name": "x", "url": url, "events": ["*"]}, "url")
        # ordinary names are accepted at save time (no DNS lookup in a request); delivery re-checks what they resolve to
        hook = self.ok(self.admin.post("/api/webhooks", {"name": "x", "url": URL, "events": ["*"]}), 201)
        self.invalid({"url": "http://127.0.0.1/x"}, "url", method="PATCH", path="/api/webhooks/%d" % hook["id"])
        self.use_config(webhook_allow_private=True)
        self.ok(self.admin.post("/api/webhooks", {"name": "x", "url": "http://127.0.0.1:9/x", "events": ["*"]}), 201)

    def test_update(self):
        self.make_project("WEB")
        hook = self.make_hook(events=["task.created"])
        path = "/api/webhooks/%d" % hook["id"]
        self.now += timedelta(minutes=5)
        r = self.ok(self.admin.patch(path, {"name": "Renamed"}))
        self.assertEqual((r["name"], r["events"], r["url"], r["updated_at"]), ("Renamed", ["task.created"], self.receiver.url, "2026-10-16T09:05:00Z"))
        r = self.ok(self.admin.patch(path, {"events": ["comment.*", "task.deleted"], "projects": ["web"], "url": URL, "active": False}))
        self.assertEqual((r["events"], r["projects"], r["url"], r["active"]), (["comment.*", "task.deleted"], ["WEB"], URL, False))
        self.now += timedelta(minutes=5)
        r = self.ok(self.admin.patch(path, {"name": "Renamed", "events": ["comment.*", "task.deleted"], "projects": ["web"], "active": False}))
        self.assertEqual(r["updated_at"], "2026-10-16T09:05:00Z")  # the same values again: no write, no new timestamp
        self.assertEqual(self.ok(self.admin.patch(path, {}))["updated_at"], "2026-10-16T09:05:00Z")
        missing = self.admin.patch("/api/webhooks/999", {"name": "x"})
        self.assertEqual((missing.status, missing.error_code), (404, "webhook_not_found"))
        self.assertEqual(self.admin.get("/api/webhooks/abc").status, 404)
        for body, field in (({"url": "ftp://x"}, "url"), ({"events": []}, "events"), ({"events": None}, "events"), ({"name": ""}, "name"), ({"active": "no"}, "active")):
            self.invalid(body, field, method="PATCH", path=path)

    def test_there_is_a_limit_on_webhooks(self):
        for i in range(webhooks.MAX_WEBHOOKS):
            self.make_hook(name="h%d" % i)
        over = self.admin.post("/api/webhooks", {"name": "one too many", "url": URL, "events": ["*"]})
        self.assertEqual((over.status, over.error_code), (409, "webhook_limit"))
        self.ok(self.admin.delete("/api/webhooks/1"), 204)
        self.ok(self.admin.post("/api/webhooks", {"name": "fits now", "url": URL, "events": ["*"]}), 201)

    def test_rotating_the_secret(self):
        hook = self.make_hook()
        rotated = self.ok(self.admin.post("/api/webhooks/%d/rotate-secret" % hook["id"]))
        self.assertTrue(rotated["secret"].startswith("whsec_"))
        self.assertNotEqual(rotated["secret"], hook["secret"])
        self.assertEqual(rotated["secret_hint"], "\u2026" + rotated["secret"][-4:])
        shown = self.ok(self.admin.get("/api/webhooks/%d" % hook["id"]))
        self.assertNotIn("secret", shown)
        self.assertEqual(shown["secret_hint"], rotated["secret_hint"])
        self.assertEqual(self.admin.post("/api/webhooks/999/rotate-secret").status, 404)

    def test_deleting_removes_the_webhook_and_its_queue(self):
        self.make_project("WEB")
        hook = self.make_hook()
        self.make_task("WEB")
        self.assertEqual(len(self.queue(hook["id"])), 1)
        self.ok(self.admin.delete("/api/webhooks/%d" % hook["id"]), 204)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.admin.get("/api/webhooks/%d" % hook["id"]).status, 404)
        self.assertEqual(self.admin.delete("/api/webhooks/%d" % hook["id"]).status, 404)

    def test_the_audit_trail_records_changes_without_secrets_or_urls(self):
        bystander = self.make_hook(name="bystander")
        hook = self.make_hook(name="Audit me")
        path = "/api/webhooks/%d" % hook["id"]
        self.ok(self.admin.patch(path, {"name": "Audit me too"}))
        self.ok(self.admin.post(path + "/rotate-secret"))
        self.ok(self.admin.delete(path), 204)
        items = self.ok(self.admin.get("/api/activity"))["items"]
        wanted = [i for i in items if i["action"].startswith("webhook.")]
        self.assertEqual([i["action"] for i in wanted], ["webhook.deleted", "webhook.secret_rotated", "webhook.updated", "webhook.created", "webhook.created"])
        self.assertEqual(wanted[2]["details"], {"id": hook["id"], "name": "Audit me too", "fields": ["name"]})
        dump = json.dumps(items)
        for sensitive in ("whsec_", hook["secret"], self.receiver.url, "key=abc"):
            self.assertNotIn(sensitive, dump)
        self.assertEqual(self.queue(bystander["id"]), [])  # even a "*" webhook is never told about webhook management

    def test_event_catalog(self):
        catalog = self.ok(self.admin.get("/api/webhooks/events"))
        self.assertIn("task.status_changed", catalog["events"])
        self.assertEqual((catalog["ping"], catalog["never_delivered"]), ("webhook.ping", ["user.*", "webhook.*"]))
        self.assertEqual(catalog["groups"]["comment"], ["comment.added", "comment.edited", "comment.deleted"])
        for pattern in catalog["patterns"]:  # everything the catalogue advertises can actually be subscribed to
            made = self.ok(self.admin.post("/api/webhooks", {"name": pattern, "url": URL, "events": [pattern]}), 201)
            self.ok(self.admin.delete("/api/webhooks/%d" % made["id"]), 204)  # (there are more patterns than the server allows webhooks)

    def test_the_catalogue_covers_every_audited_action(self):
        """Fails when a feature starts logging an audit action that nobody added to ``webhooks.EVENT_GROUPS``.

        Such an action is still delivered to ``*`` (and, if its group is catalogued, ``<group>.*``) subscriptions,
        but it cannot be picked by name or group in the UI until it is catalogued - and documented."""
        audited = audited_actions()
        self.assertTrue({"task.created", "project.created", "user.created", "webhook.created"} <= audited, "the source scan itself is broken")
        known = set(webhooks.EVENTS)
        unknown = sorted(a for a in audited if a not in known and a.split(".")[0] not in webhooks.NEVER_DELIVERED)
        self.assertEqual(unknown, [], "add these audit actions to EVENT_GROUPS in services/webhooks.py and to the events table in docs/API.md")
        self.assertEqual(sorted(known - audited), [], "catalogued events that no code logs any more")


class OutboxTests(WebhookCase):
    def test_a_task_event_is_queued_with_a_complete_payload(self):
        self.use_config(public_url="https://pm.example.com")
        self.make_project("WEB")
        a = self.make_hook(name="A", events=["task.created"])
        b = self.make_hook(name="B", events=["task.*"])
        c = self.make_hook(name="C", events=["comment.*"])
        self.make_task("WEB", "Ship it", assignee="bob", priority="high", due_date="2026-10-30")
        self.assertEqual((self.events_queued(a["id"]), self.events_queued(b["id"]), self.events_queued(c["id"])), (["task.created"], ["task.created"], []))
        row = self.queue(a["id"])[0]
        activity_id = self.rows("SELECT id FROM activity WHERE action = 'task.created'")[0]["id"]
        self.assertEqual(
            json.loads(row["payload"]),
            {
                "id": "evt_%d" % activity_id,
                "event": "task.created",
                "created_at": "2026-10-16T09:00:00Z",
                "project": {"key": "WEB", "name": "Project WEB", "url": "https://pm.example.com/#/p/WEB"},
                "task": {"key": "WEB-1", "title": "Ship it", "status": "todo", "priority": "high", "type": "task", "assignee": "bob",
                         "due_date": "2026-10-30", "url": "https://pm.example.com/#/t/WEB-1"},  # fmt: skip
                "actor": {"username": "alice", "display_name": "Alice"},
                "details": {"title": "Ship it", "status": "todo", "assignee": "bob"},
            },
        )
        self.assertEqual(
            (row["status"], row["attempts"], row["max_attempts"], row["next_attempt_at"], row["event_id"], row["redelivery_of"]),
            ("pending", 0, 6, "2026-10-16T09:00:00Z", "evt_%d" % activity_id, None),
        )
        self.assertEqual(row["payload"], self.queue(b["id"])[0]["payload"])  # every subscriber is told exactly the same thing

    def test_links_are_left_out_without_a_public_url(self):
        self.make_project("WEB")
        hook = self.make_hook()
        self.make_task("WEB")
        payload = json.loads(self.queue(hook["id"])[0]["payload"])
        self.assertNotIn("url", payload["task"])
        self.assertNotIn("url", payload["project"])

    def test_the_payload_shows_the_state_after_the_change(self):
        self.make_project("WEB")
        self.make_task("WEB", "Work")
        hook = self.make_hook(events=["task.status_changed"])
        self.ok(self.set_status("WEB-1", "in_progress"))
        payload = json.loads(self.queue(hook["id"])[0]["payload"])
        self.assertEqual(payload["event"], "task.status_changed")
        self.assertEqual(payload["details"], {"changes": {"status": ["todo", "in_progress"]}})
        self.assertEqual(payload["task"]["status"], "in_progress")

    def test_subscription_patterns(self):
        self.make_project("WEB")
        self.make_task("WEB", "t")
        exact = self.make_hook(name="exact", events=["task.status_changed"])
        group = self.make_hook(name="group", events=["comment.*", "milestone.created"])
        everything = self.make_hook(name="all", events=["*"])
        self.ok(self.set_status("WEB-1", "in_progress"))
        self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "hello"}), 201)
        self.ok(self.client("bob").post("/api/tasks/WEB-1/time", {"hours": 1.5}), 201)
        self.assertEqual(self.events_queued(exact["id"]), ["task.status_changed"])
        self.assertEqual(self.events_queued(group["id"]), ["comment.added"])
        self.assertEqual(self.events_queued(everything["id"]), ["task.status_changed", "comment.added", "time.logged"])

    def test_project_scope(self):
        self.make_project("WEB")
        self.make_project("OPS")
        scoped = self.make_hook(name="web only", projects=["WEB"])
        everywhere = self.make_hook(name="everywhere")
        self.make_task("WEB", "a")
        self.make_task("OPS", "b")
        keys = lambda hook: [json.loads(r["payload"])["project"]["key"] for r in self.queue(hook["id"])]  # noqa: E731
        self.assertEqual((keys(scoped), keys(everywhere)), (["WEB"], ["WEB", "OPS"]))

    def test_a_deleted_project_is_still_announced_to_hooks_scoped_to_it(self):
        self.use_config(public_url="https://pm.example.com")
        self.make_project("WEB")
        self.make_project("OPS")
        web = self.make_hook(name="web", projects=["WEB"], events=["project.deleted"])
        ops = self.make_hook(name="ops", projects=["OPS"], events=["project.deleted"])
        self.ok(self.admin.delete("/api/projects/WEB", query={"confirm": "WEB"}), 204)
        self.assertEqual((self.events_queued(web["id"]), self.events_queued(ops["id"])), (["project.deleted"], []))
        payload = json.loads(self.queue(web["id"])[0]["payload"])
        self.assertEqual(payload["project"], {"key": "WEB", "name": "Project WEB"})  # and no link to a project that is gone
        self.assertEqual(payload["details"], {"key": "WEB", "name": "Project WEB"})

    def test_a_deleted_task_keeps_its_title(self):
        self.use_config(public_url="https://pm.example.com")
        self.make_project("WEB")
        self.make_task("WEB", "Doomed")
        hook = self.make_hook(events=["task.deleted"])
        self.ok(self.client("alice").delete("/api/tasks/WEB-1"), 204)
        payload = json.loads(self.queue(hook["id"])[0]["payload"])
        self.assertEqual(payload["task"], {"key": "WEB-1", "title": "Doomed"})  # nothing left to link to or to describe
        self.assertEqual(payload["details"], {"title": "Doomed", "subtasks_deleted": 0})

    def test_comment_text_never_leaves_in_an_event(self):
        self.make_project("WEB")
        self.make_task("WEB")
        hook = self.make_hook(events=["comment.*"])
        self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "my private words for @carol"}), 201)
        stored = self.queue(hook["id"])[0]["payload"]
        self.assertNotIn("private words", stored)
        self.assertEqual(json.loads(stored)["details"]["mentions"], ["carol"])

    def test_inactive_webhooks_receive_nothing(self):
        self.make_project("WEB")
        hook = self.make_hook(active=False)
        self.make_task("WEB", "one")
        self.assertEqual(self.queue(), [])
        self.ok(self.admin.patch("/api/webhooks/%d" % hook["id"], {"active": True}))
        self.make_task("WEB", "two")
        self.assertEqual(self.events_queued(), ["task.created"])

    def test_pausing_cancels_the_backlog_but_not_a_pending_test_ping(self):
        self.make_project("WEB")
        hook = self.make_hook()
        path = "/api/webhooks/%d" % hook["id"]
        self.make_task("WEB", "one")
        self.make_task("WEB", "two")
        self.ok(self.admin.post(path + "/test"), 202)
        self.assertEqual(self.ok(self.admin.get(path))["pending_deliveries"], 3)
        self.ok(self.admin.patch(path, {"active": False}))
        states = [(r["event"], r["status"], r["error"]) for r in self.queue()]
        self.assertEqual(states, [("task.created", "failed", "cancelled")] * 2 + [("webhook.ping", "pending", None)])
        self.ok(self.admin.patch(path, {"active": True}))
        self.assertEqual([r["status"] for r in self.queue()], ["failed", "failed", "pending"])  # resuming does not resurrect old events
        self.assertEqual(self.ok(self.admin.get(path))["pending_deliveries"], 1)

    def test_user_and_webhook_administration_is_never_delivered(self):
        hook = self.make_hook(events=["*"])
        dave = self.make_user("dave", "member")
        self.ok(self.admin.patch("/api/users/%d" % dave["id"], {"role": "manager"}))
        self.ok(self.admin.post("/api/users/%d/rotate-token" % dave["id"]))
        self.ok(self.admin.patch("/api/webhooks/%d" % hook["id"], {"name": "renamed"}))
        logged = {r["action"] for r in self.rows("SELECT action FROM activity")}
        self.assertTrue({"user.created", "user.updated", "user.token_rotated", "webhook.updated"} <= logged)
        self.assertEqual(self.queue(), [])

    def test_a_csv_import_is_one_event_not_one_per_task(self):
        self.make_project("WEB", members=("bob",))
        hook = self.make_hook()
        r = self.client("alice").request("POST", "/api/projects/WEB/import", raw=b"title,status\nOne,todo\nTwo,done\nThree,todo\n", content_type="text/csv")
        self.assertEqual(self.ok(r)["created"], 3)
        self.assertEqual(self.events_queued(hook["id"]), ["import.completed"])
        self.assertEqual(json.loads(self.queue()[0]["payload"])["details"], {"created": 3, "skipped_duplicates": 0, "errors": 0})

    def test_events_of_a_rolled_back_request_are_never_queued(self):
        self.make_project("WEB")
        self.make_hook()
        project_id = self.rows("SELECT id FROM projects")[0]["id"]
        with self.assertRaises(RuntimeError), self.app.session() as ctx:
            activity.log(ctx, "task.created", project_id=project_id, task_id=None, task_key="WEB-9", details={"title": "x"})
            self.assertEqual(ctx.conn.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0], 1)  # queued inside the transaction...
            raise RuntimeError("the request failed after logging")
        self.assertEqual(self.queue(), [])  # ...and gone with it
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM activity WHERE task_key = 'WEB-9'")[0]["n"], 0)
        with self.app.session() as ctx:
            activity.log(ctx, "task.created", project_id=project_id, task_id=None, task_key="WEB-9", details={"title": "x"})
        self.assertEqual(self.events_queued(), ["task.created"])

    def test_a_runaway_backlog_is_capped_without_failing_requests(self):
        self.make_project("WEB")
        self.make_hook()
        webhooks._last_warned.clear()
        with mock.patch.object(webhooks, "MAX_PENDING", 2), self.assertLogs("zylker.webhooks", "WARNING") as logs:
            for i in range(4):
                self.make_task("WEB", "t%d" % i)
        self.assertEqual(len(logs.records), 1, "one warning, not one per dropped event")
        self.assertEqual(len(self.queue()), 2)
        self.assertEqual(len(self.rows("SELECT id FROM tasks")), 4)

    def test_missing_webhook_tables_never_break_ordinary_work(self):
        self.make_hook()
        conn = connect(self.config.db_path)
        conn.executescript("DROP TABLE webhook_deliveries; DROP TABLE webhooks; DROP TABLE webhook_state;")
        conn.close()
        webhooks._last_warned.clear()
        with self.assertLogs("zylker.webhooks", "WARNING"):
            self.make_project("WEB")  # e.g. a backup from before webhooks existed was restored under a running server
        self.make_task("WEB")
        r = self.admin.get("/api/webhooks")
        self.assertEqual((r.status, r.error_code), (409, "webhooks_not_initialised"))
        self.use_config()  # starting the app again creates the tables
        self.assertEqual(self.ok(self.admin.get("/api/webhooks"))["items"], [])


class SchemaTests(WebhookCase):
    def test_the_tables_are_additive_and_leave_the_schema_version_alone(self):
        version = lambda: self.rows("PRAGMA user_version")[0]["user_version"]  # noqa: E731
        self.assertEqual(version(), SCHEMA_VERSION)
        webhooks.ensure_schema(self.config.db_path)
        webhooks.ensure_schema(self.config.db_path)
        self.assertEqual(version(), SCHEMA_VERSION)
        names = {r["name"] for r in self.rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"webhooks", "webhook_deliveries", "webhook_state"} <= names)

    def test_a_database_from_before_webhooks_gains_the_tables_when_the_app_starts(self):
        old = os.path.join(self.tmp, "old.db")
        init_db(old)
        self.assertNotIn("webhooks", table_names(old))
        app = App(Config(db_path=old))
        self.assertTrue({"webhooks", "webhook_deliveries", "webhook_state"} <= table_names(old))
        with app.session() as ctx:
            _, token = users.create_user(ctx, {"username": "root", "display_name": "Root", "role": "admin"})
        self.assertEqual(self.ok(Client(app, token).get("/api/webhooks"))["items"], [])

    def test_missing_in_memory_and_uninitialised_files_are_left_alone(self):
        missing = os.path.join(self.tmp, "missing.db")
        webhooks.ensure_schema(missing)
        self.assertFalse(os.path.exists(missing))
        webhooks.ensure_schema(":memory:")
        empty = os.path.join(self.tmp, "empty.db")
        open(empty, "wb").close()
        webhooks.ensure_schema(empty)  # not a Zylker database yet: `init` must still be able to create the real schema
        self.assertEqual(table_names(empty), set())


class SigningTests(unittest.TestCase):
    SECRET, BODY, T0 = "whsec_test", b'{"a":1}', 1700000000
    KNOWN = "v1=38877139021993b830af32feea6e18a8da83eb2f6e49ee50bd9e4cf4ca4d3789"  # printf '1700000000.{"a":1}' | openssl dgst -sha256 -hmac whsec_test

    def test_matches_an_independent_implementation(self):
        self.assertEqual(webhooks.sign(self.SECRET, self.T0, self.BODY), self.KNOWN)
        self.assertEqual(webhooks.sign(self.SECRET, str(self.T0), self.BODY), self.KNOWN)

    def test_verification(self):
        good = self.KNOWN
        check = lambda **kw: webhooks.verify(**{"secret": self.SECRET, "timestamp": self.T0, "body": self.BODY, "signature": good, "now": self.T0, **kw})  # noqa: E731
        self.assertTrue(check(now=self.T0 + 10))
        self.assertTrue(check(timestamp=str(self.T0)))  # headers arrive as strings
        self.assertFalse(check(secret="whsec_other"))
        self.assertFalse(check(body=b'{"a":2}'))
        self.assertFalse(check(timestamp=self.T0 + 1))
        self.assertFalse(check(now=self.T0 + 301))  # a captured request cannot be replayed later
        self.assertFalse(check(now=self.T0 - 301))
        self.assertTrue(check(now=self.T0 + 301, tolerance=600))
        for junk in (None, "", "v1=", "sha256=" + good[3:], good[3:], "v1=zz", "v2=" + good[3:]):
            self.assertFalse(check(signature=junk), junk)
        for junk in (None, "", "abc", "1.5"):
            self.assertFalse(check(timestamp=junk), junk)
        self.assertTrue(check(signature="v1=0000, " + good))  # several signatures: any one may match

    def test_secrets_are_long_and_unique(self):
        secrets_ = {webhooks.new_secret() for _ in range(50)}
        self.assertEqual(len(secrets_), 50)
        self.assertTrue(all(s.startswith("whsec_") and len(s) >= 40 for s in secrets_))


class WorkerTests(WebhookCase):
    def one_delivery(self, **hook_fields):
        self.make_project("WEB")
        hook = self.make_hook(**hook_fields)
        self.make_task("WEB", "t")
        return hook

    def test_a_successful_delivery_is_signed_and_logged(self):
        hook = self.one_delivery()
        sender = FakeSender(SendResult(True, 204, None, "", 12))
        self.assertEqual(self.worker(sender).run_once(), 1)
        d = self.queue()[0]
        self.assertEqual(
            (d["status"], d["attempts"], d["response_status"], d["error"], d["duration_ms"], d["next_attempt_at"], d["lease_until"], d["last_attempt_at"]),
            ("succeeded", 1, 204, None, 12, None, None, "2026-10-16T09:00:00Z"),
        )
        call = sender.calls[0]
        self.assertEqual((call["url"], call["allow_private"], call["timeout"]), (self.receiver.url, True, self.config.webhook_timeout))
        self.assertEqual(call["body"], d["payload"].encode("utf-8"))
        h = call["headers"]
        self.assertEqual((h["X-Zylker-Event"], h["X-Zylker-Delivery"], h["X-Zylker-Attempt"], h["Content-Type"]), ("task.created", str(d["id"]), "1", "application/json"))
        self.assertEqual(h["X-Zylker-Timestamp"], str(STAMP))
        self.assertTrue(h["User-Agent"].startswith("ZylkerProjects/"))
        self.assertTrue(webhooks.verify(hook["secret"], h["X-Zylker-Timestamp"], call["body"], h["X-Zylker-Signature"], now=STAMP))
        health = self.ok(self.admin.get("/api/webhooks/%d" % hook["id"]))["health"]
        self.assertEqual(health, {"last_delivery_at": "2026-10-16T09:00:00Z", "last_status": "succeeded", "consecutive_failures": 0})
        self.assertEqual(self.worker(sender).run_once(), 0)
        self.assertEqual(len(sender.calls), 1)

    def test_retry_schedule_and_final_failure(self):
        hook = self.one_delivery()
        sender = FakeSender(FAIL_500)
        worker = self.worker(sender)
        started = self.now
        for attempt, gap in enumerate(webhooks.RETRY_DELAYS, start=1):
            self.assertEqual(worker.run_once(), 1)
            d = self.queue()[0]
            self.assertEqual((d["status"], d["attempts"], d["error"], d["response_status"]), ("pending", attempt, "http_500", 500))
            self.assertEqual(d["next_attempt_at"], iso(self.now + timedelta(seconds=gap)))
            self.now += timedelta(seconds=gap - 1)
            self.assertEqual(worker.run_once(), 0, "not due one second early")
            self.now += timedelta(seconds=1)
        self.assertEqual(webhooks.RETRY_DELAYS, (30, 120, 600, 3600, 21600))
        self.assertEqual(worker.run_once(), 1)
        d = self.queue()[0]
        self.assertEqual((d["status"], d["attempts"], d["next_attempt_at"], d["lease_until"]), ("failed", 6, None, None))
        self.assertEqual(worker.run_once(), 0)
        self.assertEqual([c["headers"]["X-Zylker-Attempt"] for c in sender.calls], ["1", "2", "3", "4", "5", "6"])
        self.assertEqual((self.now - started).total_seconds(), sum(webhooks.RETRY_DELAYS))
        health = self.ok(self.admin.get("/api/webhooks/%d" % hook["id"]))["health"]
        self.assertEqual((health["last_status"], health["consecutive_failures"]), ("failed", 6))

    def test_a_late_success_ends_the_streak(self):
        hook = self.one_delivery()
        worker = self.worker(FakeSender(FAIL_500, FAIL_500, OK))
        worker.run_once()
        self.now += timedelta(seconds=30)
        worker.run_once()
        self.assertEqual(self.ok(self.admin.get("/api/webhooks/%d" % hook["id"]))["health"]["consecutive_failures"], 2)
        self.now += timedelta(seconds=120)
        worker.run_once()
        d = self.queue()[0]
        self.assertEqual((d["status"], d["attempts"], d["error"], d["response_status"]), ("succeeded", 3, None, 200))
        health = self.ok(self.admin.get("/api/webhooks/%d" % hook["id"]))["health"]
        self.assertEqual((health["last_status"], health["consecutive_failures"]), ("succeeded", 0))

    def test_every_kind_of_failure_is_retried(self):
        self.one_delivery()
        failures = [(404, "http_404"), (302, "http_302"), (None, "timeout"), (None, "blocked_address"), (None, "dns_error")]
        worker = self.worker(FakeSender(*[SendResult(False, s, e, "", 1) for s, e in failures]))
        for attempt, (status, error) in enumerate(failures, start=1):
            worker.run_once()
            d = self.queue()[0]
            self.assertEqual((d["status"], d["attempts"], d["response_status"], d["error"]), ("pending", attempt, status, error))
            self.now += timedelta(days=1)

    def test_a_sender_crash_is_a_failed_attempt_not_a_dead_worker(self):
        self.one_delivery()
        worker = self.worker(FakeSender(RuntimeError("boom"), OK))
        with self.assertLogs("zylker.webhooks", "ERROR"):
            self.assertEqual(worker.run_once(), 1)
        self.assertEqual((self.queue()[0]["status"], self.queue()[0]["error"]), ("pending", "internal_error"))
        self.now += timedelta(seconds=30)
        worker.run_once()
        self.assertEqual(self.queue()[0]["status"], "succeeded")

    def test_the_rotated_secret_signs_the_next_attempt(self):
        hook = self.one_delivery()
        sender = FakeSender(FAIL_500, OK)
        worker = self.worker(sender)
        worker.run_once()
        new = self.ok(self.admin.post("/api/webhooks/%d/rotate-secret" % hook["id"]))["secret"]
        self.now += timedelta(seconds=30)
        worker.run_once()
        first, second = sender.calls
        sig = lambda call: (call["headers"]["X-Zylker-Timestamp"], call["body"], call["headers"]["X-Zylker-Signature"])  # noqa: E731
        t1, t2 = int(first["headers"]["X-Zylker-Timestamp"]), int(second["headers"]["X-Zylker-Timestamp"])
        self.assertTrue(webhooks.verify(hook["secret"], *sig(first), now=t1))
        self.assertFalse(webhooks.verify(new, *sig(first), now=t1))
        self.assertTrue(webhooks.verify(new, *sig(second), now=t2))
        self.assertFalse(webhooks.verify(hook["secret"], *sig(second), now=t2))
        self.assertEqual(t2 - t1, 30)  # every attempt carries a fresh timestamp, so the signature differs although the body does not
        self.assertEqual(first["body"], second["body"])

    def test_a_lease_that_ran_out_is_picked_up_again(self):
        self.one_delivery()
        lease_end = iso(self.now + timedelta(seconds=60))
        conn = connect(self.config.db_path)
        conn.execute("UPDATE webhook_deliveries SET status = 'sending', attempts = 1, lease_until = ?", (lease_end,))
        conn.close()  # a worker claimed it and died
        sender = FakeSender()
        worker = self.worker(sender)
        self.assertEqual(worker.run_once(), 0)  # still leased
        self.now += timedelta(seconds=61)
        self.assertEqual(worker.run_once(), 1)
        d = self.queue()[0]
        self.assertEqual((d["status"], d["attempts"], sender.calls[0]["headers"]["X-Zylker-Attempt"]), ("succeeded", 2, "2"))

    def test_a_worker_that_died_on_the_final_attempt_leaves_a_failed_delivery_not_a_loop(self):
        hook = self.one_delivery()
        conn = connect(self.config.db_path)
        conn.execute("UPDATE webhook_deliveries SET status = 'sending', attempts = max_attempts, lease_until = ?", (iso(self.now - timedelta(seconds=1)),))
        conn.close()
        sender = FakeSender()
        self.assertEqual(self.worker(sender).run_once(), 0)
        self.assertEqual(sender.calls, [])
        d = self.queue()[0]
        self.assertEqual((d["status"], d["error"], d["lease_until"]), ("failed", "lease_expired", None))
        self.assertEqual(self.ok(self.admin.get("/api/webhooks/%d" % hook["id"]))["health"]["last_status"], "failed")

    def test_a_result_that_arrives_after_the_lease_was_lost_is_ignored(self):
        self.one_delivery()
        slow, other = self.worker(), self.worker()
        claimed = slow._claim(self.now)[0]
        self.assertEqual(claimed["attempts"], 1)
        later = self.now + timedelta(seconds=slow.lease_seconds + 1)
        self.assertEqual(other._claim(later)[0]["attempts"], 2)  # the lease expired, someone else took over
        slow._record(claimed, OK)  # the slow worker finally reports a success for attempt 1
        d = self.queue()[0]
        self.assertEqual((d["status"], d["attempts"]), ("sending", 2))

    def test_two_workers_never_send_the_same_delivery(self):
        self.make_project("WEB")
        self.make_hook()
        for i in range(30):
            self.make_task("WEB", "t%d" % i)
        sent, lock = [], threading.Lock()

        def sender(url, body, headers, timeout=10.0, allow_private=False):
            with lock:
                sent.append(headers["X-Zylker-Delivery"])
            time.sleep(0.002)
            return OK

        workers = [self.worker(sender, batch=3), self.worker(sender, batch=3)]
        threads = [threading.Thread(target=w.run_until_idle) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual((len(sent), len(set(sent))), (30, 30))
        self.assertEqual({(r["status"], r["attempts"]) for r in self.queue()}, {("succeeded", 1)})

    def test_one_worker_sends_in_parallel(self):
        self.make_project("WEB")
        self.make_hook()
        for i in range(3):
            self.make_task("WEB", "t%d" % i)
        barrier = threading.Barrier(3, timeout=10)

        def sender(url, body, headers, timeout=10.0, allow_private=False):
            barrier.wait()  # only returns once all three sends are in flight at the same time
            return OK

        self.assertEqual(self.worker(sender, concurrency=3, batch=3).run_once(), 3)
        self.assertEqual([r["status"] for r in self.queue()], ["succeeded"] * 3)

    def test_paused_webhooks_are_skipped_but_pings_still_go_out(self):
        hook = self.one_delivery()
        path = "/api/webhooks/%d" % hook["id"]
        self.ok(self.admin.patch(path, {"active": False}))
        conn = connect(self.config.db_path)
        conn.execute("UPDATE webhook_deliveries SET status = 'pending', error = NULL, next_attempt_at = ?", (iso(self.now),))  # even a stray due row is not sent
        conn.close()
        sender = FakeSender()
        self.assertEqual(self.worker(sender).run_once(), 0)
        ping = self.ok(self.admin.post(path + "/test"), 202)
        self.assertEqual(self.worker(sender).run_once(), 1)
        self.assertEqual([c["headers"]["X-Zylker-Event"] for c in sender.calls], ["webhook.ping"])
        self.assertEqual(self.ok(self.admin.get("%s/deliveries/%d" % (path, ping["id"])))["status"], "succeeded")

    def test_the_test_event(self):
        hook = self.make_hook(name="Ping me")
        path = "/api/webhooks/%d" % hook["id"]
        ping = self.ok(self.admin.post(path + "/test"), 202)
        self.assertEqual((ping["event"], ping["status"], ping["attempts"], ping["max_attempts"]), ("webhook.ping", "pending", 0, 1))
        self.assertTrue(ping["event_id"].startswith("evt_test_"))
        sender = FakeSender(FAIL_500)
        self.worker(sender).run_once()
        done = self.ok(self.admin.get("%s/deliveries/%d" % (path, ping["id"])))
        self.assertEqual((done["status"], done["attempts"], done["next_attempt_at"], done["error"]), ("failed", 1, None, "http_500"))  # never retried
        self.assertEqual(
            done["payload"],
            {"id": ping["event_id"], "event": "webhook.ping", "created_at": "2026-10-16T09:00:00Z", "project": None, "task": None,
             "actor": {"username": "admin", "display_name": "Admin"}, "details": {"message": "Test event from Zylker Projects", "webhook": "Ping me"}},
        )  # fmt: skip
        self.assertEqual(self.worker(sender).run_once(), 0)
        self.assertEqual(self.admin.post("/api/webhooks/999/test").status, 404)

    def test_redelivery(self):
        hook = self.one_delivery()
        path = "/api/webhooks/%d" % hook["id"]
        first = self.queue()[0]["id"]
        busy = self.admin.post("%s/deliveries/%d/redeliver" % (path, first))
        self.assertEqual((busy.status, busy.error_code), (409, "delivery_in_progress"))
        worker = self.worker(FakeSender(FAIL_500, FAIL_500, FAIL_500, FAIL_500, FAIL_500, FAIL_500, OK))
        for _ in range(6):
            worker.run_once()
            self.now += timedelta(days=1)
        self.assertEqual(self.queue()[0]["status"], "failed")
        again = self.ok(self.admin.post("%s/deliveries/%d/redeliver" % (path, first)), 202)
        self.assertNotEqual(again["id"], first)
        self.assertEqual((again["redelivery_of"], again["status"], again["attempts"], again["event_id"]), (first, "pending", 0, self.queue()[0]["event_id"]))
        self.assertEqual(self.queue()[1]["payload"], self.queue()[0]["payload"])
        worker.run_once()
        self.assertEqual([r["status"] for r in self.queue()], ["failed", "succeeded"])
        self.ok(self.admin.patch(path, {"active": False}))
        paused = self.admin.post("%s/deliveries/%d/redeliver" % (path, first))
        self.assertEqual((paused.status, paused.error_code), (409, "webhook_paused"))
        other = self.make_hook(name="other")
        self.assertEqual(self.admin.post("/api/webhooks/%d/deliveries/%d/redeliver" % (other["id"], first)).status, 404)  # not that webhook's delivery
        self.assertEqual(self.admin.post("%s/deliveries/999/redeliver" % path).status, 404)

    def test_the_delivery_log_is_bounded(self):
        self.make_project("WEB")
        self.make_hook()
        with mock.patch.object(webhooks, "KEEP_DELIVERIES", 3):
            for i in range(6):
                self.make_task("WEB", "t%d" % i)
            self.worker().run_until_idle()
        self.assertEqual([r["id"] for r in self.queue()], [4, 5, 6])

    def test_unfinished_deliveries_are_never_pruned(self):
        self.make_project("WEB")
        hook = self.make_hook()
        with mock.patch.object(webhooks, "KEEP_DELIVERIES", 1):
            for i in range(3):
                self.make_task("WEB", "t%d" % i)
            self.worker(FakeSender(OK, OK, FAIL_500)).run_until_idle()
        self.assertEqual([(r["id"], r["status"]) for r in self.queue(hook["id"])], [(2, "succeeded"), (3, "pending")])

    def test_worker_heartbeat_is_reported(self):
        idle = {"running": False, "last_seen": None}
        self.assertEqual(self.ok(self.admin.get("/api/webhooks"))["worker"], idle)
        self.worker().run_once()
        self.assertEqual(self.ok(self.admin.get("/api/webhooks"))["worker"], {"running": True, "last_seen": "2026-10-16T09:00:00Z"})
        self.now += timedelta(seconds=webhooks.HEARTBEAT_STALE_SECONDS + 1)
        self.assertFalse(self.ok(self.admin.get("/api/webhooks"))["worker"]["running"])

    def test_an_idle_worker_never_takes_the_writer_lock(self):
        worker = self.worker()
        worker.run_once()  # the first pass writes the heartbeat
        blocker = connect(self.config.db_path)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")  # somebody is in the middle of a write...
        started = time.monotonic()
        self.assertEqual(worker.run_once(), 0)  # ...and an idle poll does not have to wait for them
        self.assertLess(time.monotonic() - started, 1.0)
        blocker.execute("ROLLBACK")

    def test_a_clock_set_back_does_not_silence_the_heartbeat(self):
        worker = self.worker()
        worker.run_once()
        self.now -= timedelta(hours=1)
        worker.run_once()
        self.assertEqual(self.ok(self.admin.get("/api/webhooks"))["worker"], {"running": True, "last_seen": iso(self.now)})

    def test_a_failing_pass_backs_off_instead_of_spinning(self):
        class FakeStop:
            def __init__(self, stop_after):
                self.waits, self.flag, self.stop_after = [], False, stop_after

            def is_set(self):
                return self.flag

            def set(self):
                self.flag = True

            def wait(self, seconds):
                self.waits.append(seconds)
                self.flag = len(self.waits) >= self.stop_after

        worker = self.worker(poll_interval=1.0)
        worker._stop = FakeStop(7)
        with mock.patch.object(worker, "run_once", side_effect=RuntimeError("the database went away")), self.assertLogs("zylker.webhooks", "ERROR"):
            worker.run_forever()
        self.assertEqual(worker._stop.waits, [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0])
        worker = self.worker(poll_interval=1.0)
        worker._stop = FakeStop(3)
        with mock.patch.object(worker, "run_once", side_effect=[RuntimeError("blip"), 0, 0]), self.assertLogs("zylker.webhooks", "ERROR"):
            worker.run_forever()
        self.assertEqual(worker._stop.waits, [2.0, 1.0, 1.0])  # one good pass and it is back to the idle rate

    def test_the_worker_thread_starts_delivers_and_stops(self):
        self.make_project("WEB")
        hook = self.make_hook()
        worker = self.worker(sender=webhook_http.post, poll_interval=0.02)  # the real sender, over a real socket
        worker.start()
        self.addCleanup(worker.stop)
        self.make_task("WEB", "Delivered by the thread")
        self.assertTrue(self.wait_for(lambda: self.receiver.requests), "nothing arrived")
        self.assertTrue(self.wait_for(lambda: self.queue()[0]["status"] == "succeeded"))
        sent = self.receiver.requests[0]
        self.assertEqual(json.loads(sent["raw"])["task"]["title"], "Delivered by the thread")
        self.assertTrue(webhooks.verify(hook["secret"], sent["headers"]["X-Zylker-Timestamp"], sent["raw"], sent["headers"]["X-Zylker-Signature"], now=STAMP))
        worker.stop()
        self.assertFalse(worker._thread.is_alive())

    def test_the_delivery_api(self):
        self.make_project("WEB")
        hook = self.make_hook()
        for i in range(5):
            self.make_task("WEB", "t%d" % i)
        other = self.make_hook(name="other")  # created after the events, so its queue stays empty
        self.worker(FakeSender(OK, FAIL_500, OK)).run_until_idle()
        path = "/api/webhooks/%d/deliveries" % hook["id"]
        everything = self.ok(self.admin.get(path))
        ids = [d["id"] for d in everything["items"]]
        self.assertEqual((ids, everything["next_before"]), ([5, 4, 3, 2, 1], None))
        self.assertNotIn("payload", everything["items"][0])
        page = self.ok(self.admin.get(path, query={"limit": 2}))
        self.assertEqual(([d["id"] for d in page["items"]], page["next_before"]), ([5, 4], 4))
        older = self.ok(self.admin.get(path, query={"limit": 2, "before": page["next_before"]}))
        self.assertEqual([d["id"] for d in older["items"]], [3, 2])
        pending = self.ok(self.admin.get(path, query={"status": "pending"}))["items"]
        self.assertEqual([(d["id"], d["error"], d["response_excerpt"]) for d in pending], [(2, "http_500", "boom")])
        self.assertEqual(len(self.ok(self.admin.get(path, query={"status": "succeeded"}))["items"]), 4)
        for query in ({"limit": 0}, {"limit": "x"}, {"limit": 101}, {"status": "weird"}, {"before": "x"}):
            self.assertEqual(self.admin.get(path, query=query).status, 422, query)
        detail = self.ok(self.admin.get("%s/%d" % (path, 5)))
        self.assertEqual(detail["payload"]["event"], "task.created")
        self.assertEqual(self.admin.get("/api/webhooks/%d/deliveries/5" % other["id"]).status, 404)  # not that webhook's delivery
        self.assertEqual(self.admin.get(path + "/999").status, 404)
        self.assertEqual(self.admin.get("/api/webhooks/999/deliveries").status, 404)
        listed = self.ok(self.admin.get("/api/webhooks"))["items"]
        self.assertEqual([(w["name"], w["pending_deliveries"]) for w in listed], [("Hook", 1), ("other", 0)])

if __name__ == "__main__":
    unittest.main()
