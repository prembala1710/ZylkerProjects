"""The admin log monitor: schema upgrade, history integrity, querying, security events, summary and export."""

import csv
import io
import json
import os
import sqlite3
from datetime import timedelta
from unittest import mock

from helpers import ApiTestCase, Client

from zylker_pm import db
from zylker_pm.app import App, client_ip
from zylker_pm.config import Config
from zylker_pm.db import init_db
from zylker_pm.services import audit


class AuditBase(ApiTestCase):
    def audit(self, **query):
        return self.ok(self.admin.get("/api/audit", query=query))

    def actions(self, **query):
        return [i["action"] for i in self.audit(**query)["items"]]

    def events(self, action, **query):
        return [i for i in self.audit(action=action, **query)["items"]]

    def minutes(self, n):
        self.now = self.now + timedelta(minutes=n)

    def prime(self, *users):
        """Everyone's first request starts a session, which is itself an event (written after that request).
        Tests that count events open the sessions they will use first, so the counting is not skewed."""
        for user in users or ("admin",):
            self.client(user).get("/api/me")


class AccessTests(AuditBase):
    def test_only_admins_can_read_the_log(self):
        for path in ("/api/audit", "/api/audit/summary", "/api/audit/export.csv"):
            for user in ("alice", "bob", "vera"):
                r = self.client(user).get(path)
                self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), (path, user))
            self.assertEqual(self.client().get(path).status, 401, path)
            self.assertEqual(self.admin.get(path).status, 200, path)

    def test_meta_lists_the_categories_and_severities(self):
        meta = self.ok(self.client("vera").get("/api/meta"))
        self.assertIn("task", meta["audit_categories"])
        self.assertEqual(meta["audit_severities"], ["info", "notice", "warning", "error"])


class UpgradeTests(AuditBase):
    def v2_database(self, path):
        """A database exactly as the previous release left it, with history that shows why the upgrade matters."""
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        db._migrate(conn, 1)
        conn.executescript(
            """
            INSERT INTO users (id, username, display_name, role, token_hash, created_at) VALUES (1,'old','Old','admin','h','2026-01-01T00:00:00Z');
            INSERT INTO projects (id, key, name, status, owner_id, created_at, updated_at) VALUES (1,'OLD','Legacy','active',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO tasks (id, project_id, number, title, reporter_id, created_at, updated_at) VALUES (1,1,1,'Alive',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
            INSERT INTO activity (id, project_id, task_id, task_key, actor_id, action, details, created_at) VALUES
              (1, 1, 1, 'OLD-1', 1, 'task.created', '{"title":"Alive"}', '2026-01-01T00:00:01Z'),
              (2, 1, 2, 'OLD-2', 1, 'task.deleted', '{"title":"Gone"}', '2026-01-01T00:00:02Z'),
              (3, 1, 1, 'OLD-9', 1, 'task.created', '{"title":"Id reused"}', '2026-01-01T00:00:03Z'),
              (4, NULL, NULL, NULL, 1, 'user.created', '{"username":"old"}', '2026-01-01T00:00:04Z');
            """
        )
        conn.close()

    def rows(self, path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM activity ORDER BY id").fetchall(), conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_upgrade_keeps_every_event_and_untangles_reused_ids(self):
        path = os.path.join(self.tmp, "v2.db")
        self.v2_database(path)
        init_db(path)
        rows, version = self.rows(path)
        self.assertEqual(version, db.SCHEMA_VERSION)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4])  # nothing lost, ids and order intact
        self.assertEqual([r["project_key"] for r in rows], ["OLD", "OLD", "OLD", None])
        self.assertEqual([r["task_id"] for r in rows], [1, None, None, None])  # only the row that still matches keeps its task
        self.assertEqual([r["task_key"] for r in rows], ["OLD-1", "OLD-2", "OLD-9", None])  # but no key is lost
        self.assertEqual([(r["ip"], r["request_id"]) for r in rows], [(None, None)] * 4)

    def test_deleting_a_project_no_longer_erases_its_history(self):
        path = os.path.join(self.tmp, "v2b.db")
        self.v2_database(path)
        init_db(path)
        conn = db.connect(path)
        conn.execute("DELETE FROM projects WHERE id = 1")  # foreign keys are ON: the old cascade would have fired here
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0], 4)
        conn.close()

    def test_indexes_exist_and_a_fresh_install_equals_an_upgrade(self):
        old, fresh = os.path.join(self.tmp, "v2c.db"), os.path.join(self.tmp, "fresh.db")
        self.v2_database(old)
        init_db(old)
        init_db(fresh)

        def shape(p):
            conn = sqlite3.connect(p)
            try:
                return conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
            finally:
                conn.close()

        self.assertEqual(shape(old), shape(fresh))
        names = {r[1] for r in shape(fresh)}
        self.assertTrue({"idx_activity_actor", "idx_activity_action", "idx_activity_created", "idx_activity_project", "idx_activity_task"} <= names)

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v2d.db")
        self.v2_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE activity_v3 (surprise INTEGER)")  # makes the migration's first statement fail
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0], 4)
        self.assertNotIn("project_key", [r[1] for r in conn.execute("PRAGMA table_info(activity)")])
        conn.close()

    def test_a_v1_database_walks_through_every_step(self):
        path = os.path.join(self.tmp, "v1.db")
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        init_db(path)
        self.assertEqual(self.rows(path)[1], db.SCHEMA_VERSION)


class HistoryIntegrityTests(AuditBase):
    def test_a_new_task_never_inherits_a_deleted_tasks_history(self):
        self.make_project("WEB", members=("bob",))
        self.make_task("WEB", "first")
        self.make_task("WEB", "doomed secret title")
        self.assertEqual(self.client("alice").delete("/api/tasks/WEB-2").status, 204)
        fresh = self.make_task("WEB", "brand new")
        feed = self.ok(self.client("alice").get("/api/tasks/%s/activity" % fresh["key"]))["items"]
        self.assertEqual([(a["action"], a["task"]) for a in feed], [("task.created", fresh["key"])])
        # ... yet the deletion is still on record, findable by key, and in the project's history
        self.assertEqual([e["task"] for e in self.events("task.deleted")], ["WEB-2"])
        project_feed = self.ok(self.client("alice").get("/api/projects/WEB/activity"))["items"]
        self.assertIn("task.deleted", [a["action"] for a in project_feed])

    def test_cascade_deleting_subtasks_detaches_them_all(self):
        self.make_project("WEB")
        self.make_task("WEB", "parent")
        self.make_task("WEB", "child", parent="WEB-1")
        self.make_task("WEB", "grandchild", parent="WEB-2")
        self.assertEqual(self.client("alice").delete("/api/tasks/WEB-1", query={"cascade": "true"}).status, 204)
        conn = sqlite3.connect(self.config.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity WHERE task_id IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM activity WHERE task_key IN ('WEB-1','WEB-2','WEB-3')").fetchone()[0], 4)  # 3 created + 1 deleted
        conn.close()
        self.assertEqual(self.make_task("WEB", "reuses an id")["key"], "WEB-4")  # numbering is separate from row ids

    def test_deleting_a_project_keeps_its_history_readable_and_unlinked(self):
        self.make_project("OLD")
        self.make_task("OLD", "Something")
        self.assertEqual(self.admin.delete("/api/projects/OLD", query={"confirm": "OLD"}).status, 204)
        kept = self.audit(project="OLD")["items"]
        self.assertEqual({i["action"] for i in kept}, {"project.created", "task.created", "project.deleted"})
        self.assertTrue(all(i["project"] == "OLD" for i in kept))  # still found by key, though the project is gone
        # a project created later may be given the freed row id; it must not inherit the old project's feed
        self.make_project("NEW")
        self.assertEqual([a["action"] for a in self.ok(self.client("alice").get("/api/projects/NEW/activity"))["items"]], ["project.created"])
        self.assertEqual(self.audit(project="NEW")["total"], 1)


class QueryTests(AuditBase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob",))
        self.make_project("API", members=("bob",))
        self.make_task("WEB", "Login page", assignee="bob")
        self.make_task("API", "Endpoint")
        self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "looks good"})
        self.client("bob").patch("/api/tasks/WEB-1", {"priority": "high"})
        self.client("vera").get("/api/audit")  # a denial

    def test_newest_first_with_full_event_shape(self):
        page = self.audit()
        ids = [i["id"] for i in page["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(page["newest_id"], ids[0])
        item = next(i for i in page["items"] if i["action"] == "comment.added")
        self.assertEqual((item["category"], item["severity"], item["task"], item["project"]), ("comment", "info", "WEB-1", "WEB"))
        self.assertEqual(item["actor"]["username"], "bob")
        self.assertEqual(item["ip"], "203.0.113.7")
        self.assertRegex(item["request_id"], r"^[0-9a-f]{16}$")

    def test_filters(self):
        self.assertEqual({i["actor"]["username"] for i in self.audit(actor="bob")["items"]}, {"bob"})
        self.assertTrue(all(i["actor"] is None for i in self.audit(actor="system")["items"]))  # created by the test fixtures
        self.assertEqual({i["category"] for i in self.audit(category="task")["items"]}, {"task"})
        self.assertEqual({i["category"] for i in self.audit(category="task,comment")["items"]}, {"task", "comment"})
        self.assertEqual({i["action"] for i in self.audit(action="comment.added")["items"]}, {"comment.added"})
        self.assertEqual({i["project"] for i in self.audit(project="api")["items"]} - {None}, {"API"})  # key is case-insensitive
        self.assertEqual({i["severity"] for i in self.audit(severity="warning")["items"]}, {"warning"})
        self.assertIn("project.created", self.actions(severity="info"))
        self.assertNotIn("user.created", self.actions(severity="info"))  # that one is a "notice"
        self.assertEqual(self.audit(ip="203.0.113.7")["total"], self.audit()["total"] - self.audit(actor="system")["total"] - 0)

    def test_category_matching_is_exact_not_a_pattern(self):
        self.admin.post("/api/project-fields", {"key": "client", "label": "Client", "type": "text"})
        self.assertEqual(self.actions(category="project_field"), ["project_field.created"])
        self.assertNotIn("project_field.created", self.actions(category="project"))  # "project" must not swallow "project_field"

    def test_search_covers_actor_task_action_details_and_address(self):
        self.assertIn("task.created", self.actions(q="login page"))  # a task title, inside the details
        self.assertIn("comment.added", self.actions(q="WEB-1"))
        self.assertTrue(self.audit(q="bob")["items"])
        self.assertIn("auth.denied", self.actions(q="insufficient_role"))
        self.assertEqual(self.audit(q="203.0.113.7")["total"], self.audit(ip="203.0.113.7")["total"])
        self.assertEqual(self.audit(q="no such thing anywhere")["items"], [])
        self.assertEqual(self.audit(q="%")["items"], [])  # wildcard characters stay literal
        self.assertEqual(self.audit(q="x'; DROP TABLE activity; --")["items"], [])
        self.assertTrue(self.audit()["items"])  # and the table is still there

    def test_request_id_correlates_every_event_of_one_request(self):
        r = self.client("bob").post("/api/projects/WEB/tasks", {"title": "Correlated"})
        rid = r.headers["X-Request-Id"]
        found = self.audit(request_id=rid)["items"]
        self.assertEqual([i["action"] for i in found], ["task.created"])
        self.assertEqual(self.audit(request_id="doesnotexist1")["items"], [])

    def test_date_range_uses_the_servers_timezone(self):
        cfg = Config(db_path=os.path.join(self.tmp, "ist.db"), timezone="Asia/Kolkata")  # UTC+5:30
        init_db(cfg.db_path)
        app = App(cfg, clock=lambda: self.now)
        from zylker_pm.services import users

        with app.session() as ctx:
            _, token = users.create_user(ctx, {"username": "root", "display_name": "Root", "role": "admin"})
        c = Client(app, token)
        # 2026-10-16 20:00 UTC is already 17 Oct 01:30 in Kolkata
        self.now = self.now.replace(hour=20, minute=0)
        c.post("/api/projects", {"key": "LATE", "name": "Late"})
        on = lambda d: [i["action"] for i in self.ok(c.get("/api/audit", query={"from": d, "to": d}))["items"]]  # noqa: E731
        self.assertIn("project.created", on("2026-10-17"))
        self.assertNotIn("project.created", on("2026-10-16"))
        for bad in ({"from": "yesterday"}, {"to": "2026-13-01"}):
            self.assertEqual(c.get("/api/audit", query=bad).status, 422)

    def test_paging_back_and_the_live_tail(self):
        self.prime()
        first = self.audit(limit=3)
        self.assertEqual(len(first["items"]), 3)
        self.assertTrue(first["next_before"])
        second = self.audit(limit=3, before=first["next_before"])
        self.assertTrue(all(i["id"] < first["next_before"] for i in second["items"]))
        self.assertNotIn("total", second)  # the total is only worked out for the first page
        seen = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
        self.assertEqual(len(seen), 6)
        # live tail: nothing newer, then something newer
        newest = first["newest_id"]
        self.assertEqual(self.audit(after=newest)["items"], [])
        self.make_task("WEB", "While you were watching")
        tail = self.audit(after=newest)
        self.assertEqual([i["action"] for i in tail["items"]], ["task.created"])
        self.assertFalse(tail["gap"])
        for _ in range(5):
            self.make_task("WEB", "burst")
        tail = self.audit(after=newest, limit=2)
        self.assertEqual((len(tail["items"]), tail["gap"]), (2, True))  # more arrived than fit: the client must reload

    def test_the_filtered_total_is_capped(self):
        with mock.patch.object(audit, "COUNT_CAP", 3):
            page = self.audit()
        self.assertEqual((page["total"], page["total_capped"]), (3, True))
        self.assertEqual(self.audit(action="comment.added")["total_capped"], False)

    def test_bad_filters_are_422_with_a_reason(self):
        for bad in ({"category": "nope"}, {"severity": "loud"}, {"action": "DROP TABLE"}, {"ip": "not-an-ip"}, {"request_id": "x"},
                    {"limit": "0"}, {"limit": "500"}, {"before": "abc"}, {"after": "-1"}):  # fmt: skip
            r = self.admin.get("/api/audit", query=bad)
            self.assertEqual((r.status, r.error_code), (422, "validation_error"), bad)


class SecurityEventTests(AuditBase):
    def test_a_bad_token_is_recorded_without_the_token(self):
        secret = "zpm_super-secret-value-that-must-never-be-stored"
        r = self.client().get("/api/me", token=secret, headers={"User-Agent": "curl/8.0"}, remote_addr="198.51.100.9")
        self.assertEqual(r.status, 401)
        [e] = self.events("auth.failed")
        self.assertEqual((e["severity"], e["actor"], e["ip"]), ("warning", None, "198.51.100.9"))
        self.assertEqual(e["details"]["reason"], "invalid_token")
        self.assertEqual((e["details"]["path"], e["details"]["method"], e["details"]["user_agent"]), ("/api/me", "GET", "curl/8.0"))
        self.assertEqual(e["request_id"], r.headers["X-Request-Id"])
        self.assertNotIn("super-secret", json.dumps(self.audit()))
        conn = sqlite3.connect(self.config.db_path)
        stored = " ".join(str(v) for row in conn.execute("SELECT * FROM activity") for v in row)
        conn.close()
        self.assertNotIn("super-secret", stored)

    def test_missing_and_invalid_tokens_are_told_apart(self):
        self.client().get("/api/me", token=None)
        self.client().get("/api/me", token="zpm_nope")
        self.assertEqual(sorted(e["details"]["reason"] for e in self.events("auth.failed")), ["invalid_token", "missing_token"])

    def test_repeats_merge_into_one_counted_row_then_start_a_new_one(self):
        for _ in range(4):
            self.client().get("/api/me", token="zpm_nope", remote_addr="198.51.100.9")
        self.client().get("/api/me", token="zpm_nope", remote_addr="198.51.100.10")  # a different address is its own row
        rows = self.events("auth.failed")
        self.assertEqual(sorted((e["ip"], e["details"]["count"]) for e in rows), [("198.51.100.10", 1), ("198.51.100.9", 4)])
        self.minutes(6)  # outside the five-minute window
        self.client().get("/api/me", token="zpm_nope", remote_addr="198.51.100.9")
        self.assertEqual(len(self.events("auth.failed")), 3)
        self.assertEqual(self.ok(self.admin.get("/api/audit/summary"))["security"]["failed_sign_ins"], 6)  # counts, not rows

    def test_a_flood_cannot_grow_the_log_without_bound(self):
        for _ in range(200):
            self.client().get("/api/projects", token="zpm_nope", remote_addr="198.51.100.9")
        self.assertEqual(len(self.events("auth.failed")), 1)
        self.assertEqual(self.events("auth.failed")[0]["details"]["count"], 200)

    def test_a_denial_names_who_and_which_route_not_the_raw_path(self):
        self.make_project("WEB", members=("bob",))
        self.make_task("WEB", "T")
        self.client("bob").delete("/api/tasks/WEB-1")  # members cannot delete tasks
        self.client("bob").get("/api/audit")
        denied = self.events("auth.denied")
        self.assertEqual(sorted((e["actor"]["username"], e["details"]["route"], e["details"]["code"]) for e in denied),
                         [("bob", "/api/audit", "insufficient_role"), ("bob", "/api/tasks/{key}", "insufficient_role")])  # fmt: skip
        self.assertTrue(all(e["severity"] == "warning" for e in denied))
        for key in ("WEB-1", "WEB-2", "WEB-3"):  # different tasks, same route: still one row
            self.client("bob").delete("/api/tasks/" + key)
        self.assertEqual(sum(e["details"]["count"] for e in self.events("auth.denied") if e["details"]["route"] == "/api/tasks/{key}"), 4)
        self.assertEqual(len([e for e in self.events("auth.denied") if e["details"]["route"] == "/api/tasks/{key}"]), 1)

    def test_a_denial_from_inside_a_rolled_back_request_is_still_recorded(self):
        self.make_project("WEB", members=("bob",))
        self.make_project("API", members=("carol",))
        self.make_task("API", "Not for bob")
        r = self.client("bob").patch("/api/tasks/API-1", {"title": "hijack"})  # bob is not a member: refused after work started
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        self.assertEqual([e["details"]["code"] for e in self.events("auth.denied")], ["not_project_member"])

    def test_hostile_text_cannot_forge_log_entries_or_grow_unbounded(self):
        evil_ua = "Mozilla\r\nINFO forged\x00" + "A" * 500
        self.client().get("/api/me", token="zpm_nope", headers={"User-Agent": evil_ua})
        [e] = self.events("auth.failed")
        ua = e["details"]["user_agent"]
        self.assertNotRegex(ua, r"[\r\n\x00]")
        self.assertLessEqual(len(ua), 200)
        self.client().get("/api/me\nINFO forged", token="zpm_nope2")
        paths = [e["details"]["path"] for e in self.events("auth.failed")]
        self.assertTrue(all("\n" not in p for p in paths))

    def test_a_crash_is_recorded_with_the_exception_type_but_never_its_message(self):
        from zylker_pm import app as app_module

        route = next(r for r in app_module.ROUTES if r.pattern == "/api/me")
        original = route.handler

        def boom(ctx, req):
            raise RuntimeError("secret internal detail /srv/app/config.py password=hunter2")

        route.handler = boom
        try:
            with self.assertLogs("zylker.access", level="ERROR"):
                r = self.client("bob").get("/api/me")
        finally:
            route.handler = original
        self.assertEqual(r.status, 500)
        body = r.json["error"]
        self.assertEqual(body["request_id"], r.headers["X-Request-Id"])  # what a user can quote to the admin
        self.assertNotIn("hunter2", r.text)
        [e] = self.events("system.error")
        self.assertEqual((e["severity"], e["actor"]["username"], e["details"]["error"], e["details"]["route"]), ("error", "bob", "RuntimeError", "/api/me"))
        self.assertEqual(e["request_id"], r.headers["X-Request-Id"])
        self.assertNotIn("hunter2", json.dumps(self.audit()))

    def test_a_failed_request_leaves_no_phantom_data_events(self):
        self.make_project("WEB", members=("bob",))
        self.prime("bob", "admin")
        before = self.audit()["total"]
        self.client("bob").post("/api/projects/WEB/tasks", {"title": "x" * 500})  # validation error
        events = self.actions()
        self.assertEqual(self.audit()["total"], before)
        self.assertNotIn("auth.failed", events)


class SessionTests(AuditBase):
    def test_a_session_is_recorded_once_per_half_hour_per_user(self):
        for _ in range(3):
            self.client("bob").get("/api/me")
        self.assertEqual(len(self.events("auth.session_started", actor="bob")), 1)
        self.minutes(29)
        self.client("bob").get("/api/me")
        self.assertEqual(len(self.events("auth.session_started", actor="bob")), 1)
        self.minutes(2)  # 31 minutes since the first request
        self.client("bob").get("/api/me")
        self.assertEqual(len(self.events("auth.session_started", actor="bob")), 2)
        self.client("carol").get("/api/me")
        self.assertEqual(len(self.events("auth.session_started", actor="carol")), 1)  # tracked per user

    def test_a_session_records_the_client_and_address(self):
        self.client("bob").get("/api/me", headers={"User-Agent": "Mozilla/5.0 Test"}, remote_addr="198.51.100.77")
        [e] = self.events("auth.session_started", actor="bob")
        self.assertEqual((e["ip"], e["details"]["user_agent"], e["severity"]), ("198.51.100.77", "Mozilla/5.0 Test", "info"))

    def test_failed_requests_do_not_start_a_session(self):
        self.client().get("/api/me", token="zpm_nope")
        self.assertEqual(self.events("auth.session_started"), [])


class ExportEventTests(AuditBase):
    def test_exporting_task_data_is_recorded_with_the_row_count(self):
        self.make_project("WEB")
        self.make_task("WEB", "One")
        self.make_task("WEB", "Two")
        self.assertEqual(self.client("vera").get("/api/projects/WEB/export.csv").status, 200)
        [e] = self.events("data.exported")
        self.assertEqual((e["actor"]["username"], e["severity"]), ("vera", "notice"))
        self.assertEqual((e["details"]["project"], e["details"]["rows"]), ("WEB", 2))

    def test_a_failed_export_is_not_recorded_as_an_export(self):
        self.client("vera").get("/api/projects/NOPE/export.csv")
        self.assertEqual(self.events("data.exported"), [])

    def test_exporting_the_audit_log_is_itself_audited(self):
        r = self.admin.get("/api/audit/export.csv", query={"category": "task", "q": "login"})
        self.assertEqual(r.status, 200)
        [e] = self.events("audit.exported")
        self.assertEqual(e["details"]["filters"], {"q": "login", "category": "task"})
        self.assertEqual(e["details"]["rows"], 0)


class ResilienceTests(AuditBase):
    def test_an_audit_write_failure_never_breaks_the_request(self):
        with mock.patch.object(audit, "record", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertLogs("zylker.access", level="WARNING") as logs:
                r = self.client().get("/api/me", token="zpm_nope")
                ok = self.client("bob").get("/api/me")
        self.assertEqual(r.status, 401)  # still the honest answer
        self.assertEqual(ok.status, 200)
        self.assertTrue(any("could not write audit event" in line for line in logs.output))

    def test_normal_requests_do_not_write_extra_rows(self):
        self.prime("bob", "admin")
        before = self.audit()["total"]
        for _ in range(5):
            self.client("bob").get("/api/projects")
            self.client("bob").get("/api/tasks")
        self.assertEqual(self.audit()["total"], before)  # routine reads leave no trace


class RequestIdTests(AuditBase):
    def test_every_response_carries_a_request_id(self):
        for r in (self.admin.get("/api/me"), self.client().get("/api/health"), self.client().get("/", token=None), self.client().get("/api/nope", token=None)):
            self.assertRegex(r.headers["X-Request-Id"], r"^[A-Za-z0-9._-]{8,64}$")

    def test_a_valid_incoming_id_is_kept_and_a_malformed_one_replaced(self):
        r = self.client("bob").get("/api/me", headers={"X-Request-Id": "trace-abc-12345"})
        self.assertEqual(r.headers["X-Request-Id"], "trace-abc-12345")
        for bad in ("short", "has spaces in it!", "x" * 200, "<script>alert(1)</script>"):
            r = self.client("bob").get("/api/me", headers={"X-Request-Id": bad})
            self.assertNotEqual(r.headers["X-Request-Id"], bad)
            self.assertRegex(r.headers["X-Request-Id"], r"^[0-9a-f]{16}$")

    def test_the_access_log_line_carries_the_id(self):
        with self.assertLogs("zylker.access", level="INFO") as logs:
            r = self.client("bob").get("/api/me")
        self.assertTrue(any("[%s]" % r.headers["X-Request-Id"] in line for line in logs.output))


class ClientAddressTests(AuditBase):
    def env(self, remote="203.0.113.7", forwarded=None):
        env = {"REMOTE_ADDR": remote}
        if forwarded is not None:
            env["HTTP_X_FORWARDED_FOR"] = forwarded
        return env

    def test_without_trusted_proxies_the_forwarded_header_is_ignored(self):
        self.assertEqual(client_ip(self.env(forwarded="198.51.100.1"), 0), "203.0.113.7")

    def test_with_one_proxy_the_client_is_the_rightmost_entry(self):
        # a client can send its own X-Forwarded-For; the proxy appends the address it actually saw
        self.assertEqual(client_ip(self.env("10.0.0.2", "1.2.3.4, 198.51.100.1"), 1), "198.51.100.1")
        self.assertEqual(client_ip(self.env("10.0.0.2", "198.51.100.1"), 1), "198.51.100.1")

    def test_with_two_proxies_it_is_the_second_from_the_right(self):
        self.assertEqual(client_ip(self.env("10.0.0.2", "9.9.9.9, 198.51.100.1, 10.0.0.9"), 2), "198.51.100.1")

    def test_missing_short_or_junk_headers_fall_back_to_the_connection(self):
        self.assertEqual(client_ip(self.env("203.0.113.7"), 1), "203.0.113.7")
        self.assertEqual(client_ip(self.env("203.0.113.7", "198.51.100.1"), 2), "203.0.113.7")  # fewer entries than proxies
        self.assertEqual(client_ip(self.env("203.0.113.7", "not an ip, <script>"), 1), "203.0.113.7")
        self.assertIsNone(client_ip({"REMOTE_ADDR": "garbage\r\nINFO forged"}, 0))
        self.assertIsNone(client_ip({}, 0))
        self.assertEqual(client_ip(self.env("::1"), 0), "::1")

    def test_it_reaches_the_log_through_the_app(self):
        cfg = Config(db_path=self.config.db_path, proxy_hops=1)
        app = App(cfg, clock=lambda: self.now)
        c = Client(app, self.tokens["bob"])
        c.get("/api/me", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.1"}, remote_addr="10.0.0.2")
        [e] = self.events("auth.session_started", actor="bob")
        self.assertEqual(e["ip"], "198.51.100.1")

    def test_the_setting_is_validated(self):
        self.assertEqual(Config.from_env({"ZYLKER_PROXY_HOPS": "2"}).proxy_hops, 2)
        self.assertEqual(Config.from_env({}).proxy_hops, 0)
        for bad in ("-1", "6", "two", ""):
            with self.assertRaises(ValueError, msg=bad):
                Config.from_env({"ZYLKER_PROXY_HOPS": bad})


class SummaryTests(AuditBase):
    def test_counts_by_day_category_severity_and_actor(self):
        self.make_project("WEB", members=("bob",))
        self.prime()
        self.make_task("WEB", "A", as_user="bob")
        self.make_task("WEB", "B", as_user="bob")
        self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "hi"})
        self.advance_days(1)
        self.make_task("WEB", "C")
        self.prime()  # a day later the admin's session has lapsed: open the new one before measuring
        s = self.ok(self.admin.get("/api/audit/summary", query={"days": 3}))
        self.assertEqual((s["days"], s["from"], s["to"]), (3, "2026-10-15", "2026-10-17"))
        by_day = {d["date"]: d["count"] for d in s["by_day"]}
        self.assertEqual(list(by_day), ["2026-10-15", "2026-10-16", "2026-10-17"])  # zero-filled and in order
        self.assertEqual(by_day["2026-10-15"], 0)
        self.assertGreater(by_day["2026-10-16"], by_day["2026-10-17"])
        self.assertEqual(sum(by_day.values()), s["total"])
        cats = {c["category"]: c["count"] for c in s["by_category"]}
        self.assertEqual((cats["task"], cats["comment"], cats["project"]), (3, 1, 1))
        self.assertEqual(sum(s["by_severity"].values()), s["total"])
        top = {a["actor"]["username"] if a["actor"] else None: a["count"] for a in s["top_actors"]}
        self.assertEqual(top["bob"], 3)  # 2 tasks + 1 comment; sign-in noise is not "activity"
        self.assertEqual(s["newest_id"], self.audit()["newest_id"])

    def test_days_follow_the_servers_timezone_including_half_hour_offsets(self):
        cfg = Config(db_path=os.path.join(self.tmp, "ist2.db"), timezone="Asia/Kolkata")
        init_db(cfg.db_path)
        app = App(cfg, clock=lambda: self.now)
        from zylker_pm.services import users

        with app.session() as ctx:
            _, token = users.create_user(ctx, {"username": "root", "display_name": "Root", "role": "admin"})
        c = Client(app, token)
        for hour, minute, key in ((18, 29, "AAA"), (18, 31, "BBB")):  # 18:30 UTC is local midnight
            self.now = self.now.replace(hour=hour, minute=minute)
            c.post("/api/projects", {"key": key, "name": key})
        s = self.ok(c.get("/api/audit/summary", query={"days": 2}))
        by_day = {d["date"]: d["count"] for d in s["by_day"]}
        created = {d: 0 for d in by_day}
        for item in self.ok(c.get("/api/audit", query={"action": "project.created"}))["items"]:
            created[{"AAA": "2026-10-16", "BBB": "2026-10-17"}[item["project"]]] += 1
        self.assertEqual(created, {"2026-10-16": 1, "2026-10-17": 1})  # one event each side of local midnight
        self.assertGreaterEqual(by_day["2026-10-16"], 1)
        self.assertGreaterEqual(by_day["2026-10-17"], 1)

    def test_security_figures_add_up_merged_counts(self):
        for _ in range(3):
            self.client().get("/api/me", token="zpm_nope", remote_addr="198.51.100.9")
        self.client().get("/api/me", token=None, remote_addr="198.51.100.10")
        for _ in range(2):
            self.client("bob").get("/api/audit")
        s = self.ok(self.admin.get("/api/audit/summary"))["security"]
        self.assertEqual((s["failed_sign_ins"], s["permission_denials"], s["errors"]), (4, 2, 0))
        self.assertEqual(s["top_failing_addresses"][0], {"ip": "198.51.100.9", "attempts": 3})

    def test_window_is_validated(self):
        for bad in ("0", "91", "x"):
            self.assertEqual(self.admin.get("/api/audit/summary", query={"days": bad}).status, 422, bad)


class ExportTests(AuditBase):
    def rows(self, **query):
        r = self.admin.get("/api/audit/export.csv", query=query)
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/csv"))
        self.assertIn('filename="audit-log.csv"', r.headers["Content-Disposition"])
        return r, list(csv.DictReader(io.StringIO(r.text.lstrip("\ufeff"))))

    def test_columns_values_and_filters(self):
        self.make_project("WEB", members=("bob",))
        self.make_task("WEB", "Exported", as_user="bob")
        _, rows = self.rows(category="task")
        self.assertEqual(list(rows[0]), audit.EXPORT_COLUMNS)
        [row] = rows
        self.assertEqual((row["action"], row["actor"], row["project"], row["task"], row["category"], row["severity"]), ("task.created", "bob", "WEB", "WEB-1", "task", "info"))
        self.assertEqual((row["ip"], row["time_utc"]), ("203.0.113.7", "2026-10-16T09:00:00Z"))
        self.assertEqual(json.loads(row["details"])["title"], "Exported")
        _, system_rows = self.rows(actor="system")
        self.assertTrue(system_rows and all(r["actor"] == "system" for r in system_rows))

    def test_it_is_a_file_with_a_bom_and_obeys_the_row_limit(self):
        self.make_project("WEB")
        for i in range(4):
            self.make_task("WEB", "t%d" % i)
        with mock.patch.object(audit, "EXPORT_LIMIT", 3):
            r, rows = self.rows()
        self.assertTrue(r.body.startswith(b"\xef\xbb\xbf"))
        self.assertEqual((len(rows), r.headers.get("X-Export-Truncated")), (3, "true"))
        r, rows = self.rows(category="task")
        self.assertIsNone(r.headers.get("X-Export-Truncated"))

    def test_spreadsheet_formulas_are_neutralised(self):
        self.assertEqual(audit.clean("=cmd"), "=cmd")  # cleaning is about control characters, not formulas ...
        from zylker_pm.services.csvio import _safe

        self.assertEqual(_safe("=HYPERLINK(\"http://evil\")"), "'=HYPERLINK(\"http://evil\")")  # ... the exporter handles those
        self.client().get("/api/me", token="zpm_nope", headers={"User-Agent": "=cmd|' /C calc'!A0"})
        _, rows = self.rows(action="auth.failed")
        self.assertNotIn("\n=cmd", self.admin.get("/api/audit/export.csv").text)
        self.assertIn("cmd", rows[0]["details"])


class SeedTests(AuditBase):
    def test_the_demo_dataset_gives_the_monitor_something_to_show(self):
        from zylker_pm.seed import seed_demo

        init_db(os.path.join(self.tmp, "demo.db"))
        app = App(Config(db_path=os.path.join(self.tmp, "demo.db")), clock=lambda: self.now)
        tokens = seed_demo(app)
        admin_cfg = Client(app, tokens["alice"])
        self.assertEqual(admin_cfg.get("/api/audit").status, 403)  # alice is a manager, not an admin
        with app.session() as ctx:
            from zylker_pm.services import users

            _, token = users.create_user(ctx, {"username": "root", "display_name": "Root", "role": "admin"})
        root = Client(app, token)
        summary = self.ok(root.get("/api/audit/summary", query={"days": 90}))
        self.assertGreater(summary["total"], 50)
        self.assertEqual(summary["security"]["failed_sign_ins"], 0)
