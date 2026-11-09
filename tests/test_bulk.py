"""Bulk actions: one set of changes applied to many tasks, with per-task reporting."""

from helpers import ApiTestCase


class BulkBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        for n in range(1, 5):
            self.make_task("WEB", "Task %d" % n)
        self.alice, self.bob = self.client("alice"), self.client("bob")
        self.notifier.messages.clear()

    def bulk(self, tasks, changes, user="alice", **extra):
        return self.client(user).post("/api/tasks/bulk", {"tasks": tasks, "changes": changes, **extra})

    def task(self, key):
        return self.ok(self.alice.get("/api/tasks/" + key))

    def results(self, body):
        return {r["key"]: r for r in body["results"]}


class ApplyTests(BulkBase):
    def test_change_status_priority_and_due_date_together(self):
        body = self.ok(self.bulk(["WEB-1", "WEB-2", "WEB-3"], {"status": "in_progress", "priority": "high", "due_date": "2026-11-01"}))
        self.assertEqual((body["requested"], body["updated"], body["unchanged"], body["failed"]), (3, 3, 0, 0))
        for key in ("WEB-1", "WEB-2", "WEB-3"):
            t = self.task(key)
            self.assertEqual((t["status"], t["priority"], t["due_date"]), ("in_progress", "high", "2026-11-01"))
        self.assertEqual(self.task("WEB-4")["status"], "todo")

    def test_assign_and_unassign(self):
        self.ok(self.bulk(["WEB-1", "WEB-2"], {"assignee": "bob"}))
        self.assertEqual([self.task(k)["assignee"]["username"] for k in ("WEB-1", "WEB-2")], ["bob", "bob"])
        self.ok(self.bulk(["WEB-1", "WEB-2"], {"assignee": None}))
        self.assertEqual([self.task(k)["assignee"] for k in ("WEB-1", "WEB-2")], [None, None])

    def test_set_and_clear_the_milestone_and_the_due_date(self):
        ms = self.ok(self.alice.post("/api/projects/WEB/milestones", {"name": "Beta", "due_date": "2026-12-01"}), 201)
        self.ok(self.bulk(["WEB-1", "WEB-2"], {"milestone": ms["id"], "due_date": "2026-11-20"}))
        self.assertEqual(self.task("WEB-2")["milestone"]["name"], "Beta")
        self.ok(self.bulk(["WEB-1", "WEB-2"], {"milestone": None, "due_date": None}))
        t = self.task("WEB-2")
        self.assertEqual((t["milestone"], t["due_date"]), (None, None))

    def test_labels_can_be_added_removed_or_replaced(self):
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"labels": ["a", "b"]}))
        self.ok(self.alice.patch("/api/tasks/WEB-2", {"labels": ["b", "c"]}))
        self.ok(self.bulk(["WEB-1", "WEB-2", "WEB-3"], {"add_labels": ["Urgent", "b"]}))
        self.assertEqual([self.task(k)["labels"] for k in ("WEB-1", "WEB-2", "WEB-3")], [["a", "b", "urgent"], ["b", "c", "urgent"], ["b", "urgent"]])
        self.ok(self.bulk(["WEB-1", "WEB-2", "WEB-3"], {"remove_labels": ["b", "nothere"]}))
        self.assertEqual([self.task(k)["labels"] for k in ("WEB-1", "WEB-2", "WEB-3")], [["a", "urgent"], ["c", "urgent"], ["urgent"]])
        self.ok(self.bulk(["WEB-1", "WEB-3"], {"add_labels": ["x"], "remove_labels": ["urgent"]}))
        self.assertEqual([self.task(k)["labels"] for k in ("WEB-1", "WEB-3")], [["a", "x"], ["x"]])
        self.ok(self.bulk(["WEB-1", "WEB-2"], {"labels": ["only"]}))
        self.assertEqual([self.task(k)["labels"] for k in ("WEB-1", "WEB-2")], [["only"], ["only"]])
        self.ok(self.bulk(["WEB-1"], {"labels": []}))
        self.assertEqual(self.task("WEB-1")["labels"], [])

    def test_tasks_already_in_the_wanted_state_are_reported_as_unchanged(self):
        self.ok(self.set_status("WEB-1", "in_progress"))
        body = self.ok(self.bulk(["WEB-1", "WEB-2"], {"status": "in_progress"}))
        self.assertEqual((body["updated"], body["unchanged"]), (1, 1))
        self.assertEqual({k: r["result"] for k, r in self.results(body).items()}, {"WEB-1": "unchanged", "WEB-2": "updated"})

    def test_keys_are_case_insensitive_and_duplicates_collapse(self):
        body = self.ok(self.bulk(["web-1", "WEB-1", " WEB-2".strip()], {"priority": "low"}))
        self.assertEqual((body["requested"], [r["key"] for r in body["results"]]), (2, ["WEB-1", "WEB-2"]))

    def test_the_order_of_the_selection_does_not_matter(self):
        child = self.make_task("WEB", "Child", parent="WEB-1")["key"]  # WEB-5
        body = self.ok(self.bulk(["WEB-1", child], {"status": "done"}))  # parent first: refused until the child is done
        self.assertEqual((body["updated"], body["failed"]), (2, 0))
        self.assertEqual((self.task("WEB-1")["status"], self.task(child)["status"]), ("done", "done"))
        body = self.ok(self.bulk([child, "WEB-1"], {"status": "todo"}))  # reopening needs the parent first
        self.assertEqual((body["updated"], body["failed"]), (2, 0))

    def test_each_task_keeps_its_own_history(self):
        self.ok(self.bulk(["WEB-1", "WEB-2"], {"priority": "critical"}))
        for key in ("WEB-1", "WEB-2"):
            act = self.ok(self.alice.get("/api/tasks/%s/activity" % key))["items"][0]
            self.assertEqual((act["action"], act["actor"]["username"], act["details"]["changes"]), ("task.updated", "alice", {"priority": ["medium", "critical"]}))


class PartialFailureTests(BulkBase):
    def test_refused_tasks_are_reported_and_the_rest_are_saved(self):
        self.ok(self.alice.post("/api/tasks/WEB-2/dependencies", {"blocked_by": "WEB-1"}))
        body = self.ok(self.bulk(["WEB-2", "WEB-3", "WEB-99"], {"status": "in_progress"}))
        self.assertEqual((body["updated"], body["failed"]), (1, 2))
        r = self.results(body)
        self.assertEqual(r["WEB-3"]["result"], "updated")
        self.assertEqual((r["WEB-2"]["result"], r["WEB-2"]["error"]["code"]), ("failed", "blocked_by_open_tasks"))
        self.assertEqual(r["WEB-2"]["error"]["details"]["blockers"], ["WEB-1"])
        self.assertEqual(r["WEB-99"]["error"]["code"], "task_not_found")
        self.assertEqual((self.task("WEB-2")["status"], self.task("WEB-3")["status"]), ("todo", "in_progress"))

    def test_a_refused_task_leaves_no_history_and_no_message_behind(self):
        self.ok(self.alice.post("/api/tasks/WEB-2/dependencies", {"blocked_by": "WEB-1"}))
        self.notifier.messages.clear()
        body = self.ok(self.bulk(["WEB-2"], {"status": "done", "priority": "high"}))
        self.assertEqual(body["failed"], 1)
        self.assertEqual(self.task("WEB-2")["priority"], "medium")  # even the part that would have been fine is undone
        self.assertEqual([a["action"] for a in self.ok(self.alice.get("/api/tasks/WEB-2/activity"))["items"]], ["task.dependency_added", "task.created"])
        self.assertEqual(self.notifier.messages, [])

    def test_value_that_only_some_tasks_can_take(self):
        self.make_project("API", members=("carol",))
        self.make_task("API", "Elsewhere")
        body = self.ok(self.bulk(["WEB-1", "API-1"], {"assignee": "bob"}))  # bob is in WEB but not API
        r = self.results(body)
        self.assertEqual((r["WEB-1"]["result"], r["API-1"]["result"]), ("updated", "failed"))
        self.assertEqual(r["API-1"]["error"]["code"], "validation_error")
        self.assertIn("assignee", r["API-1"]["error"]["details"])

    def test_milestones_belong_to_one_project(self):
        self.make_project("API", members=("carol",))
        self.make_task("API", "Elsewhere")
        ms = self.ok(self.alice.post("/api/projects/WEB/milestones", {"name": "Beta", "due_date": "2026-12-01"}), 201)
        r = self.results(self.ok(self.bulk(["WEB-1", "API-1"], {"milestone": ms["id"]})))
        self.assertEqual((r["WEB-1"]["result"], r["API-1"]["result"]), ("updated", "failed"))

    def test_members_can_only_change_tasks_in_their_own_projects(self):
        self.make_project("API", members=("carol",))
        self.make_task("API", "Elsewhere")
        r = self.results(self.ok(self.bulk(["WEB-1", "API-1"], {"priority": "high"}, user="bob")))
        self.assertEqual(r["WEB-1"]["result"], "updated")
        self.assertEqual((r["API-1"]["result"], r["API-1"]["error"]["code"]), ("failed", "not_project_member"))

    def test_archived_projects_are_read_only(self):
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "archived"}))
        body = self.ok(self.bulk(["WEB-1"], {"priority": "high"}))
        self.assertEqual(self.results(body)["WEB-1"]["error"]["code"], "project_archived")

    def test_too_many_labels_for_one_task_fail_just_that_task(self):
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"labels": ["l%d" % i for i in range(19)]}))
        r = self.results(self.ok(self.bulk(["WEB-1", "WEB-2"], {"add_labels": ["x", "y"]})))
        self.assertEqual((r["WEB-1"]["result"], r["WEB-2"]["result"]), ("failed", "updated"))
        self.assertEqual(len(self.task("WEB-1")["labels"]), 19)


class AtomicTests(BulkBase):
    def test_all_or_nothing_mode_undoes_everything_on_any_failure(self):
        self.ok(self.alice.post("/api/tasks/WEB-2/dependencies", {"blocked_by": "WEB-1"}))
        self.notifier.messages.clear()
        r = self.bulk(["WEB-3", "WEB-2"], {"status": "in_progress"}, atomic=True)
        self.assertEqual((r.status, r.error_code), (409, "bulk_failed"))
        failed = r.json["error"]["details"]["failed"]
        self.assertEqual([(f["key"], f["error"]["code"]) for f in failed], [("WEB-2", "blocked_by_open_tasks")])
        self.assertEqual(self.task("WEB-3")["status"], "todo")  # the task that would have worked was rolled back too
        self.assertEqual(self.notifier.messages, [])

    def test_all_or_nothing_mode_applies_everything_when_nothing_fails(self):
        body = self.ok(self.bulk(["WEB-1", "WEB-2"], {"priority": "low"}, atomic=True))
        self.assertEqual((body["atomic"], body["updated"]), (True, 2))
        self.assertEqual(self.task("WEB-2")["priority"], "low")


class ValidationTests(BulkBase):
    def test_the_request_itself_must_make_sense(self):
        bad = [
            {},
            {"tasks": [], "changes": {"priority": "low"}},
            {"tasks": "WEB-1", "changes": {"priority": "low"}},
            {"tasks": ["WEB-1", 5], "changes": {"priority": "low"}},
            {"tasks": ["nonsense"], "changes": {"priority": "low"}},
            {"tasks": ["WEB-1"]},
            {"tasks": ["WEB-1"], "changes": {}},
            {"tasks": ["WEB-1"], "changes": []},
            {"tasks": ["WEB-1"], "changes": {"title": "renamed"}},  # not a bulk field
            {"tasks": ["WEB-1"], "changes": {"parent": "WEB-2"}},
            {"tasks": ["WEB-1"], "changes": {"priority": "urgent"}},
            {"tasks": ["WEB-1"], "changes": {"status": "wip"}},
            {"tasks": ["WEB-1"], "changes": {"due_date": "tomorrow"}},
            {"tasks": ["WEB-1"], "changes": {"labels": ["a"], "add_labels": ["b"]}},
            {"tasks": ["WEB-1"], "changes": {"add_labels": ["has space"]}},
            {"tasks": ["WEB-1"], "changes": {"remove_labels": "x"}},
            {"tasks": ["WEB-1"], "changes": {"priority": "low"}, "atomic": "yes"},
            {"tasks": ["WEB-1"], "changes": {"priority": "low"}, "dry_run": True},
        ]
        for body in bad:
            r = self.client("alice").post("/api/tasks/bulk", body)
            self.assertEqual((r.status, r.error_code), (422, "validation_error"), body)
        self.assertEqual(self.task("WEB-1")["priority"], "medium")

    def test_at_most_a_hundred_tasks(self):
        keys = ["WEB-%d" % n for n in range(1, 102)]
        self.assertEqual(self.bulk(keys, {"priority": "low"}).status, 422)
        self.ok(self.bulk(keys[:100], {"priority": "low"}))  # the missing ones are reported, not fatal

    def test_viewers_cannot_use_it_and_anonymous_users_are_turned_away(self):
        self.assertEqual(self.bulk(["WEB-1"], {"priority": "low"}, user="vera").status, 403)
        self.assertEqual(self.client(token="bogus").post("/api/tasks/bulk", {}).status, 401)


class NotificationTests(BulkBase):
    def test_one_summary_message_replaces_a_message_per_task(self):
        self.ok(self.client("carol").post("/api/tasks/WEB-1/watchers", {}))
        self.ok(self.client("vera").post("/api/tasks/WEB-2/watchers", {}))
        self.notifier.messages.clear()
        self.ok(self.bulk(["WEB-1", "WEB-2", "WEB-3"], {"status": "done", "assignee": "bob"}))
        self.assertEqual(len(self.notifier.messages), 1)
        text = self.notifier.messages[0]
        self.assertIn("Alice updated 3 tasks", text)
        self.assertIn("status \u2192 done; assignee \u2192 bob", text)
        self.assertIn("*WEB-1*, *WEB-2*, *WEB-3*", text)
        self.assertIn("Watching: Carol, Vera", text)

    def test_long_selections_are_abbreviated_and_unchanged_runs_say_nothing(self):
        for n in range(5, 16):
            self.make_task("WEB", "Task %d" % n)
        self.notifier.messages.clear()
        self.ok(self.bulk(["WEB-%d" % n for n in range(1, 16)], {"priority": "high"}))
        self.assertIn("and 5 more", self.notifier.messages[0])
        self.notifier.messages.clear()
        self.ok(self.bulk(["WEB-1", "WEB-2"], {"priority": "high"}))
        self.assertEqual(self.notifier.messages, [])

    def test_with_a_public_url_the_keys_are_links(self):
        from dataclasses import replace

        self.app.config = replace(self.app.config, public_url="https://pm.example.com")
        self.ok(self.bulk(["WEB-1"], {"priority": "high"}))
        self.assertIn("[WEB-1](https://pm.example.com/#/t/WEB-1)", self.notifier.messages[0])
