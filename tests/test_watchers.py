"""Watchers: following a task without being its assignee, and being named in Cliq messages about it."""

from unittest import mock

from helpers import ApiTestCase


class WatcherBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.make_task("WEB", "Release plan", assignee="bob")
        self.alice, self.bob, self.carol = self.client("alice"), self.client("bob"), self.client("carol")
        self.notifier.messages.clear()

    def watch(self, who="carol", target=None):
        return self.client(who).post("/api/tasks/WEB-1/watchers", {"username": target} if target else {})

    def names(self, key="WEB-1"):
        return [w["username"] for w in self.ok(self.alice.get("/api/tasks/%s/watchers" % key))["items"]]

    def user_id(self, username):
        return next(u["id"] for u in self.ok(self.alice.get("/api/users"))["items"] if u["username"] == username)


class FollowTests(WatcherBase):
    def test_following_yourself_is_allowed_for_everyone_and_idempotent(self):
        for who in ("carol", "vera", "bob"):  # a viewer, and the assignee, may follow too
            body = self.ok(self.watch(who))
            self.assertTrue(body["watching"])
        self.ok(self.watch("carol"))  # twice is fine and adds nothing
        self.assertEqual(self.names(), ["bob", "carol", "vera"])

    def test_the_task_shows_who_is_watching(self):
        self.ok(self.watch("carol"))
        mine = self.ok(self.carol.get("/api/tasks/WEB-1"))
        self.assertEqual((mine["watching"], mine["watcher_count"], [w["username"] for w in mine["watchers"]]), (True, 1, ["carol"]))
        other = self.ok(self.bob.get("/api/tasks/WEB-1"))
        self.assertEqual((other["watching"], other["watcher_count"]), (False, 1))
        listed = self.ok(self.bob.get("/api/tasks", query={"project": "WEB"}))["items"][0]
        self.assertEqual(listed["watcher_count"], 1)
        self.assertNotIn("watchers", listed)

    def test_stop_following(self):
        self.ok(self.watch("carol"))
        self.ok(self.carol.delete("/api/tasks/WEB-1/watchers/carol"), 204)
        self.assertEqual(self.names(), [])
        r = self.carol.delete("/api/tasks/WEB-1/watchers/carol")
        self.assertEqual((r.status, r.error_code), (404, "watcher_not_found"))
        self.assertEqual(self.carol.delete("/api/tasks/WEB-1/watchers/nobody").status, 404)

    def test_adding_someone_else_needs_write_access(self):
        self.assertEqual(self.watch("vera", target="carol").status, 403)  # a viewer cannot sign others up
        self.make_project("API", members=("carol",))
        self.make_task("API", "Elsewhere")
        r = self.client("bob").post("/api/tasks/API-1/watchers", {"username": "carol"})  # bob is not in API
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        self.ok(self.watch("bob", target="carol"))  # a project member can
        self.ok(self.watch("alice", target="vera"))  # so can a manager - and the person need not be a project member
        self.assertEqual(self.names(), ["carol", "vera"])

    def test_you_cannot_remove_other_people_without_write_access(self):
        self.ok(self.watch("carol"))
        self.assertEqual(self.client("vera").delete("/api/tasks/WEB-1/watchers/carol").status, 403)
        self.ok(self.bob.delete("/api/tasks/WEB-1/watchers/carol"), 204)

    def test_unknown_and_deactivated_people_are_refused(self):
        for name in ("ghost", "", 5):
            self.assertEqual(self.client("alice").post("/api/tasks/WEB-1/watchers", {"username": name}).status, 422, name)
        self.assertEqual(self.alice.post("/api/tasks/WEB-1/watchers", {"user": "carol"}).status, 422)  # unknown field
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("vera"), {"active": False}))
        self.assertEqual(self.alice.post("/api/tasks/WEB-1/watchers", {"username": "vera"}).status, 422)

    def test_deactivated_watchers_drop_out_of_the_list_and_the_count(self):
        self.ok(self.watch("vera"))
        self.ok(self.watch("carol"))
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("vera"), {"active": False}))
        self.assertEqual(self.names(), ["carol"])
        self.assertEqual(self.ok(self.alice.get("/api/tasks/WEB-1"))["watcher_count"], 1)

    def test_the_limit(self):
        with mock.patch("zylker_pm.services.watchers.MAX_WATCHERS", 2):
            self.ok(self.watch("bob"))
            self.ok(self.watch("carol"))
            r = self.watch("vera")
            self.assertEqual((r.status, r.error_code), (409, "too_many_watchers"))
            self.ok(self.watch("carol"))  # someone already on the list is not "one more"

    def test_nobody_can_start_following_in_an_archived_project_but_anyone_can_stop(self):
        self.ok(self.watch("carol"))
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "archived"}))
        r = self.watch("bob")
        self.assertEqual((r.status, r.error_code), (409, "project_archived"))
        self.ok(self.carol.delete("/api/tasks/WEB-1/watchers/carol"), 204)

    def test_deleting_the_task_removes_its_watchers(self):
        self.ok(self.watch("carol"))
        self.ok(self.alice.delete("/api/tasks/WEB-1"), 204)
        with self.app.session(write=False) as ctx:
            self.assertEqual(ctx.conn.execute("SELECT COUNT(*) FROM task_watchers").fetchone()[0], 0)

    def test_filter_tasks_by_watcher(self):
        self.make_task("WEB", "Second")
        self.ok(self.watch("carol"))
        self.ok(self.client("carol").post("/api/tasks/WEB-2/watchers", {}))
        self.ok(self.watch("bob"))
        keys = lambda q: [t["key"] for t in self.ok(self.carol.get("/api/tasks", query=q))["items"]]  # noqa: E731
        self.assertEqual(keys({"watcher": "me", "sort": "key"}), ["WEB-1", "WEB-2"])
        self.assertEqual(keys({"watcher": "bob"}), ["WEB-1"])
        self.assertEqual(keys({"watcher": "vera"}), [])
        self.assertEqual(keys({"watcher": "bob,carol"}), ["WEB-1"])  # both must be watching

    def test_following_is_recorded_in_the_history(self):
        self.ok(self.watch("carol"))
        self.ok(self.watch("alice", target="vera"))
        self.ok(self.carol.delete("/api/tasks/WEB-1/watchers/carol"), 204)
        newest = self.ok(self.alice.get("/api/tasks/WEB-1/activity"))["items"][:3]
        acts = [(a["action"], a["details"]["user"], a["details"]["self"]) for a in newest][::-1]
        self.assertEqual(acts, [("watcher.added", "carol", True), ("watcher.added", "vera", False), ("watcher.removed", "carol", True)])


class NotificationTests(WatcherBase):
    def test_a_status_change_names_the_watchers_in_the_message_about_it(self):
        self.ok(self.watch("carol"))
        self.ok(self.watch("vera"))
        self.ok(self.bob.patch("/api/tasks/WEB-1", {"status": "done"}))
        self.assertEqual(len(self.notifier.messages), 1)  # the completion message carries the watchers, no second message
        self.assertIn("completed by Bob", self.notifier.messages[0])
        self.assertIn("Watching: Carol, Vera", self.notifier.messages[0])

    def test_other_changes_get_their_own_message(self):
        self.ok(self.watch("carol"))
        self.ok(self.bob.patch("/api/tasks/WEB-1", {"priority": "high", "due_date": "2026-10-30"}))
        self.assertEqual(len(self.notifier.messages), 1)
        text = self.notifier.messages[0]
        self.assertIn("Bob: priority: medium \u2192 high; due date: none \u2192 2026-10-30", text)
        self.assertIn("Watching: Carol", text)

    def test_status_changes_nobody_announces_still_reach_watchers(self):
        self.ok(self.watch("carol"))
        self.ok(self.bob.patch("/api/tasks/WEB-1", {"status": "in_progress"}))
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("status: todo \u2192 in progress", self.notifier.messages[0])

    def test_a_new_assignee_is_told_once_not_also_named_as_a_watcher(self):
        self.ok(self.watch("carol"))
        self.ok(self.watch("vera"))
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"assignee": "carol"}))
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("Assigned to *Carol*", self.notifier.messages[0])
        self.assertIn("Watching: Vera", self.notifier.messages[0])
        self.assertNotIn("Carol, ", self.notifier.messages[0].split("Watching:")[1])

    def test_the_person_acting_is_never_named(self):
        self.ok(self.watch("bob"))
        self.ok(self.bob.patch("/api/tasks/WEB-1", {"priority": "high"}))
        self.assertEqual(self.notifier.messages, [])

    def test_comments_reach_watchers_but_mentioned_people_are_not_told_twice(self):
        self.ok(self.watch("carol"))
        self.ok(self.watch("vera"))
        self.ok(self.bob.post("/api/tasks/WEB-1/comments", {"body": "Ping @carol, looks ready"}), 201)
        texts = self.notifier.messages
        self.assertEqual(len(texts), 2)
        self.assertIn("mentioned Carol", texts[0])
        self.assertIn("commented", texts[1])
        self.assertIn("Watching: Vera", texts[1])
        self.assertNotIn("Carol", texts[1].split("Watching:")[1])

    def test_no_watchers_no_extra_messages(self):
        self.ok(self.bob.patch("/api/tasks/WEB-1", {"priority": "high"}))
        self.ok(self.bob.post("/api/tasks/WEB-1/comments", {"body": "hello"}), 201)
        self.assertEqual(self.notifier.messages, [])

    def test_a_rejected_change_tells_nobody(self):
        self.ok(self.watch("carol"))
        self.assertEqual(self.bob.patch("/api/tasks/WEB-1", {"status": "in_review", "priority": "nonsense"}).status, 422)
        self.assertEqual(self.notifier.messages, [])

    def test_markup_in_names_and_titles_cannot_reach_the_channel(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"display_name": "*Carol* [x](http://evil)"}))
        self.ok(self.watch("carol"))
        self.ok(self.bob.patch("/api/tasks/WEB-1", {"priority": "high"}))
        text = self.notifier.messages[0]
        self.assertIn("Watching: Carol x(http://evil)", text)  # no brackets or asterisks left to form a link or bold text
        self.assertNotIn("[", text.split("Watching:")[1])
        self.assertNotIn("*", text.split("Watching:")[1])
