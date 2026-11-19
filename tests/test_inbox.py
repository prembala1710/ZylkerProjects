"""The notification inbox: who hears about what, merging, reading, watching, limits and safety."""

import os
import sqlite3
from datetime import timedelta
from unittest import mock

from helpers import ApiTestCase

from zylker_pm import db
from zylker_pm.db import init_db
from zylker_pm.services import inbox


class InboxBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.make_project("OPS", members=("bob",))

    def put(self, who, path, body=None, status=200):
        r = self.client(who).request("PUT", path, body if body is not None else {})
        self.assertEqual(r.status, status, r.text)
        return r.json

    def box(self, who, **query):
        return self.ok(self.client(who).get("/api/inbox", query=query))

    def items(self, who, **query):
        return self.box(who, **query)["items"]

    def kinds(self, who, **query):
        return [i["kind"] for i in self.items(who, **query)]

    def unread(self, who):
        return self.ok(self.client(who).get("/api/inbox/count"))["unread"]

    def task(self, title="A task", **fields):
        return self.make_task("WEB", title, **fields)["key"]

    def comment(self, who, key, body):
        return self.ok(self.client(who).post("/api/tasks/%s/comments" % key, {"body": body}), 201)

    def status(self, who, key, status):
        r = self.client(who).patch("/api/tasks/" + key, {"status": status})
        self.assertEqual(r.status, 200, r.text)

    def watch(self, who, key, state="watching"):
        return self.put(who, "/api/tasks/%s/watch" % key, {"state": state})

    def later(self, **delta):
        self.now = self.now + timedelta(**delta)

    def conn(self):
        conn = db.connect(self.config.db_path)
        self.addCleanup(conn.close)
        return conn

    def rows(self, sql, *args):
        return self.conn().execute(sql, args).fetchall()


class AssignmentTests(InboxBase):
    def test_the_assignee_is_told_and_the_assigner_is_not(self):
        key = self.task("Fix login", assignee="bob")
        [item] = self.items("bob")
        self.assertEqual((item["kind"], item["reason"], item["task"], item["project"], item["read"]), ("assigned", "assigned", key, "WEB", False))
        self.assertEqual(item["text"], "Alice assigned WEB-1 Fix login to you")
        self.assertEqual(item["actor"]["name"], "Alice")
        self.assertEqual(self.items("alice"), [])
        self.assertEqual(self.unread("bob"), 1)

    def test_assigning_later_and_to_yourself(self):
        key = self.task()
        self.ok(self.client("alice").patch("/api/tasks/" + key, {"assignee": "carol"}))
        self.assertEqual(self.kinds("carol"), ["assigned"])
        self.ok(self.client("carol").patch("/api/tasks/" + key, {"assignee": "bob"}))
        self.assertEqual(self.kinds("bob"), ["assigned"])
        self.ok(self.client("bob").patch("/api/tasks/" + key, {"assignee": "carol"}))  # carol is told again, by bob
        self.ok(self.client("carol").patch("/api/tasks/" + key, {"assignee": "bob"}))
        self.ok(self.client("bob").patch("/api/tasks/" + key, {"assignee": None}))
        self.ok(self.client("alice").patch("/api/tasks/" + key, {"assignee": "bob"}))
        self.assertEqual(self.items("bob")[0]["actor"]["name"], "Alice")
        key2 = self.task("Mine", as_user="bob", assignee="bob")
        self.assertNotIn(key2, [i["task"] for i in self.items("bob")])  # nobody tells you what you did to yourself

    def test_the_assignee_and_the_creator_now_watch_the_task(self):
        key = self.task(assignee="bob")
        w = self.ok(self.client("vera").get("/api/tasks/%s/watchers" % key))
        self.assertEqual(sorted(x["username"] for x in w["watchers"]), ["alice", "bob"])
        self.assertEqual({x["source"] for x in w["watchers"]}, {"auto"})

    def test_a_bulk_assignment_is_one_item(self):
        for i in range(4):
            self.task("Task %d" % i, assignee="bob")
        items = self.items("bob")
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["count"], items[0]["kind"]), (4, "assigned"))
        self.assertIn("assigned you 4 tasks in WEB", items[0]["text"])

    def test_assignments_a_moment_apart_stay_separate(self):
        self.task("One", assignee="bob")
        self.later(seconds=30)
        self.task("Two", assignee="bob")
        self.assertEqual([i["count"] for i in self.items("bob")], [1, 1])

    def test_inactive_people_hear_nothing(self):
        bob = [u for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == "bob"][0]
        self.ok(self.admin.patch("/api/users/%d" % bob["id"], {"active": False}))
        self.task(assignee="carol")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM notifications WHERE user_id = ?", bob["id"])[0][0], 0)


class CommentTests(InboxBase):
    def setUp(self):
        super().setUp()
        self.key = self.task(assignee="bob")

    def test_watchers_hear_about_comments_but_not_the_author(self):
        self.comment("carol", self.key, "Looks good to me")
        [c] = [i for i in self.items("bob") if i["kind"] == "comment"]
        self.assertEqual((c["reason"], c["excerpt"], c["count"]), ("watching", "Looks good to me", 1))
        self.assertEqual(c["text"], "Carol commented on WEB-1 A task: \u201cLooks good to me\u201d")
        self.assertEqual(self.items("carol"), [])
        self.assertEqual(self.kinds("alice"), ["comment"])  # the creator watches too

    def test_a_mention_beats_watching_and_is_one_item(self):
        self.comment("alice", self.key, "@bob can you take this?")
        mine = [i for i in self.items("bob") if i["kind"] != "assigned"]
        self.assertEqual([(i["kind"], i["reason"]) for i in mine], [("mention", "mentioned")])
        self.assertEqual(mine[0]["text"], "Alice mentioned you on WEB-1 A task: \u201c@bob can you take this?\u201d")

    def test_a_mention_makes_you_a_watcher(self):
        self.comment("alice", self.key, "@carol look")
        self.comment("bob", self.key, "second")
        self.assertEqual(self.kinds("carol"), ["comment", "mention"])

    def test_comments_merge_while_unread_and_recent(self):
        for who, text in (("carol", "one"), ("alice", "two"), ("carol", "three")):
            self.comment(who, self.key, text)
        [c] = [i for i in self.items("bob") if i["kind"] == "comment"]
        self.assertEqual((c["count"], c["excerpt"]), (3, "three"))
        self.assertEqual(c["text"], "3 new comments on WEB-1 A task (latest by Carol)")
        self.assertEqual(c["actors"], ["Carol", "Alice"])

    def test_a_comment_after_ten_minutes_or_after_reading_is_a_new_item(self):
        self.comment("carol", self.key, "one")
        self.later(minutes=11)
        self.comment("carol", self.key, "two")
        self.assertEqual([i["count"] for i in self.items("bob", kind="comment")], [1, 1])
        newest = self.items("bob", kind="comment")[0]["id"]
        self.ok(self.client("bob").post("/api/inbox/read", {"ids": [newest]}))
        self.comment("carol", self.key, "three")
        self.assertEqual([i["count"] for i in self.items("bob", kind="comment")], [1, 1, 1])

    def test_mentions_never_merge(self):
        for text in ("@bob one", "@bob two"):
            self.comment("alice", self.key, text)
        self.assertEqual([i["count"] for i in self.items("bob", kind="mention")], [1, 1])

    def test_comments_on_other_tasks_are_separate(self):
        other = self.task("Other", assignee="bob")
        self.comment("carol", self.key, "a")
        self.comment("carol", other, "b")
        self.assertEqual(len(self.items("bob", kind="comment")), 2)

    def test_what_a_commenter_wrote_is_not_markup(self):
        self.comment("carol", self.key, "x" * 300)
        self.assertEqual(len(self.items("bob", kind="comment")[0]["excerpt"]), 140)


class StatusAndEditTests(InboxBase):
    def setUp(self):
        super().setUp()
        self.key = self.task(assignee="bob")

    def test_status_changes_reach_watchers_and_merge(self):
        self.status("carol", self.key, "in_progress")
        self.status("carol", self.key, "done")
        [s] = [i for i in self.items("bob") if i["kind"] == "status_changed"]
        self.assertEqual((s["count"], s["details"]["status"]), (2, ["todo", "done"]))
        self.assertEqual(s["text"], "2 status changes on WEB-1 A task (todo to done)")
        self.status("carol", self.key, "todo")
        self.later(minutes=20)
        self.status("carol", self.key, "in_progress")
        [one] = [i for i in self.items("bob", kind="status_changed")][:1]
        self.assertEqual(one["text"], "Carol moved WEB-1 A task from todo to in_progress")

    def test_edits_name_the_fields_and_an_assignment_alone_is_not_an_edit(self):
        self.ok(self.client("carol").patch("/api/tasks/" + self.key, {"title": "New title", "priority": "high"}))
        [u] = [i for i in self.items("bob") if i["kind"] == "updated"]
        self.assertEqual(u["details"]["fields"], ["priority", "title"])
        self.ok(self.client("alice").patch("/api/tasks/" + self.key, {"assignee": "carol"}))
        self.assertEqual(len([i for i in self.items("bob") if i["kind"] == "updated"]), 1)
        self.assertEqual(self.kinds("carol"), ["assigned"])

    def test_edits_merge_and_union_the_fields(self):
        self.ok(self.client("carol").patch("/api/tasks/" + self.key, {"title": "T2"}))
        self.ok(self.client("carol").patch("/api/tasks/" + self.key, {"description": "d"}))
        [u] = [i for i in self.items("bob") if i["kind"] == "updated"]
        self.assertEqual((u["count"], u["details"]["fields"]), (2, ["description", "title"]))

    def test_nothing_changed_means_nothing_sent(self):
        before = self.box("bob")
        self.ok(self.client("carol").patch("/api/tasks/" + self.key, {"title": "A task"}))
        self.assertEqual(self.box("bob"), before)


class WatchingTests(InboxBase):
    def setUp(self):
        super().setUp()
        self.key = self.task(assignee="bob")

    def test_a_task_you_never_touched_is_silent(self):
        self.comment("alice", self.key, "hello")
        self.assertEqual(self.items("carol"), [])

    def test_watching_by_choice_even_as_a_viewer(self):
        w = self.watch("vera", self.key)
        self.assertEqual(w["me"], {"state": "watching", "source": "manual"})
        self.comment("alice", self.key, "hello")
        self.assertEqual(self.kinds("vera"), ["comment"])

    def test_ignoring_silences_everything_but_a_mention(self):
        self.watch("bob", self.key, "ignoring")
        self.comment("carol", self.key, "chat")
        self.status("carol", self.key, "in_progress")
        self.assertEqual([i["kind"] for i in self.items("bob") if i["kind"] != "assigned"], [])
        self.comment("carol", self.key, "@bob really")
        self.assertEqual(self.kinds("bob")[0], "mention")

    def test_an_ignored_task_stays_ignored_when_you_act_on_it(self):
        self.watch("carol", self.key, "ignoring")
        self.comment("carol", self.key, "my own comment")
        self.comment("alice", self.key, "reply")
        self.assertEqual(self.items("carol"), [])
        self.assertEqual(self.ok(self.client("carol").get("/api/tasks/%s/watchers" % self.key))["me"]["state"], "ignoring")

    def test_a_choice_overrides_the_automatic_one_and_clearing_restores_the_default(self):
        self.watch("bob", self.key, "ignoring")
        self.assertEqual(self.client("bob").delete("/api/tasks/%s/watch" % self.key).status, 204)
        self.assertIsNone(self.ok(self.client("bob").get("/api/tasks/%s/watchers" % self.key))["me"])
        self.comment("carol", self.key, "silent now")  # the automatic watch went with the row
        self.assertEqual([i for i in self.items("bob") if i["kind"] == "comment"], [])
        self.comment("bob", self.key, "I am back")  # acting on it again makes you a watcher again
        self.comment("carol", self.key, "heard")
        self.assertEqual(self.kinds("bob")[0], "comment")
        self.assertEqual(self.client("bob").delete("/api/tasks/%s/watch" % self.key).status, 204)
        self.assertEqual(self.client("bob").delete("/api/tasks/%s/watch" % self.key).status, 204)  # idempotent

    def test_ignoring_a_task_beats_watching_its_project(self):
        self.put("bob", "/api/projects/WEB/watch")
        other = self.task("Other")
        self.watch("bob", self.key, "ignoring")
        self.comment("carol", self.key, "on the ignored one")
        self.assertEqual([i for i in self.items("bob") if i["kind"] == "comment"], [])
        self.comment("carol", other, "on another")
        self.assertEqual([i["task"] for i in self.items("bob") if i["kind"] == "comment"], [other])

    def test_you_are_not_told_about_your_own_actions_on_what_you_watch(self):
        self.watch("vera", self.key)
        self.put("carol", "/api/projects/WEB/watch")
        self.comment("alice", self.key, "from the creator")
        self.status("alice", self.key, "in_progress")
        self.assertEqual(self.items("alice"), [])
        self.comment("carol", self.key, "my own words")
        self.assertNotIn("my own words", [i["excerpt"] for i in self.items("carol")])

    def test_the_list_of_what_you_watch(self):
        self.watch("vera", self.key)
        self.put("vera", "/api/projects/OPS/watch", {"level": "important"})
        mine = self.ok(self.client("vera").get("/api/watching"))
        self.assertEqual(mine["tasks"], [{"key": self.key, "title": "A task", "state": "watching", "source": "manual"}])
        self.assertEqual(mine["projects"], [{"key": "OPS", "name": "Project OPS", "level": "important"}])

    def test_validation(self):
        for body in ({}, {"state": "maybe"}, {"state": 1}, {"x": 1}):
            self.assertEqual(self.client("vera").request("PUT", "/api/tasks/%s/watch" % self.key, body).status, 422, body)
        self.assertEqual(self.client("vera").request("PUT", "/api/tasks/WEB-99/watch", {"state": "watching"}).status, 404)
        self.assertEqual(self.client("vera").request("PUT", "/api/projects/NOPE/watch", {}).status, 404)
        self.assertEqual(self.client("vera").request("PUT", "/api/projects/WEB/watch", {"level": "some"}).status, 422)
        self.assertEqual(self.client("vera").get("/api/tasks/WEB-99/watchers").status, 404)
        self.assertEqual(self.client().request("PUT", "/api/tasks/%s/watch" % self.key, {"state": "watching"}).status, 401)

    def test_a_limit_on_what_you_can_follow(self):
        other = self.task("Other")
        third = self.task("Third")
        with mock.patch.object(inbox, "MAX_WATCHES", 2):
            self.watch("vera", self.key)
            self.watch("vera", other)
            r = self.client("vera").request("PUT", "/api/tasks/%s/watch" % third, {"state": "watching"})
            self.assertEqual((r.status, r.json["error"]["code"]), (409, "watch_limit"))
            self.watch("vera", other, "ignoring")  # changing one you already have is fine
            r = self.client("vera").request("PUT", "/api/projects/WEB/watch", {})
            self.assertEqual(r.status, 409)

    def test_a_read_only_token_cannot_change_watching_but_can_read(self):
        t = self.ok(self.client("vera").post("/api/tokens", {"name": "ro", "read_only": True}), 201)["token"]
        r = self.client(token=t).request("PUT", "/api/tasks/%s/watch" % self.key, {"state": "watching"})
        self.assertEqual((r.status, r.json["error"]["code"]), (403, "token_read_only"))
        self.assertEqual(self.client(token=t).get("/api/inbox").status, 200)

    def test_deleting_the_task_keeps_the_notification_and_drops_the_watch(self):
        self.comment("carol", self.key, "hi")
        self.assertEqual(self.client("alice").delete("/api/tasks/%s" % self.key).status, 204)
        self.assertTrue(all(i["task"] == self.key for i in self.items("bob")))
        self.assertGreaterEqual(len(self.items("bob")), 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM task_watches")[0][0], 0)
        self.assertEqual(self.ok(self.client("bob").get("/api/watching"))["tasks"], [])


class ProjectWatchTests(InboxBase):
    def test_all_hears_everything_in_the_project(self):
        self.put("vera", "/api/projects/WEB/watch")
        key = self.task("New thing")
        self.comment("alice", key, "c")
        self.status("alice", key, "in_progress")
        self.ok(self.client("alice").patch("/api/tasks/" + key, {"title": "Newer"}))
        self.assertEqual(sorted(self.kinds("vera")), ["comment", "status_changed", "task_created", "updated"])

    def test_important_hears_only_what_matters(self):
        self.put("vera", "/api/projects/WEB/watch", {"level": "important"})
        key = self.task("New thing")
        self.comment("alice", key, "c")
        self.status("alice", key, "in_progress")
        self.assertEqual(self.items("vera"), [])
        self.status("alice", key, "blocked")
        self.assertEqual(self.kinds("vera"), ["status_changed"])

    def test_other_projects_are_not_heard(self):
        self.put("vera", "/api/projects/OPS/watch")
        self.task("In WEB")
        self.assertEqual(self.items("vera"), [])

    def test_created_tasks_merge_per_project(self):
        self.put("vera", "/api/projects/WEB/watch")
        for i in range(3):
            self.task("Task %d" % i)
        [item] = self.items("vera")
        self.assertEqual((item["kind"], item["count"]), ("task_created", 3))
        self.assertEqual(item["text"], "3 tasks created in WEB (latest by Alice)")

    def test_a_project_that_changes_status_and_sprints(self):
        self.put("vera", "/api/projects/WEB/watch", {"level": "important"})
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "on_hold"}))
        self.assertEqual(self.items("vera")[0]["text"], "Alice moved project WEB from active to on_hold")
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "active"}))
        key = self.task("In the sprint")
        s = self.ok(self.client("alice").post("/api/projects/WEB/sprints", {"name": "S1", "start_date": "2026-10-12", "end_date": "2026-10-23"}), 201)
        self.ok(self.client("alice").post("/api/sprints/%d/tasks" % s["id"], {"tasks": [key]}))
        self.ok(self.client("alice").post("/api/sprints/%d/start" % s["id"], {}))
        self.assertEqual(self.items("vera")[0]["text"], "Alice started sprint S1 in WEB")

    def test_stopping(self):
        self.put("vera", "/api/projects/WEB/watch")
        self.assertEqual(self.client("vera").delete("/api/projects/WEB/watch").status, 204)
        self.task("Quiet")
        self.assertEqual(self.items("vera"), [])
        self.put("vera", "/api/projects/WEB/watch")
        self.assertEqual(self.put("vera", "/api/projects/WEB/watch", {"level": "important"}), {"project": "WEB", "level": "important"})

    def test_deleting_the_project_removes_the_watch(self):
        self.put("vera", "/api/projects/OPS/watch")
        self.assertEqual(self.admin.delete("/api/projects/OPS", query={"confirm": "OPS"}).status, 204)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM project_watches")[0][0], 0)


class TimesheetNoticeTests(InboxBase):
    def test_the_owner_hears_the_decision(self):
        key = self.task(assignee="bob")
        self.ok(self.client("bob").post("/api/tasks/%s/time" % key, {"hours": 4, "work_date": "2026-10-12"}), 201)
        sheet = self.ok(self.client("bob").post("/api/timesheets/submit", {"week": "2026-10-12"}), 201)
        self.assertEqual([i for i in self.kinds("bob") if i == "timesheet"], [])
        self.ok(self.client("alice").post("/api/timesheets/%d/reject" % sheet["id"], {"note": "Missing the review"}))
        self.assertEqual(self.items("bob")[0]["text"], "Alice sent back your timesheet for the week of 2026-10-12: \u201cMissing the review\u201d")
        self.ok(self.client("bob").post("/api/timesheets/submit", {"week": "2026-10-12"}), 201)
        self.ok(self.client("alice").post("/api/timesheets/%d/approve" % sheet["id"], {}))
        self.assertEqual(self.items("bob")[0]["text"], "Alice approved your timesheet for the week of 2026-10-12")
        self.ok(self.admin.post("/api/timesheets/%d/reopen" % sheet["id"], {"note": "late entry"}))
        self.assertEqual(self.kinds("bob")[:3], ["timesheet", "timesheet", "timesheet"])
        self.assertEqual(self.items("alice"), [])


class TemplateFloodTests(InboxBase):
    def test_starting_a_project_from_a_template_is_one_item_per_person(self):
        for i in range(5):
            self.task("Step %d" % i, assignee="bob")
        t = self.ok(self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "Std"}), 201)
        self.ok(self.client("bob").post("/api/inbox/read", {"all": True}))
        self.ok(self.client("alice").post("/api/templates/%d/instantiate" % t["id"], {"key": "NEW", "name": "New", "start_date": "2026-11-02", "assignments": {"bob": "bob"}}), 201)
        items = [i for i in self.items("bob") if i["project"] == "NEW"]
        self.assertEqual([(i["kind"], i["count"]) for i in items], [("assigned", 5)])


class SettingsTests(InboxBase):
    def test_defaults_and_changes(self):
        s = self.ok(self.client("bob").get("/api/me/notification-settings"))
        self.assertEqual(s["self_notify"], False)
        self.assertEqual(sorted(s["kinds"]), sorted(inbox.KINDS))
        self.assertTrue(all(s["kinds"].values()))
        s = self.put("bob", "/api/me/notification-settings", {"kinds": {"comment": False}})
        self.assertEqual((s["kinds"]["comment"], s["kinds"]["mention"]), (False, True))
        s = self.put("bob", "/api/me/notification-settings", {"self_notify": True})
        self.assertEqual((s["kinds"]["comment"], s["self_notify"]), (False, True))  # the other setting survived
        self.assertTrue(self.ok(self.client("carol").get("/api/me/notification-settings"))["kinds"]["comment"])

    def test_a_switched_off_kind_is_not_delivered(self):
        key = self.task(assignee="bob")
        self.put("bob", "/api/me/notification-settings", {"kinds": {"comment": False}})
        self.comment("carol", key, "quiet")
        self.comment("carol", key, "@bob loud")
        self.assertEqual([i["kind"] for i in self.items("bob") if i["kind"] != "assigned"], ["mention"])

    def test_telling_you_about_yourself_is_opt_in(self):
        key = self.task()
        self.put("alice", "/api/me/notification-settings", {"self_notify": True})
        self.comment("alice", key, "note to self")
        self.assertEqual(self.kinds("alice"), ["comment"])

    def test_validation(self):
        for body in ({"kinds": {"nope": True}}, {"kinds": {"comment": "yes"}}, {"kinds": []}, {"self_notify": "yes"}, {"bogus": 1}):
            self.assertEqual(self.client("bob").request("PUT", "/api/me/notification-settings", body).status, 422, body)


class InboxApiTests(InboxBase):
    def setUp(self):
        super().setUp()
        self.k1 = self.task("One", assignee="bob")
        self.k2 = self.task("Two", assignee="bob")
        self.later(minutes=20)
        self.comment("carol", self.k1, "hello")
        self.later(minutes=20)
        self.comment("alice", self.k2, "@bob ping")

    def test_newest_first_with_a_cursor(self):
        box = self.box("bob", limit=2)
        self.assertEqual(len(box["items"]), 2)
        self.assertEqual(box["items"][0]["kind"], "mention")
        self.assertIsNotNone(box["next_before"])
        rest = self.items("bob", before=box["next_before"])
        self.assertGreaterEqual(len(rest), 1)
        self.assertTrue(all(i["event_id"] < box["next_before"] for i in rest))
        everything = self.items("bob")
        self.assertEqual([i["id"] for i in everything], [i["id"] for i in box["items"]] + [i["id"] for i in self.items("bob", before=box["next_before"])])
        self.assertIsNone(self.box("bob")["next_before"])

    def test_filters(self):
        self.assertEqual(set(self.kinds("bob", kind="mention,comment")), {"mention", "comment"})
        self.assertEqual(self.kinds("bob", kind="assigned"), ["assigned"])
        self.assertEqual(self.items("bob", project="ops"), [])
        self.assertEqual(len(self.items("bob", project="web")), len(self.items("bob")))
        newest = self.items("bob")[0]["id"]
        self.ok(self.client("bob").post("/api/inbox/read", {"ids": [newest]}))
        self.assertNotIn(newest, [i["id"] for i in self.items("bob", unread="1")])

    def test_counts(self):
        c = self.ok(self.client("bob").get("/api/inbox/count"))
        self.assertEqual(c["unread"], c["total"])
        self.assertEqual(self.box("bob")["unread"], c["unread"])

    def test_reading(self):
        ids = [i["id"] for i in self.items("bob")]
        r = self.ok(self.client("bob").post("/api/inbox/read", {"ids": ids[:1]}))
        self.assertEqual((r["updated"], r["unread"]), (1, len(ids) - 1))
        self.assertEqual(self.ok(self.client("bob").post("/api/inbox/read", {"ids": ids[:1]}))["updated"], 0)  # already read
        r = self.ok(self.client("bob").post("/api/inbox/read", {"all": True, "kind": "assigned"}))
        self.assertEqual(self.unread("bob"), len(ids) - 1 - r["updated"])
        r = self.ok(self.client("bob").post("/api/inbox/read", {"all": True}))
        self.assertEqual(r["unread"], 0)
        self.assertEqual(self.items("bob", unread="1"), [])
        r = self.ok(self.client("bob").post("/api/inbox/unread", {"ids": ids[:2]}))
        self.assertEqual((r["updated"], r["unread"]), (2, 2))

    def test_reading_up_to_a_point(self):
        items = self.items("bob")
        cut = items[1]["event_id"]
        r = self.ok(self.client("bob").post("/api/inbox/read", {"all": True, "before": cut}))
        self.assertEqual(r["unread"], 1)
        self.assertFalse(self.items("bob")[0]["read"])

    def test_reading_by_project(self):
        self.assertEqual(self.ok(self.client("bob").post("/api/inbox/read", {"all": True, "project": "OPS"}))["updated"], 0)
        self.assertGreater(self.ok(self.client("bob").post("/api/inbox/read", {"all": True, "project": "web"}))["updated"], 0)

    def test_it_is_yours_alone(self):
        theirs = self.items("bob")[0]["id"]
        self.assertEqual(self.ok(self.client("carol").post("/api/inbox/read", {"ids": [theirs]}))["updated"], 0)
        self.assertFalse(self.items("bob")[0]["read"])
        self.assertEqual(self.client("carol").get("/api/inbox/%d" % theirs).status, 404)
        self.assertEqual(self.client("carol").delete("/api/inbox/%d" % theirs).status, 404)
        self.assertEqual(self.client("carol").request("DELETE", "/api/inbox", query={"status": "all"}).status, 200)
        self.assertEqual(self.client("bob").get("/api/inbox/%d" % theirs).status, 200)

    def test_deleting_and_clearing(self):
        items = self.items("bob")
        self.assertEqual(self.client("bob").delete("/api/inbox/%d" % items[0]["id"]).status, 204)
        self.assertEqual(self.client("bob").delete("/api/inbox/%d" % items[0]["id"]).status, 404)
        self.ok(self.client("bob").post("/api/inbox/read", {"ids": [items[1]["id"]]}))
        r = self.ok(self.client("bob").delete("/api/inbox", query={"status": "read"}))
        self.assertEqual(r["removed"], 1)
        r = self.ok(self.client("bob").delete("/api/inbox", query={"status": "all"}))
        self.assertEqual((r["removed"], r["unread"]), (len(items) - 2, 0))
        self.assertEqual(self.items("bob"), [])

    def test_one_item(self):
        item = self.items("bob")[0]
        self.assertEqual(self.ok(self.client("bob").get("/api/inbox/%d" % item["id"])), item)

    def test_bad_requests(self):
        for q in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"before": "x"}, {"before": 0}, {"kind": "nope"}):
            self.assertEqual(self.client("bob").get("/api/inbox", query=q).status, 422, q)
        for body in ({}, {"ids": []}, {"ids": "1"}, {"ids": [True]}, {"ids": list(range(1, 202))}, {"ids": [1], "all": True}, {"all": False}, {"all": True, "kind": "x"}, {"bogus": 1}):
            self.assertEqual(self.client("bob").post("/api/inbox/read", body).status, 422, body)
        for body in ({}, {"ids": []}, {"ids": ["1"]}):
            self.assertEqual(self.client("bob").post("/api/inbox/unread", body).status, 422, body)
        for q in ({}, {"status": "some"}):
            self.assertEqual(self.client("bob").request("DELETE", "/api/inbox", query=q).status, 422, q)
        self.assertEqual(self.client("bob").get("/api/inbox/abc").status, 404)
        self.assertEqual(self.client().get("/api/inbox").status, 401)


class BoundsTests(InboxBase):
    def test_an_inbox_holds_a_fixed_number_and_loses_read_items_first(self):
        key = self.task()
        with mock.patch.object(inbox, "MAX_ITEMS", 4):
            for i in range(3):
                self.comment("alice", key, "@bob mention %d" % i)
            old = self.items("bob")
            self.ok(self.client("bob").post("/api/inbox/read", {"ids": [old[-1]["id"]]}))  # the oldest is read
            for i in range(3, 6):
                self.comment("alice", key, "@bob mention %d" % i)
            items = self.items("bob")
            self.assertEqual(len(items), 4)
            self.assertEqual([i["excerpt"][-9:] for i in items], ["mention 5", "mention 4", "mention 3", "mention 2"])
            self.assertTrue(all(not i["read"] for i in items))  # the read one went first

    def test_when_nothing_is_read_the_oldest_unread_goes(self):
        key = self.task()
        with mock.patch.object(inbox, "MAX_ITEMS", 3):
            for i in range(5):
                self.comment("alice", key, "@bob m%d" % i)
        self.assertEqual([i["excerpt"][-2:] for i in self.items("bob")], ["m4", "m3", "m2"])

    def test_read_items_are_forgotten_after_a_month(self):
        key = self.task()
        self.comment("alice", key, "@bob old news")
        self.ok(self.client("bob").post("/api/inbox/read", {"all": True}))
        self.later(days=31)
        self.comment("alice", key, "@bob fresh")
        self.assertEqual([i["excerpt"] for i in self.items("bob")], ["@bob fresh"])

    def test_unread_items_are_not_forgotten_by_age(self):
        key = self.task()
        self.comment("alice", key, "@bob old but unread")
        self.later(days=60)
        self.comment("alice", key, "@bob fresh")
        self.assertEqual(len(self.items("bob")), 2)


class RobustnessTests(InboxBase):
    def test_a_failure_in_the_inbox_never_fails_the_change(self):
        key = self.task(assignee="bob")
        with mock.patch.object(inbox, "_fan_out", side_effect=RuntimeError("boom")):
            c = self.comment("carol", key, "still saved")
        self.assertEqual(self.ok(self.client("bob").get("/api/tasks/%s/comments" % key))["items"][-1]["id"], c["id"])
        self.assertEqual([i for i in self.items("bob") if i["kind"] == "comment"], [])
        self.comment("carol", key, "and it works again")
        self.assertEqual(self.kinds("bob")[0], "comment")

    def test_a_half_written_notification_is_rolled_back(self):
        key = self.task(assignee="bob")
        real = inbox._deliver
        calls = []

        def flaky(*args, **kw):
            calls.append(1)
            real(*args, **kw)
            raise RuntimeError("after writing")

        with mock.patch.object(inbox, "_deliver", flaky):
            self.comment("carol", key, "x")
        self.assertTrue(calls)
        self.assertEqual([i for i in self.items("bob") if i["kind"] == "comment"], [])

    def test_an_event_is_delivered_once_per_person(self):
        key = self.task(assignee="bob")
        self.watch("bob", key)
        self.put("bob", "/api/projects/WEB/watch")
        self.comment("carol", key, "@bob look")
        self.assertEqual([i["kind"] for i in self.items("bob") if i["event_id"] == self.items("bob")[0]["event_id"]], ["mention"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM (SELECT user_id, last_event_id FROM notifications GROUP BY 1, 2 HAVING COUNT(*) > 1)")[0][0], 0)

    def test_a_rolled_back_request_leaves_nothing(self):
        key = self.task(assignee="bob")
        before = self.rows("SELECT COUNT(*) FROM notifications")[0][0]
        r = self.client("alice").post("/api/tasks/%s/comments" % key, {"body": ""})
        self.assertEqual(r.status, 422)
        r = self.client("alice").patch("/api/tasks/" + key, {"assignee": "carol", "due_date": "bad"})
        self.assertEqual(r.status, 422)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM notifications")[0][0], before)

    def test_hostile_input_never_causes_a_server_error(self):
        key = self.task(assignee="bob")
        junk = [None, "", "0", "-1", "' OR 1=1 --", "\x00", "9" * 400, [], {}, True, 10**30]
        for who in ("bob", "alice", "vera"):
            for v in junk:
                if not isinstance(v, (list, dict, bool, int)):
                    for key_ in ("limit", "before", "kind", "project", "unread", "status"):
                        for path in ("/api/inbox", "/api/watching"):
                            self.assertLess(self.client(who).get(path, query={key_: v}).status, 500, (path, key_, v))
                        self.assertLess(self.client(who).request("DELETE", "/api/inbox", query={key_: v}).status, 500)
                for path, bodies in (
                    ("/api/inbox/read", ({"ids": v}, {"all": v}, {"kind": v}, {"project": v}, {"before": v}, v)),
                    ("/api/inbox/unread", ({"ids": v}, v)),
                ):
                    for body in bodies:
                        self.assertLess(self.client(who).post(path, body).status, 500, (path, body))
                for path in ("/api/me/notification-settings", "/api/tasks/%s/watch" % key, "/api/projects/WEB/watch"):
                    for body in ({"kinds": v}, {"state": v}, {"level": v}, {"self_notify": v}, v):
                        self.assertLess(self.client(who).request("PUT", path, body).status, 500, (path, body))
        self.assertGreaterEqual(self.rows("SELECT COUNT(*) FROM users")[0][0], 5)


class UpgradeTests(InboxBase):
    def v10_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        for version in range(1, 10):
            db._migrate(conn, version)
        conn.close()

    def names(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_an_older_database_upgrades_to_the_same_schema_as_a_new_one(self):
        old = os.path.join(self.tmp, "v10.db")
        self.v10_database(old)
        self.assertNotIn("notifications", self.names(old)[0])
        init_db(old)
        fresh = os.path.join(self.tmp, "fresh11.db")
        init_db(fresh)
        self.assertEqual(self.names(old), self.names(fresh))
        self.assertEqual(self.names(old)[1], db.SCHEMA_VERSION)

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v10b.db")
        self.v10_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE notifications (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        names, version = self.names(path)
        self.assertEqual(version, 10)
        self.assertNotIn("task_watches", names)
