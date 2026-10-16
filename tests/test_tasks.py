"""Task creation, validation, workflow transitions, hierarchy, dependencies and deadlock detection."""

from helpers import ApiTestCase


class TaskBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.alice = self.client("alice")

    def patch(self, key, body, user="alice"):
        return self.client(user).patch("/api/tasks/" + key, body)

    def error(self, response, status, code):
        self.assertEqual((response.status, response.error_code), (status, code), response.text)
        return response.json["error"]


class CreateTests(TaskBase):
    def test_keys_are_sequential_per_project_and_defaults_apply(self):
        t1 = self.make_task("WEB", "First")
        t2 = self.make_task("WEB", "Second")
        self.make_project("API")
        t3 = self.make_task("API", "Other project")
        self.assertEqual((t1["key"], t2["key"], t3["key"]), ("WEB-1", "WEB-2", "API-1"))
        self.assertEqual((t1["status"], t1["priority"], t1["type"]), ("todo", "medium", "task"))
        self.assertEqual(t1["reporter"]["username"], "alice")
        self.assertIsNone(t1["assignee"])
        self.assertEqual(t1["subtasks"], {"total": 0, "done": 0, "open": 0})

    def test_deleted_task_numbers_are_never_reused(self):
        self.make_task("WEB", "a")
        self.ok(self.alice.delete("/api/tasks/WEB-1"), 204)
        self.assertEqual(self.make_task("WEB", "b")["key"], "WEB-2")

    def test_full_payload_and_label_normalisation(self):
        t = self.make_task(
            "WEB", "Login bug", type="bug", priority="high", assignee="bob", start_date="2026-10-01",
            due_date="2026-10-20", estimate_hours=6.5, labels=["Backend", "backend", "auth/ui"], external_ref="IP-55",
            description="Steps\n1. go",
        )  # fmt: skip
        self.assertEqual(t["labels"], ["auth/ui", "backend"])
        self.assertEqual((t["assignee"]["username"], t["estimate_hours"], t["external_ref"]), ("bob", 6.5, "IP-55"))
        self.assertFalse(t["is_overdue"])

    def test_validation_errors(self):
        post = lambda body: self.alice.post("/api/projects/WEB/tasks", body)  # noqa: E731
        d = lambda r: self.error(r, 422, "validation_error")["details"]  # noqa: E731
        self.assertIn("title", d(post({})))
        self.assertIn("title", d(post({"title": "   "})))
        self.assertIn("title", d(post({"title": "x" * 201})))
        self.assertIn("priority", d(post({"title": "x", "priority": "urgent"})))
        self.assertIn("status", d(post({"title": "x", "status": "wip"})))
        self.assertIn("estimate_hours", d(post({"title": "x", "estimate_hours": -1})))
        self.assertIn("estimate_hours", d(post({"title": "x", "estimate_hours": "3"})))
        self.assertIn("estimate_hours", d(post({"title": "x", "estimate_hours": True})))
        self.assertIn("due_date", d(post({"title": "x", "start_date": "2026-10-10", "due_date": "2026-10-09"})))
        self.assertIn("due_date", d(post({"title": "x", "due_date": "10/10/2026"})))
        self.assertIn("labels", d(post({"title": "x", "labels": ["has space"]})))
        self.assertIn("labels", d(post({"title": "x", "labels": "bug"})))
        self.assertEqual(d(post({"title": "x", "nope": 1}))["nope"], "unknown field")

    def test_assignee_must_be_an_active_project_member(self):
        r = self.alice.post("/api/projects/WEB/tasks", {"title": "x", "assignee": "vera"})
        self.assertIn("not a member", self.error(r, 422, "validation_error")["details"]["assignee"])
        r = self.alice.post("/api/projects/WEB/tasks", {"title": "x", "assignee": "ghost"})
        self.assertIn("assignee", self.error(r, 422, "validation_error")["details"])

    def test_malformed_requests(self):
        r = self.alice.request("POST", "/api/projects/WEB/tasks", raw=b"{nope", content_type="application/json")
        self.error(r, 400, "bad_request")
        r = self.alice.request("POST", "/api/projects/WEB/tasks", raw=b"[]", content_type="application/json")
        self.error(r, 400, "bad_request")
        r = self.alice.request("POST", "/api/projects/WEB/tasks", raw=b"title=x", content_type="text/plain")
        self.error(r, 415, "unsupported_media_type")
        self.error(self.alice.get("/api/tasks/not-a-key"), 400, "invalid_key")
        self.error(self.alice.get("/api/tasks/WEB-999"), 404, "task_not_found")

    def test_assignment_notifies_unless_self_assigned(self):
        self.make_task("WEB", "For bob", assignee="bob", priority="high", due_date="2026-10-20")
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("Assigned to *Bob* by Alice", self.notifier.messages[0])
        self.make_task("WEB", "Self", as_user="bob", assignee="bob")
        self.assertEqual(len(self.notifier.messages), 1)  # bob assigned himself: no ping


class WorkflowTests(TaskBase):
    def test_valid_transitions_and_completed_at(self):
        self.make_task("WEB", "T")
        t = self.ok(self.set_status("WEB-1", "in_progress"))
        self.assertIsNone(t["completed_at"])
        self.ok(self.set_status("WEB-1", "in_review"))
        t = self.ok(self.set_status("WEB-1", "done"))
        self.assertEqual(t["completed_at"], "2026-10-16T09:00:00Z")
        t = self.ok(self.set_status("WEB-1", "todo"))  # reopen
        self.assertIsNone(t["completed_at"])

    def test_invalid_transitions_are_rejected_with_the_allowed_set(self):
        self.make_task("WEB", "T")
        self.ok(self.set_status("WEB-1", "done"))
        err = self.error(self.set_status("WEB-1", "in_review"), 409, "invalid_transition")
        self.assertEqual(err["details"]["allowed"], ["todo", "in_progress"])
        self.make_task("WEB", "U")
        self.error(self.set_status("WEB-2", "in_review"), 409, "invalid_transition")  # todo -> in_review skips work

    def test_same_status_is_a_no_op(self):
        self.make_task("WEB", "T")
        before = self.ok(self.alice.get("/api/tasks/WEB-1"))
        after = self.ok(self.set_status("WEB-1", "todo"))
        self.assertEqual(before["updated_at"], after["updated_at"])
        acts = self.ok(self.alice.get("/api/tasks/WEB-1/activity"))["items"]
        self.assertEqual([a["action"] for a in acts], ["task.created"])

    def test_completion_and_blocking_notify(self):
        self.make_task("WEB", "Ship it")
        self.ok(self.set_status("WEB-1", "blocked"))
        self.ok(self.set_status("WEB-1", "in_progress"))
        self.ok(self.set_status("WEB-1", "done"))
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertIn("marked blocked", self.notifier.messages[0])
        self.assertIn("completed by Alice", self.notifier.messages[1])

    def test_changes_are_audited_with_old_and_new_values(self):
        self.make_task("WEB", "T")
        self.ok(self.patch("WEB-1", {"priority": "high", "title": "T2", "assignee": "bob", "labels": ["x"]}))
        self.ok(self.set_status("WEB-1", "in_progress"))
        acts = self.ok(self.alice.get("/api/tasks/WEB-1/activity"))["items"]
        self.assertEqual([a["action"] for a in acts], ["task.status_changed", "task.updated", "task.created"])
        changes = acts[1]["details"]["changes"]
        self.assertEqual(changes["priority"], ["medium", "high"])
        self.assertEqual(changes["assignee"], [None, "bob"])
        self.assertEqual(changes["labels"], [[], ["x"]])
        self.assertEqual(acts[0]["details"]["changes"]["status"], ["todo", "in_progress"])
        self.assertEqual(acts[0]["actor"]["username"], "alice")

    def test_date_order_is_enforced_on_update_too(self):
        self.make_task("WEB", "T", start_date="2026-10-10", due_date="2026-10-20")
        err = self.error(self.patch("WEB-1", {"due_date": "2026-10-01"}), 422, "validation_error")
        self.assertIn("due_date", err["details"])  # earlier than the existing start date
        err = self.error(self.patch("WEB-1", {"start_date": "2026-10-25"}), 422, "validation_error")
        self.assertIn("due_date", err["details"])  # later than the existing due date
        t = self.ok(self.patch("WEB-1", {"start_date": "2026-10-25", "due_date": "2026-10-30"}))  # both together: fine
        self.assertEqual((t["start_date"], t["due_date"]), ("2026-10-25", "2026-10-30"))
        t = self.ok(self.patch("WEB-1", {"start_date": None}))  # clearing one side is fine
        self.assertIsNone(t["start_date"])

    def test_unassigning_and_clearing_fields(self):
        self.make_task("WEB", "T", assignee="bob", due_date="2026-10-20", estimate_hours=3)
        t = self.ok(self.patch("WEB-1", {"assignee": None, "due_date": None, "estimate_hours": None}))
        self.assertEqual((t["assignee"], t["due_date"], t["estimate_hours"]), (None, None, None))
        self.error(self.patch("WEB-1", {"title": None}), 422, "validation_error")

    def test_overdue_flag_only_applies_to_open_tasks(self):
        self.make_task("WEB", "late", due_date="2026-10-15")
        self.make_task("WEB", "today", due_date="2026-10-16")
        self.assertTrue(self.ok(self.alice.get("/api/tasks/WEB-1"))["is_overdue"])
        self.assertFalse(self.ok(self.alice.get("/api/tasks/WEB-2"))["is_overdue"])
        self.ok(self.set_status("WEB-1", "done"))
        self.assertFalse(self.ok(self.alice.get("/api/tasks/WEB-1"))["is_overdue"])


class HierarchyTests(TaskBase):
    def setUp(self):
        super().setUp()
        self.make_task("WEB", "Epic", type="epic")  # WEB-1
        self.make_task("WEB", "Child A", parent="WEB-1")  # WEB-2
        self.make_task("WEB", "Child B", parent="WEB-1")  # WEB-3

    def test_parent_reports_subtask_counts_and_lists_them(self):
        t = self.ok(self.alice.get("/api/tasks/WEB-1"))
        self.assertEqual(t["subtasks"], {"total": 2, "done": 0, "open": 2})
        self.assertEqual([s["key"] for s in t["subtask_list"]], ["WEB-2", "WEB-3"])
        self.assertEqual(self.ok(self.alice.get("/api/tasks/WEB-2"))["parent"], "WEB-1")

    def test_parent_cannot_close_while_subtasks_are_open(self):
        err = self.error(self.set_status("WEB-1", "done"), 409, "open_subtasks")
        self.assertEqual(err["details"]["subtasks"], ["WEB-2", "WEB-3"])
        self.ok(self.set_status("WEB-2", "done"))
        self.ok(self.set_status("WEB-3", "cancelled"))  # cancelled counts as closed
        self.ok(self.set_status("WEB-1", "done"))

    def test_cannot_reopen_a_child_under_a_closed_parent(self):
        self.ok(self.set_status("WEB-2", "done"))
        self.ok(self.set_status("WEB-3", "done"))
        self.ok(self.set_status("WEB-1", "done"))
        self.error(self.set_status("WEB-2", "todo"), 409, "parent_closed")
        self.ok(self.set_status("WEB-1", "todo"))
        self.ok(self.set_status("WEB-2", "todo"))

    def test_cannot_add_open_subtask_to_closed_parent(self):
        self.ok(self.set_status("WEB-2", "done"))
        self.ok(self.set_status("WEB-3", "done"))
        self.ok(self.set_status("WEB-1", "done"))
        r = self.alice.post("/api/projects/WEB/tasks", {"title": "late", "parent": "WEB-1"})
        self.error(r, 409, "parent_closed")

    def test_parent_must_exist_and_share_the_project(self):
        self.make_project("API")
        other = self.make_task("API", "Elsewhere")
        err = self.error(self.patch("WEB-2", {"parent": other["key"]}), 422, "validation_error")
        self.assertIn("same project", err["details"]["parent"])
        self.error(self.patch("WEB-2", {"parent": "WEB-99"}), 422, "validation_error")
        self.error(self.patch("WEB-2", {"parent": "WEB-2"}), 422, "validation_error")

    def test_cannot_create_a_parent_loop(self):
        err = self.error(self.patch("WEB-1", {"parent": "WEB-2"}), 409, "dependency_cycle")
        self.assertEqual(err["details"]["cycle"][0], "WEB-1")

    def test_reparenting_moves_the_task_and_clears_with_null(self):
        t = self.ok(self.patch("WEB-3", {"parent": "WEB-2"}))
        self.assertEqual(t["parent"], "WEB-2")
        self.assertEqual(self.ok(self.alice.get("/api/tasks/WEB-1"))["subtasks"]["total"], 1)
        t = self.ok(self.patch("WEB-3", {"parent": None}))
        self.assertIsNone(t["parent"])

    def test_nesting_depth_is_limited(self):
        parent = "WEB-1"
        for level in range(2, 6):  # depth 2..5 allowed
            parent = self.make_task("WEB", "L%d" % level, parent=parent)["key"]
        r = self.alice.post("/api/projects/WEB/tasks", {"title": "too deep", "parent": parent})
        self.error(r, 409, "max_depth_exceeded")

    def test_deleting_a_parent_requires_cascade(self):
        err = self.error(self.alice.delete("/api/tasks/WEB-1"), 409, "has_subtasks")
        self.assertEqual(err["details"]["subtasks"], 2)
        self.ok(self.alice.delete("/api/tasks/WEB-1", query={"cascade": "true"}), 204)
        self.assertEqual(self.ok(self.alice.get("/api/tasks"))["total"], 0)


class DependencyTests(TaskBase):
    def setUp(self):
        super().setUp()
        for title in ("Design", "Build", "Test", "Launch"):  # WEB-1..4
            self.make_task("WEB", title)

    def dep(self, key, **body):
        return self.alice.post("/api/tasks/%s/dependencies" % key, body)

    def test_blocker_gates_starting_and_finishing(self):
        t = self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.assertEqual([b["key"] for b in t["blocked_by"]], ["WEB-1"])
        self.assertEqual(t["blocked_by_open"], 1)
        self.assertEqual([b["key"] for b in self.ok(self.alice.get("/api/tasks/WEB-1"))["blocks"]], ["WEB-2"])
        for status in ("in_progress", "done"):
            err = self.error(self.set_status("WEB-2", status), 409, "blocked_by_open_tasks")
            self.assertEqual(err["details"]["blockers"], ["WEB-1"])
        self.ok(self.set_status("WEB-2", "blocked"))  # flagging it blocked is always allowed
        self.ok(self.set_status("WEB-1", "done"))
        self.assertEqual(self.ok(self.alice.get("/api/tasks/WEB-2"))["blocked_by_open"], 0)
        self.ok(self.set_status("WEB-2", "in_progress"))

    def test_cancelled_blockers_no_longer_block(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.ok(self.set_status("WEB-1", "cancelled"))
        self.ok(self.set_status("WEB-2", "in_progress"))

    def test_blocks_direction_and_removal_in_either_direction(self):
        self.ok(self.dep("WEB-1", blocks="WEB-2"))
        self.assertEqual(self.ok(self.alice.get("/api/tasks/WEB-2"))["blocked_by"][0]["key"], "WEB-1")
        t = self.ok(self.alice.delete("/api/tasks/WEB-2/dependencies/WEB-1"))
        self.assertEqual(t["blocked_by"], [])
        self.error(self.alice.delete("/api/tasks/WEB-2/dependencies/WEB-1"), 404, "dependency_not_found")

    def test_invalid_dependencies(self):
        self.error(self.dep("WEB-1", blocked_by="WEB-1"), 422, "validation_error")
        self.error(self.dep("WEB-1", blocked_by="WEB-99"), 422, "validation_error")
        self.error(self.dep("WEB-1", blocked_by="nonsense"), 422, "validation_error")
        self.error(self.dep("WEB-1"), 422, "validation_error")
        self.error(self.dep("WEB-1", blocked_by="WEB-2", blocks="WEB-3"), 422, "validation_error")
        self.ok(self.dep("WEB-1", blocked_by="WEB-2"))
        self.error(self.dep("WEB-1", blocked_by="WEB-2"), 409, "dependency_exists")
        self.make_project("API")
        other = self.make_task("API", "x")
        self.error(self.dep("WEB-1", blocked_by=other["key"]), 422, "validation_error")

    def test_direct_and_transitive_cycles_are_rejected_with_the_path(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.ok(self.dep("WEB-3", blocked_by="WEB-2"))
        err = self.error(self.dep("WEB-1", blocked_by="WEB-2"), 409, "dependency_cycle")  # 2-cycle
        self.assertEqual(err["details"]["cycle"], ["WEB-2", "WEB-1", "WEB-2"])
        err = self.error(self.dep("WEB-1", blocked_by="WEB-3"), 409, "dependency_cycle")  # 3-cycle
        self.assertEqual(err["details"]["cycle"], ["WEB-3", "WEB-1", "WEB-2", "WEB-3"])
        self.ok(self.dep("WEB-4", blocked_by="WEB-1"))  # diamonds are fine
        self.ok(self.dep("WEB-4", blocked_by="WEB-3"))

    def test_a_parent_can_never_wait_for_its_own_subtask(self):
        child = self.make_task("WEB", "Sub", parent="WEB-1")  # WEB-5 under WEB-1
        self.error(self.dep(child["key"], blocked_by="WEB-1"), 409, "dependency_cycle")
        self.error(self.dep("WEB-1", blocks=child["key"]), 409, "dependency_cycle")
        self.ok(self.dep("WEB-1", blocked_by=child["key"]))  # redundant but harmless

    def test_longer_loop_through_the_hierarchy_is_caught(self):
        # parent WEB-1 -> WEB-2 -> child WEB-5 -> (child must finish before parent) -> WEB-1
        child = self.make_task("WEB", "Sub", parent="WEB-1")
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        err = self.error(self.dep(child["key"], blocked_by="WEB-2"), 409, "dependency_cycle")
        self.assertEqual(err["details"]["cycle"][0], "WEB-2")

    def test_reparenting_cannot_create_a_hidden_cycle(self):
        # WEB-1 waits for WEB-2 (edge WEB-2 -> WEB-1). Making WEB-1 a subtask of WEB-2 would
        # also require WEB-1 to finish before WEB-2: a deadlock, so it must be refused.
        self.ok(self.dep("WEB-1", blocked_by="WEB-2"))
        self.error(self.patch("WEB-1", {"parent": "WEB-2"}), 409, "dependency_cycle")

    def test_reparenting_in_the_same_direction_as_a_dependency_is_fine(self):
        # WEB-1 must finish before WEB-2, and a subtask finishing before its parent is the same order.
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.assertEqual(self.ok(self.patch("WEB-1", {"parent": "WEB-2"}))["parent"], "WEB-2")

    def test_deleting_a_task_removes_its_dependencies(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.ok(self.alice.delete("/api/tasks/WEB-1"), 204)
        t = self.ok(self.alice.get("/api/tasks/WEB-2"))
        self.assertEqual((t["blocked_by"], t["blocked_by_open"]), ([], 0))
        self.ok(self.set_status("WEB-2", "in_progress"))
