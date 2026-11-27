"""Calendar feeds: management, privacy of the address, and exactly what the feed contains."""

import hashlib
import json
import os
import random
import sqlite3
import threading
import time
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

import icalcheck
from helpers import FIXED_NOW, ApiTestCase

from zylker_pm.app import App
from zylker_pm.config import Config
from zylker_pm.notify import RecordingNotifier
from zylker_pm.services import calendar_feeds

TODAY = FIXED_NOW.date()  # 2026-10-16, a Friday


def d(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


def ics_day(offset):
    return d(offset).replace("-", "")


class FeedCase(ApiTestCase):
    def feed(self, user="bob", **body):
        payload = {"scope": "mine"}
        payload.update(body)
        return self.ok(self.client(user).post("/api/calendar/feeds", payload), 201)

    def fetch(self, feed, method="GET", client=None):
        return (client or self.client()).request(method, feed["path"])

    def events(self, feed):
        response = self.fetch(feed)
        self.assertEqual(response.status, 200, response.text)
        return icalcheck.events(response.text)

    def by_uid(self, feed):
        return {e["UID"]: e for e in self.events(feed)}

    def keys(self, feed):
        """Task keys (WEB-1) shown in a feed, in the order of the file."""
        return [e["UID"].split("@")[0][len("task-"):] for e in self.events(feed) if e["UID"].startswith("task-")]

    def db(self):
        conn = sqlite3.connect(self.config.db_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def user_id(self, username):
        return next(u["id"] for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == username)


class ManagementTests(FeedCase):
    def test_creating_a_feed_returns_its_address_once(self):
        r = self.client("bob").post("/api/calendar/feeds", {"scope": "mine"})
        feed = self.ok(r, 201)
        self.assertTrue(feed["token"].startswith("zcf_") and len(feed["token"]) == 4 + 43)
        self.assertEqual(feed["path"], "/api/calendar/%s.ics" % feed["token"])
        self.assertIsNone(feed["url"])  # no ZYLKER_PUBLIC_URL in this configuration
        self.assertEqual(
            {k: feed[k] for k in ("name", "scope", "project", "include_done", "include_milestones", "use_count", "last_used_at", "rotated_at")},
            {"name": "My tasks", "scope": "mine", "project": None, "include_done": False, "include_milestones": False, "use_count": 0, "last_used_at": None, "rotated_at": None},
        )
        self.assertEqual(feed["token_hint"], feed["token"][-4:])
        self.assertEqual(feed["created_at"], "2026-10-16T09:00:00Z")

    def test_the_list_never_contains_an_address(self):
        feed = self.feed()
        listing = self.ok(self.client("bob").get("/api/calendar/feeds"))
        self.assertEqual(len(listing["items"]), 1)
        for text in (json.dumps(listing),):
            self.assertNotIn(feed["token"], text)
            self.assertNotIn("zcf_", text)
        for hidden in ("token", "path", "url"):
            self.assertNotIn(hidden, listing["items"][0])
        self.assertEqual(listing["limits"], {"max_feeds": 10, "past_days": 60, "future_days": 365, "max_events": 5000})

    def test_the_database_keeps_only_a_hash(self):
        feed = self.feed()
        row = self.db().execute("SELECT * FROM calendar_feeds").fetchone()
        self.assertEqual(row["token_hash"], hashlib.sha256(feed["token"].encode()).hexdigest())
        self.assertNotIn(feed["token"], " ".join(str(v) for v in tuple(row)))
        self.assertEqual(row["token_hint"], feed["token"][-4:])

    def test_every_role_can_have_feeds(self):
        for user in ("vera", "bob", "alice", "admin"):
            self.assertEqual(self.feed(user)["scope"], "mine")

    def test_names_and_defaults(self):
        self.make_project("WEB")
        self.assertEqual(self.feed(scope="project", project="web")["name"], "Project WEB")  # the key is not case sensitive
        self.assertEqual(self.feed(scope="mine", name="  Work  ")["name"], "Work")
        self.assertEqual(self.feed(scope="mine", name="\u65e5\u672c\u8a9e \U0001f642")["name"], "\u65e5\u672c\u8a9e \U0001f642")

    def test_the_default_name_of_a_project_feed_always_fits(self):
        self.make_project("LONG", name="L" * 100)
        self.assertEqual(self.feed(scope="project", project="LONG")["name"], "L" * 60)
        self.make_project("ODD")
        conn = self.db()
        conn.execute("UPDATE projects SET name = ? WHERE key = 'ODD'", ("\u202eSneaky\x07 name",))  # data that predates a stricter rule
        conn.commit()
        self.assertEqual(self.feed(scope="project", project="ODD")["name"], "Sneaky name")
        conn.execute("UPDATE projects SET name = ? WHERE key = 'ODD'", ("\u202e\x07",))
        conn.commit()
        self.assertEqual(self.feed(scope="project", project="ODD")["name"], "ODD")  # nothing left of the name: the key will do

    def test_a_project_feed_remembers_its_options(self):
        self.make_project("WEB")
        feed = self.feed(scope="project", project="WEB", include_done=True, include_milestones=False)
        self.assertEqual((feed["project"], feed["include_done"], feed["include_milestones"]), ("WEB", True, False))
        self.assertTrue(self.feed(scope="project", project="WEB")["include_milestones"])  # milestones are on unless switched off

    def test_validation(self):
        self.make_project("WEB")
        cases = [
            ({}, "scope", "required"),
            ({"scope": "everything"}, "scope", "must be one of"),
            ({"scope": "project"}, "project", "required"),
            ({"scope": "project", "project": None}, "project", "must not be null"),
            ({"scope": "project", "project": 12}, "project", "must be a string"),
            ({"scope": "mine", "project": "WEB"}, "project", "only applies to a project feed"),
            ({"scope": "mine", "include_milestones": True}, "include_milestones", "only applies to a project feed"),
            ({"scope": "mine", "name": ""}, "name", "must not be empty"),
            ({"scope": "mine", "name": "   "}, "name", "must not be empty"),
            ({"scope": "mine", "name": "x" * 61}, "name", "at most 60"),
            ({"scope": "mine", "name": "bad\u202ename"}, "name", "direction-override"),
            ({"scope": "mine", "name": "tab\tname"}, "name", "control"),
            ({"scope": "mine", "name": "line\nbreak"}, "name", "control"),
            ({"scope": "mine", "name": 5}, "name", "must be a string"),
            ({"scope": "mine", "include_done": "yes"}, "include_done", "must be true or false"),
            ({"scope": "mine", "include_done": 1}, "include_done", "must be true or false"),
            ({"scope": "mine", "colour": "red"}, "colour", "unknown field"),
        ]
        for body, field, fragment in cases:
            r = self.client("bob").post("/api/calendar/feeds", body)
            self.assertEqual(r.status, 422, (body, r.text))
            self.assertIn(fragment, r.json["error"]["details"][field], body)
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"], [])  # nothing half-created

    def test_a_body_that_is_not_an_object_or_not_json_is_refused(self):
        self.assertEqual(self.client("bob").post("/api/calendar/feeds", []).status, 400)
        r = self.client("bob").request("POST", "/api/calendar/feeds", raw=b"scope=mine", content_type="text/plain")
        self.assertEqual(r.status, 415)

    def test_an_unknown_project_is_404(self):
        r = self.client("bob").post("/api/calendar/feeds", {"scope": "project", "project": "NOPE"})
        self.assertEqual((r.status, r.error_code), (404, "project_not_found"))

    def test_a_project_feed_can_be_made_for_an_archived_project(self):
        self.make_project("OLD")
        self.ok(self.client("alice").patch("/api/projects/OLD", {"status": "archived"}))
        self.assertEqual(self.feed(scope="project", project="OLD")["project"], "OLD")

    def test_at_most_ten_feeds_per_person(self):
        for i in range(10):
            self.feed(name="Feed %d" % i)
        r = self.client("bob").post("/api/calendar/feeds", {"scope": "mine"})
        self.assertEqual((r.status, r.error_code), (409, "feed_limit"))
        self.assertEqual(self.feed("carol")["scope"], "mine")  # the limit is per person
        first = self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]
        self.assertEqual(self.client("bob").delete("/api/calendar/feeds/%d" % first["id"]).status, 204)
        self.assertEqual(self.feed()["scope"], "mine")

    def test_feeds_are_private_to_their_owner(self):
        feed = self.feed("bob")
        for user in ("alice", "admin", "vera", "carol"):
            self.assertEqual(self.ok(self.client(user).get("/api/calendar/feeds"))["items"], [], user)
            for method, path in (("POST", "/api/calendar/feeds/%d/rotate"), ("DELETE", "/api/calendar/feeds/%d")):
                r = self.client(user).request(method, path % feed["id"])
                self.assertEqual((r.status, r.error_code), (404, "feed_not_found"), (user, method))
        self.assertEqual(self.fetch(feed).status, 200)  # and none of that harmed it
        self.assertEqual(len(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"]), 1)

    def test_bad_ids_are_404_never_500(self):
        for bad in ("abc", "0", "-5", "1.5", "99999999999999999999", str(2**63), "%20"):
            for method, path in (("POST", "/api/calendar/feeds/%s/rotate"), ("DELETE", "/api/calendar/feeds/%s")):
                r = self.client("bob").request(method, path % bad)
                self.assertEqual((r.status, r.error_code), (404, "feed_not_found"), (method, bad))

    def test_management_needs_a_token_and_an_address_is_not_a_token(self):
        feed = self.feed()
        for method, path in (("GET", "/api/calendar/feeds"), ("POST", "/api/calendar/feeds"), ("POST", "/api/calendar/feeds/1/rotate"), ("DELETE", "/api/calendar/feeds/1")):
            self.assertEqual(self.client().request(method, path).status, 401, (method, path))
        self.assertEqual(self.client(token=feed["token"]).get("/api/me").status, 401)
        self.assertEqual(self.client(token=feed["token"]).get("/api/calendar/feeds").status, 401)

    def test_replacing_the_address(self):
        feed = self.feed()
        self.assertEqual(self.fetch(feed).status, 200)
        self.advance_days(1)
        new = self.ok(self.client("bob").post("/api/calendar/feeds/%d/rotate" % feed["id"]))
        self.assertNotEqual(new["token"], feed["token"])
        self.assertEqual(self.fetch(feed).status, 404)  # the old address is dead at once
        self.assertEqual(self.fetch(new).status, 200)
        listed = self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]
        self.assertEqual((listed["id"], listed["token_hint"]), (feed["id"], new["token"][-4:]))
        self.assertEqual(listed["rotated_at"], "2026-10-17T09:00:00Z")
        self.assertEqual((listed["use_count"], listed["name"]), (1, "My tasks"))  # only the fetch of the new address counts
        again = self.ok(self.client("bob").post("/api/calendar/feeds/%d/rotate" % feed["id"]))
        self.assertEqual(self.fetch(new).status, 404)
        self.assertEqual((again["use_count"], again["last_used_at"]), (0, None))

    def test_deleting_a_feed(self):
        feed = self.feed()
        self.assertEqual(self.client("bob").delete("/api/calendar/feeds/%d" % feed["id"]).status, 204)
        self.assertEqual(self.fetch(feed).status, 404)
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"], [])
        self.assertEqual(self.client("bob").delete("/api/calendar/feeds/%d" % feed["id"]).status, 404)

    def test_a_deactivated_owner_stops_the_feed_and_a_reactivated_one_restores_it(self):
        feed = self.feed("bob")
        bob = self.user_id("bob")
        self.ok(self.admin.patch("/api/users/%d" % bob, {"active": False}))
        self.assertEqual(self.fetch(feed).status, 404)
        self.ok(self.admin.patch("/api/users/%d" % bob, {"active": True}))
        self.assertEqual(self.fetch(feed).status, 200)
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]["use_count"], 1)  # the refused fetch was not counted

    def test_deleting_a_project_removes_the_feeds_that_followed_it(self):
        self.make_project("WEB")
        feed = self.feed(scope="project", project="WEB")
        mine = self.feed()
        self.assertEqual(self.admin.delete("/api/projects/WEB", query={"confirm": "WEB"}).status, 204)
        self.assertEqual(self.fetch(feed).status, 404)
        self.assertEqual([f["id"] for f in self.ok(self.client("bob").get("/api/calendar/feeds"))["items"]], [mine["id"]])

    def test_changes_are_recorded_for_admins_without_the_address(self):
        self.make_project("WEB")
        feed = self.feed(scope="project", project="WEB", name="Web dates")
        new = self.ok(self.client("bob").post("/api/calendar/feeds/%d/rotate" % feed["id"]))
        self.client("bob").delete("/api/calendar/feeds/%d" % feed["id"])
        items = self.ok(self.admin.get("/api/activity"))["items"]
        wanted = [i for i in items if i["action"].startswith("calendar.")]
        self.assertEqual([i["action"] for i in reversed(wanted)], ["calendar.feed_created", "calendar.feed_rotated", "calendar.feed_deleted"])
        self.assertEqual({i["actor"]["username"] for i in wanted}, {"bob"})
        self.assertEqual(wanted[-1]["details"], {"feed": "Web dates", "scope": "project", "project": "WEB"})
        text = json.dumps(items)
        self.assertNotIn(feed["token"], text)
        self.assertNotIn(new["token"], text)
        self.assertEqual(self.ok(self.client("alice").get("/api/projects/WEB/activity"))["items"][0]["action"].startswith("calendar."), False)

    def test_the_table_is_created_for_databases_that_predate_it(self):
        conn = self.db()
        conn.execute("DROP TABLE calendar_feeds")
        conn.commit()
        App(self.config)  # starting the application is what creates it
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"], [])
        self.assertEqual(self.feed()["scope"], "mine")

    def test_a_missing_table_is_explained_and_creates_no_500(self):
        conn = self.db()
        conn.execute("DROP TABLE calendar_feeds")
        conn.commit()
        r = self.client("bob").get("/api/calendar/feeds")
        self.assertEqual((r.status, r.error_code), (409, "calendar_not_initialised"))
        self.assertEqual(self.client("bob").post("/api/calendar/feeds", {"scope": "mine"}).status, 409)
        self.assertEqual(self.client().get("/api/calendar/zcf_whatever.ics").status, 404)  # no table, no feeds
        calendar_feeds.ensure_schema(self.config.db_path)
        self.assertEqual(self.client("bob").get("/api/calendar/feeds").status, 200)

    def test_ensure_schema_leaves_in_memory_and_missing_databases_alone(self):
        calendar_feeds.ensure_schema(":memory:")
        missing = os.path.join(self.tmp, "does-not-exist.db")
        calendar_feeds.ensure_schema(missing)
        self.assertFalse(os.path.exists(missing))

    def test_the_database_itself_refuses_nonsense(self):
        self.make_project("WEB")
        conn = self.db()
        uid = self.user_id("bob")
        pid = conn.execute("SELECT id FROM projects").fetchone()[0]
        base = "INSERT INTO calendar_feeds (user_id, name, scope, project_id, include_done, include_milestones, token_hash, token_hint, created_at) VALUES "
        bad = [
            "(%d, 'x', 'project', NULL, 0, 0, 'h1', 'abcd', 't')" % uid,  # a project feed without a project
            "(%d, 'x', 'mine', %d, 0, 0, 'h2', 'abcd', 't')" % (uid, pid),  # a personal feed with one
            "(%d, 'x', 'mine', NULL, 0, 1, 'h3', 'abcd', 't')" % uid,  # milestones on a personal feed
            "(%d, 'x', 'team', NULL, 0, 0, 'h4', 'abcd', 't')" % uid,  # an unknown scope
            "(%d, '', 'mine', NULL, 0, 0, 'h5', 'abcd', 't')" % uid,  # no name
            "(%d, '%s', 'mine', NULL, 0, 0, 'h6', 'abcd', 't')" % (uid, "x" * 61),  # a name that is too long
            "(%d, 'x', 'mine', NULL, 2, 0, 'h7', 'abcd', 't')" % uid,  # not a boolean
        ]
        for values in bad:
            with self.assertRaises(sqlite3.IntegrityError, msg=values):
                conn.execute(base + values)
        conn.execute(base + "(%d, 'x', 'mine', NULL, 0, 0, 'same', 'abcd', 't')" % uid)
        with self.assertRaises(sqlite3.IntegrityError):  # two feeds can never share an address
            conn.execute(base + "(%d, 'y', 'mine', NULL, 0, 0, 'same', 'abcd', 't')" % uid)


class FetchTests(FeedCase):
    def test_the_address_needs_no_authorization_header(self):
        feed = self.feed()
        r = self.client().request("GET", feed["path"])  # no Authorization header at all
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "text/calendar; charset=utf-8")
        self.assertEqual(r.headers["Content-Disposition"], 'inline; filename="zylker-calendar.ics"')
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")
        icalcheck.parse(r.text)

    def test_a_bad_authorization_header_does_not_matter_either(self):
        feed = self.feed()
        self.assertEqual(self.client(token="zpm_not-a-real-token").request("GET", feed["path"]).status, 200)

    def test_head_gives_the_headers_and_no_body(self):
        feed = self.feed()
        r = self.fetch(feed, "HEAD")
        self.assertEqual((r.status, r.body, r.headers["Content-Type"]), (200, b"", "text/calendar; charset=utf-8"))

    def test_other_methods_are_refused(self):
        feed = self.feed()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            r = self.fetch(feed, method)
            self.assertEqual((r.status, r.headers["Allow"]), (405, "GET"), method)

    def test_unknown_addresses_are_all_the_same_404(self):
        self.feed()
        for path in ("/api/calendar/zcf_" + "A" * 43 + ".ics", "/api/calendar/x.ics", "/api/calendar/feeds.ics", "/api/calendar/%s.ics" % ("z" * 10000), "/api/calendar/zcf_%C3%A9.ics"):
            r = self.client().get(path)
            self.assertEqual((r.status, r.error_code), (404, "feed_not_found"), path[:60])
            self.assertEqual(r.headers["Content-Type"], "application/json; charset=utf-8")
        for path in ("/api/calendar/zcf_x", "/api/calendar/.ics", "/api/calendar/a/b.ics"):  # not feed routes at all
            self.assertEqual(self.client().get(path).status, 404, path)

    def test_every_successful_fetch_is_counted(self):
        feed = self.feed()
        listed = lambda: self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]  # noqa: E731
        self.assertEqual((listed()["use_count"], listed()["last_used_at"]), (0, None))
        self.fetch(feed)
        self.advance_days(1)
        self.fetch(feed)
        self.fetch(feed, "HEAD")
        self.client().get("/api/calendar/zcf_" + "A" * 43 + ".ics")  # a failed fetch counts for nobody
        self.assertEqual((listed()["use_count"], listed()["last_used_at"]), (3, "2026-10-17T09:00:00Z"))

    def test_an_unknown_address_never_waits_for_the_write_lock(self):
        # Anybody can send an unknown address, so it must not queue behind (or hold up) the writers of the database.
        holder = self.db()
        holder.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            r = self.client().get("/api/calendar/zcf_%s.ics" % ("C" * 43))
            elapsed = time.monotonic() - started
        finally:
            holder.execute("ROLLBACK")
        self.assertEqual((r.status, r.error_code), (404, "feed_not_found"))
        self.assertLess(elapsed, 2.0)

    def test_failing_to_count_a_fetch_does_not_fail_the_feed(self):
        feed = self.feed()

        def locked(path):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(calendar_feeds, "connect", side_effect=locked):
            with self.assertLogs("zylker.calendar", level="WARNING") as logged:
                r = self.fetch(feed)
        self.assertEqual(r.status, 200)
        icalcheck.parse(r.text)
        self.assertEqual(len(logged.output), 1)
        self.assertIn("could not record a calendar feed fetch (OperationalError)", logged.output[0])
        self.assertNotIn(feed["token"], logged.output[0])
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]["use_count"], 0)

    def test_a_fetch_counts_only_for_the_address_that_was_read(self):
        feed = self.feed()
        ctx = SimpleNamespace(config=self.config, now_iso="2026-10-16T09:00:00Z")
        calendar_feeds._record_fetch(ctx, feed["id"], hashlib.sha256(b"an older address").hexdigest())  # replaced in the meantime
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]["use_count"], 0)
        calendar_feeds._record_fetch(ctx, feed["id"], hashlib.sha256(feed["token"].encode()).hexdigest())
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]["use_count"], 1)

    def test_the_address_is_not_written_to_the_access_log(self):
        feed = self.feed()
        with self.assertLogs("zylker.access", level="INFO") as logged:
            self.fetch(feed)
            self.client().get("/api/calendar/zcf_%s.ics" % ("B" * 43))
        text = "\n".join(logged.output)
        self.assertNotIn(feed["token"], text)
        self.assertNotIn("B" * 20, text)
        self.assertIn("GET /api/calendar/<feed>.ics -> 200", text)
        self.assertIn("GET /api/calendar/<feed>.ics -> 404", text)

    def test_nor_to_the_error_log(self):
        feed = self.feed()
        with mock.patch.object(calendar_feeds, "render_feed", side_effect=RuntimeError("boom")):
            with self.assertLogs("zylker.access", level="ERROR") as logged:
                r = self.fetch(feed)
        self.assertEqual(r.status, 500)
        self.assertNotIn(feed["token"], "\n".join(logged.output))
        self.assertIn("/api/calendar/<feed>.ics", "\n".join(logged.output))
        self.assertNotIn(feed["token"], r.text)

    def test_other_paths_are_logged_as_before(self):
        with self.assertLogs("zylker.access", level="INFO") as logged:
            self.client("bob").get("/api/calendar/feeds")
        self.assertIn("GET /api/calendar/feeds -> 200", "\n".join(logged.output))

    def test_parallel_fetches_and_writes_all_succeed_and_are_all_counted(self):
        self.make_project("WEB")
        feed = self.feed()
        statuses, lock = [], threading.Lock()

        def reader():
            client = self.client()
            for _ in range(12):
                r = client.get(feed["path"])
                with lock:
                    statuses.append(r.status)

        def writer():
            client = self.client("alice")
            for i in range(12):
                r = client.post("/api/projects/WEB/tasks", {"title": "Extra %d" % i, "assignee": "bob", "due_date": d(3)})
                with lock:
                    statuses.append(r.status)

        threads = [threading.Thread(target=reader) for _ in range(6)] + [threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(set(statuses)), [200, 201])
        self.assertEqual(statuses.count(200), 72)
        self.assertEqual(self.ok(self.client("bob").get("/api/calendar/feeds"))["items"][0]["use_count"], 72)
        self.assertEqual(len(self.keys(feed)), 12)


class ContentTests(FeedCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB")
        self.make_task("WEB", "Homepage", assignee="bob", start_date=d(3), due_date=d(7), estimate_hours=8, priority="high")  # WEB-1
        self.make_task("WEB", "Due only, with comma; semi", assignee="bob", due_date=d(4))  # WEB-2
        self.make_task("WEB", "Start only", assignee="bob", start_date=d(5))  # WEB-3
        self.make_task("WEB", "No dates", assignee="bob")  # WEB-4
        self.make_task("WEB", "Already done", assignee="bob", due_date=d(-1))  # WEB-5
        self.make_task("WEB", "Cancelled", assignee="bob", due_date=d(6))  # WEB-6
        self.make_task("WEB", "Carols task", assignee="carol", due_date=d(6))  # WEB-7
        self.make_task("WEB", "Unassigned", due_date=d(9))  # WEB-8
        self.make_task("WEB", "In review", assignee="bob", due_date=d(14))  # WEB-9
        self.make_task("WEB", "Blocked", assignee="bob", due_date=d(15))  # WEB-10
        self.ok(self.set_status("WEB-5", "done"))
        self.ok(self.set_status("WEB-6", "cancelled"))
        self.ok(self.set_status("WEB-9", "in_progress"))
        self.ok(self.set_status("WEB-9", "in_review"))
        self.ok(self.set_status("WEB-10", "blocked"))

    def test_my_tasks_are_the_open_dated_tasks_assigned_to_me(self):
        feed = self.feed("bob")
        self.assertEqual(self.keys(feed), ["WEB-1", "WEB-2", "WEB-3", "WEB-9", "WEB-10"])  # no dates, done, cancelled, carol's, unassigned
        self.assertEqual(self.keys(self.feed("carol")), ["WEB-7"])
        self.assertEqual(self.keys(self.feed("vera")), [])

    def test_a_task_with_two_dates_is_one_event_across_them(self):
        e = self.by_uid(self.feed("bob"))["task-WEB-1@zylker-pm"]
        self.assertEqual((e["SUMMARY"], e["STATUS"], e["TRANSP"]), ("WEB-1 Homepage", "CONFIRMED", "TRANSPARENT"))
        self.assertEqual((e["DTSTART"], e["DTEND"]), (ics_day(3), ics_day(8)))  # DTEND is the day after the due date
        self.assertEqual(e["_params"]["DTSTART"], {"VALUE": "DATE"})
        self.assertEqual(e["_params"]["DTEND"], {"VALUE": "DATE"})
        self.assertEqual(e["DESCRIPTION"], "Project: Project WEB (WEB)\nStatus: To do\nPriority: High\nEstimate: 8 h")
        self.assertNotIn("URL", e)

    def test_a_task_with_one_date_is_a_one_day_event_that_says_which(self):
        found = self.by_uid(self.feed("bob"))
        due, start = found["task-WEB-2@zylker-pm"], found["task-WEB-3@zylker-pm"]
        self.assertEqual((due["SUMMARY"], due["DTSTART"], due["DTEND"]), ("WEB-2 Due only, with comma; semi (due)", ics_day(4), ics_day(5)))
        self.assertEqual((start["SUMMARY"], start["DTSTART"], start["DTEND"]), ("WEB-3 Start only (starts)", ics_day(5), ics_day(6)))

    def test_status_and_priority_are_written_for_people(self):
        found = self.by_uid(self.feed("bob"))
        self.assertIn("Status: In review\nPriority: Medium", found["task-WEB-9@zylker-pm"]["DESCRIPTION"])
        self.assertIn("Status: Blocked", found["task-WEB-10@zylker-pm"]["DESCRIPTION"])
        self.assertNotIn("Estimate", found["task-WEB-2@zylker-pm"]["DESCRIPTION"])  # none was given

    def test_events_come_in_calendar_order(self):
        self.assertEqual(self.keys(self.feed("bob")), ["WEB-1", "WEB-2", "WEB-3", "WEB-9", "WEB-10"])
        self.make_task("WEB", "Earlier", assignee="bob", due_date=d(1))  # WEB-11
        self.make_task("WEB", "Same day, later key", assignee="bob", start_date=d(3), due_date=d(7))  # WEB-12: same days as WEB-1
        self.assertEqual(self.keys(self.feed("bob")), ["WEB-11", "WEB-1", "WEB-12", "WEB-2", "WEB-3", "WEB-9", "WEB-10"])

    def test_finished_tasks_only_when_asked_and_cancelled_never(self):
        plain, with_done = self.feed("bob"), self.feed("bob", scope="mine", include_done=True)
        self.assertNotIn("WEB-5", self.keys(plain))
        self.assertEqual(self.keys(with_done), ["WEB-5", "WEB-1", "WEB-2", "WEB-3", "WEB-9", "WEB-10"])
        done = self.by_uid(with_done)["task-WEB-5@zylker-pm"]
        self.assertEqual(done["SUMMARY"], "\u2713 WEB-5 Already done (due)")
        self.assertIn("Status: Done", done["DESCRIPTION"])
        self.assertNotIn("WEB-6", self.keys(with_done))

    def test_a_project_feed_shows_every_assignee_and_names_them(self):
        feed = self.feed("alice", scope="project", project="WEB", include_milestones=False)
        self.assertEqual(self.keys(feed), ["WEB-1", "WEB-2", "WEB-3", "WEB-7", "WEB-8", "WEB-9", "WEB-10"])
        found = self.by_uid(feed)
        self.assertIn("Assignee: Bob", found["task-WEB-1@zylker-pm"]["DESCRIPTION"])
        self.assertIn("Assignee: Carol", found["task-WEB-7@zylker-pm"]["DESCRIPTION"])
        self.assertNotIn("Assignee", found["task-WEB-8@zylker-pm"]["DESCRIPTION"])  # nobody is assigned

    def test_a_personal_feed_does_not_name_anybody(self):
        text = self.fetch(self.feed("bob")).text
        for other in ("Assignee", "Carol", "Alice"):
            self.assertNotIn(other, text)
        self.assertIn("Open tasks assigned to Bob", text)  # only the calendar's own description names its owner

    def test_only_the_chosen_project_is_in_a_project_feed(self):
        self.make_project("OTH")
        self.make_task("OTH", "Elsewhere", assignee="bob", due_date=d(2))
        self.assertEqual(self.keys(self.feed("bob", scope="project", project="OTH")), ["OTH-1"])
        self.assertEqual(self.keys(self.feed("bob", scope="project", project="WEB", include_milestones=False))[:1], ["WEB-1"])
        self.assertIn("OTH-1", self.keys(self.feed("bob")))  # my tasks span every project

    def test_tasks_of_an_archived_project_leave_my_tasks_but_not_the_project_feed(self):
        self.make_project("OLD")
        self.make_task("OLD", "Old news", assignee="bob", due_date=d(2))
        mine, project = self.feed("bob"), self.feed("bob", scope="project", project="OLD")
        self.assertIn("OLD-1", self.keys(mine))
        self.ok(self.client("alice").patch("/api/projects/OLD", {"status": "archived"}))
        self.assertNotIn("OLD-1", self.keys(mine))
        self.assertEqual(self.keys(project), ["OLD-1"])

    def test_the_window_is_sixty_days_back_and_a_year_ahead(self):
        cases = {
            "due on the first day": (None, d(-60), True),
            "due the day before": (None, d(-61), False),
            "starts on the last day": (d(365), None, True),
            "starts the day after": (d(366), None, False),
            "ends on the first day": (d(-200), d(-60), True),
            "ends the day before": (d(-200), d(-61), False),
            "starts on the last day, ends later": (d(365), d(400), True),
            "starts the day after, ends later": (d(366), d(400), False),
            "spans the whole window": (d(-100), d(500), True),
        }
        keys = {}
        for name, (start, due, _) in cases.items():
            fields = {k: v for k, v in (("start_date", start), ("due_date", due)) if v}
            keys[name] = self.make_task("WEB", name, assignee="carol", **fields)["key"]
        shown = self.keys(self.feed("carol"))
        for name, (_, _, expected) in cases.items():
            self.assertEqual(keys[name] in shown, expected, name)
        span = self.by_uid(self.feed("carol"))["task-%s@zylker-pm" % keys["spans the whole window"]]
        self.assertEqual((span["DTSTART"], span["DTEND"]), (ics_day(-100), ics_day(501)))  # the whole task, not just the window

    def test_the_window_follows_the_organisations_day(self):
        # 22:00 UTC on 16 October is already 17 October in Kolkata, so the window starts a day later there.
        late = FIXED_NOW.replace(hour=22)
        india = App(Config(db_path=self.config.db_path, timezone="Asia/Kolkata"), clock=lambda: late, notifier=RecordingNotifier())
        edge = self.make_task("WEB", "Edge", assignee="carol", due_date=d(-60))["key"]  # 17 Aug
        fine = self.make_task("WEB", "Fine", assignee="carol", due_date=d(-59))["key"]  # 18 Aug
        utc_feed = self.feed("carol")
        self.assertEqual({edge, fine} - set(self.keys(utc_feed)), set())
        from helpers import Client

        india_client = Client(india, None)
        shown = [e["UID"] for e in icalcheck.events(india_client.get(utc_feed["path"]).text)]
        self.assertNotIn("task-%s@zylker-pm" % edge, shown)
        self.assertIn("task-%s@zylker-pm" % fine, shown)

    def test_milestones_are_in_project_feeds_unless_switched_off(self):
        beta = self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "Beta launch", "due_date": d(12)}), 201)
        late = self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "Overdue, really", "due_date": d(-6)}), 201)
        achieved = self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "Done one", "due_date": d(17)}), 201)
        self.ok(self.client("alice").patch("/api/tasks/WEB-5", {"milestone": achieved["id"]}))  # WEB-5 is done
        for name, due in (("Ancient", d(-61)), ("Far away", d(366)), ("Edge in", d(-60)), ("Edge out", d(365))):
            self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": name, "due_date": due}), 201)
        feed = self.feed("alice", scope="project", project="WEB")
        found = self.by_uid(feed)
        summaries = {e["UID"]: e["SUMMARY"] for e in found.values() if e["UID"].startswith("milestone-")}
        self.assertEqual(
            sorted(summaries.values()),
            sorted(["Milestone: Beta launch", "Milestone: Overdue, really", "\u2713 Milestone: Done one", "Milestone: Edge in", "Milestone: Edge out"]),
        )
        e = found["milestone-%d@zylker-pm" % beta["id"]]
        self.assertEqual((e["DTSTART"], e["DTEND"], e["TRANSP"]), (ics_day(12), ics_day(13), "TRANSPARENT"))
        self.assertEqual(e["DESCRIPTION"], "Project: Project WEB (WEB)\nMilestone: open\nTasks done: 0 of 0")
        self.assertIn("Milestone: overdue", found["milestone-%d@zylker-pm" % late["id"]]["DESCRIPTION"])
        self.assertEqual(found["milestone-%d@zylker-pm" % achieved["id"]]["DESCRIPTION"], "Project: Project WEB (WEB)\nMilestone: achieved\nTasks done: 1 of 1")
        starts = [e["DTSTART"] for e in self.events(feed)]
        self.assertEqual(starts, sorted(starts))  # tasks and milestones share one calendar order
        without = self.feed("alice", scope="project", project="WEB", include_milestones=False)
        self.assertEqual([k for k in self.by_uid(without) if k.startswith("milestone-")], [])
        self.assertEqual([k for k in self.by_uid(self.feed("alice")) if k.startswith("milestone-")], [])  # my tasks never has them

    def test_nothing_private_leaves_through_the_feed(self):
        bob = self.user_id("bob")
        self.ok(self.admin.patch("/api/users/%d" % bob, {"email": "bob-private@example.com"}))
        self.ok(self.client("alice").patch("/api/tasks/WEB-1", {"description": "SENTINEL-DESCRIPTION", "labels": ["sentinel-label"], "external_ref": "SENTINEL-EXTREF"}))
        self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "SENTINEL-COMMENT"}), 201)
        self.ok(self.client("bob").post("/api/tasks/WEB-1/time", {"hours": 3, "note": "SENTINEL-NOTE"}), 201)
        for feed in (self.feed("bob"), self.feed("alice", scope="project", project="WEB")):
            text = self.fetch(feed).text
            for secret in ("SENTINEL", "sentinel", "bob-private", "example.com", "bob@", "3 h", "Reporter", "Alice Morgan"):
                self.assertNotIn(secret, text, secret)
            self.assertIn("WEB-1 Homepage", text)

    def test_hostile_titles_cannot_break_out_of_their_line(self):
        titles = [
            "Plan, review; ship \\ now",
            "line one\nline two",
            "evil\r\nATTENDEE:mailto:evil@example.com\r\nEND:VEVENT",
            "\u65e5\u672c\u8a9e\u306e\u30bf\u30b9\u30af \u2713 \U0001f642 " * 6,
            "x" * 200,
            "tab\there",
            "back\\nslash",
        ]
        keys = [self.make_task("WEB", t, assignee="carol", due_date=d(2 + i))["key"] for i, t in enumerate(titles)]
        found = self.by_uid(self.feed("carol"))  # parsing is strict: bare line feeds, long lines and bad escapes all fail it
        for key, title in zip(keys, titles, strict=True):
            expected = "%s %s (due)" % (key, title.strip().replace("\r\n", "\n").replace("\t", " "))
            self.assertEqual(found["task-%s@zylker-pm" % key]["SUMMARY"], expected)
        self.assertNotIn("ATTENDEE", {name for e in found.values() for name in e})

    def test_an_update_changes_only_that_events_modified_time(self):
        feed = self.feed("bob")
        before = self.by_uid(feed)
        self.assertEqual({e["LAST-MODIFIED"] for e in before.values()}, {"20261016T090000Z"})
        self.advance_days(2)
        self.ok(self.client("alice").patch("/api/tasks/WEB-2", {"title": "Renamed"}))
        after = self.by_uid(feed)
        self.assertEqual(after["task-WEB-2@zylker-pm"]["LAST-MODIFIED"], "20261018T090000Z")
        self.assertEqual(after["task-WEB-2@zylker-pm"]["DTSTAMP"], "20261018T090000Z")
        self.assertEqual(after["task-WEB-2@zylker-pm"]["SUMMARY"], "WEB-2 Renamed (due)")
        self.assertEqual({e["LAST-MODIFIED"] for u, e in after.items() if u != "task-WEB-2@zylker-pm"}, {"20261016T090000Z"})

    def test_the_same_data_gives_the_same_bytes(self):
        feed = self.feed("bob")
        first = self.fetch(feed).body
        self.advance_days(1)  # time passing alone changes nothing in the file while nothing crosses the window
        self.assertEqual(self.fetch(feed).body, first)

    def test_the_calendar_itself_is_named_and_described(self):
        calendar = icalcheck.parse(self.fetch(self.feed("bob", scope="mine", name="Bob, work")).text)
        found = icalcheck.props(calendar)
        self.assertEqual((found["NAME"]["value"], found["X-WR-CALNAME"]["value"]), ("Bob, work", "Bob, work"))
        self.assertEqual(found["X-WR-CALDESC"]["value"], "Zylker Projects. Open tasks assigned to Bob, from 60 days ago to 365 days ahead.")
        self.assertEqual((found["VERSION"]["value"], found["METHOD"]["value"], found["X-PUBLISHED-TTL"]["value"]), ("2.0", "PUBLISH", "PT1H"))
        project = icalcheck.props(icalcheck.parse(self.fetch(self.feed("bob", scope="project", project="WEB")).text))
        self.assertEqual(project["X-WR-CALDESC"]["value"], "Zylker Projects. Project WEB (WEB): tasks and milestones, from 60 days ago to 365 days ahead.")

    def test_links_back_to_the_task_when_the_public_address_is_known(self):
        public = App(Config(db_path=self.config.db_path, public_url="https://pm.example.com"), clock=lambda: FIXED_NOW, notifier=RecordingNotifier())
        from helpers import Client

        bob = Client(public, self.tokens["bob"])
        feed = self.ok(bob.post("/api/calendar/feeds", {"scope": "mine"}), 201)
        self.assertEqual(feed["url"], "https://pm.example.com" + feed["path"])
        found = {e["UID"]: e for e in icalcheck.events(Client(public, None).get(feed["path"]).text)}
        e = found["task-WEB-1@pm.example.com"]
        self.assertEqual(e["URL"], "https://pm.example.com/#/t/WEB-1")
        self.assertTrue(e["DESCRIPTION"].endswith("Open in Zylker Projects: https://pm.example.com/#/t/WEB-1"))
        self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "Beta", "due_date": d(12)}), 201)
        project = self.ok(bob.post("/api/calendar/feeds", {"scope": "project", "project": "WEB"}), 201)
        milestone = next(e for e in icalcheck.events(Client(public, None).get(project["path"]).text) if e["UID"].startswith("milestone-"))
        self.assertEqual(milestone["URL"], "https://pm.example.com/#/p/WEB/milestones")

    def test_a_feed_stops_at_the_cap_and_says_so(self):
        feed = self.feed("carol")
        for i in range(9):
            self.make_task("WEB", "More %d" % i, assignee="carol", due_date=d(10 + i))
        with mock.patch.object(calendar_feeds, "MAX_EVENTS", 5):
            calendar = icalcheck.parse(self.fetch(feed).text)
        self.assertEqual(len(calendar["children"]), 5)
        self.assertTrue(icalcheck.props(calendar)["X-WR-CALDESC"]["value"].endswith("Only the first 5 tasks are listed."))
        self.assertEqual([icalcheck.events(self.fetch(feed).text)[0]["SUMMARY"]], ["WEB-7 Carols task (due)"])  # the cap is back at 5000: nothing missing

    def test_a_row_with_unreadable_dates_is_skipped_not_fatal(self):
        feed = self.feed("bob")
        conn = self.db()
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("UPDATE tasks SET start_date = 'garbage', due_date = 'also garbage' WHERE number = 2")
        conn.commit()
        self.assertEqual(self.keys(feed), ["WEB-1", "WEB-3", "WEB-9", "WEB-10"])


class OracleTests(FeedCase):
    """The feed against a second, independent reading of the same data: the ordinary task and milestone endpoints."""

    def test_random_projects_agree_with_the_task_api(self):
        rng = random.Random(2026)
        for key in ("AAA", "BBB", "CCC"):
            self.make_project(key)
        statuses = ["todo"] * 5 + ["in_progress", "in_review", "blocked", "done", "cancelled"]
        for key in ("AAA", "BBB", "CCC"):
            for i in range(40):
                fields = {"assignee": rng.choice(["bob", "carol", None, None])}
                if rng.random() < 0.7:
                    start = rng.randint(-150, 420)
                    if rng.random() < 0.8:
                        fields["start_date"] = d(start)
                    if rng.random() < 0.8:
                        fields["due_date"] = d(start + rng.randint(0, 60))
                task = self.make_task(key, "%s task %d" % (key, i), **{k: v for k, v in fields.items() if v})
                status = rng.choice(statuses)
                if status == "in_review":
                    self.ok(self.set_status(task["key"], "in_progress"))
                if status != "todo":
                    self.ok(self.set_status(task["key"], status))
        self.ok(self.client("alice").patch("/api/projects/CCC", {"status": "archived"}))
        for key in ("AAA", "BBB", "CCC"):
            for i in range(4):
                self.ok(self.client("alice").post("/api/projects/%s/milestones" % key, {"name": "%s m%d" % (key, i), "due_date": d(rng.randint(-120, 420))}), 201) if key != "CCC" else None

        tasks = []
        for key in ("AAA", "BBB", "CCC"):
            tasks += self.ok(self.client("alice").get("/api/tasks", query={"project": key, "limit": 200}))["items"]
        self.assertEqual(len(tasks), 120)
        low, high = d(-60), d(365)

        def expected_tasks(wanted, include_done):
            out = {}
            for t in tasks:
                if not wanted(t) or t["status"] == "cancelled" or (t["status"] == "done" and not include_done):
                    continue
                days = [x for x in (t["start_date"], t["due_date"]) if x]
                if not days or max(days) < low or min(days) > high:
                    continue
                out["task-%s@zylker-pm" % t["key"]] = (min(days).replace("-", ""), (date.fromisoformat(max(days)) + timedelta(days=1)).isoformat().replace("-", ""))
            return out

        def actual(feed):
            return {e["UID"]: (e["DTSTART"], e["DTEND"]) for e in self.events(feed) if e["UID"].startswith("task-")}

        checked = 0
        for include_done in (False, True):
            for user in ("bob", "carol"):
                feed = self.feed(user, scope="mine", include_done=include_done)
                want = expected_tasks(lambda t, u=user: t["assignee"] and t["assignee"]["username"] == u and t["project"] != "CCC", include_done)
                self.assertEqual(actual(feed), want, (user, include_done))
                checked += len(want)
            for key in ("AAA", "BBB", "CCC"):
                feed = self.feed("alice", scope="project", project=key, include_done=include_done, include_milestones=True)
                want = expected_tasks(lambda t, k=key: t["project"] == k, include_done)
                self.assertEqual(actual(feed), want, (key, include_done))
                checked += len(want)
        self.assertGreater(checked, 100)  # the comparison was not between two empty sets
        for key in ("AAA", "BBB"):
            listed = self.ok(self.client("alice").get("/api/projects/%s/milestones" % key))
            items = listed["items"] if isinstance(listed, dict) else listed
            want = {"milestone-%d@zylker-pm" % m["id"]: m["due_date"].replace("-", "") for m in items if low <= m["due_date"] <= high}
            feed = self.feed("alice", scope="project", project=key)
            got = {e["UID"]: e["DTSTART"] for e in self.events(feed) if e["UID"].startswith("milestone-")}
            self.assertEqual(got, want, key)


if __name__ == "__main__":
    unittest.main()
