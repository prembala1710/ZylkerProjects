"""Start-to-start / finish-to-finish dependencies with lag, deadlock detection over them, and informational task links."""

from helpers import ApiTestCase


class Base(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        for title in ("Design", "Build", "Test", "Launch"):  # WEB-1..4
            self.make_task("WEB", title)
        self.alice = self.client("alice")

    def dep(self, key, **body):
        return self.alice.post("/api/tasks/%s/dependencies" % key, body)

    def error(self, response, status, code):
        self.assertEqual((response.status, response.error_code), (status, code), response.text)
        return response.json["error"]

    def task(self, key):
        return self.ok(self.alice.get("/api/tasks/" + key))


class TypeTests(Base):
    def test_default_is_finish_to_start_without_lag(self):
        t = self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        entry = t["blocked_by"][0]
        self.assertEqual((entry["type"], entry["lag_days"], entry["schedule"]), ("finish_to_start", 0, None))
        self.assertEqual(self.task("WEB-1")["blocks"][0]["type"], "finish_to_start")

    def test_start_to_start_lets_the_task_start_once_the_blocker_has_started(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.assertEqual(self.task("WEB-2")["blocked_by_open"], 1)
        err = self.error(self.set_status("WEB-2", "in_progress"), 409, "blocked_by_open_tasks")
        self.assertEqual(err["details"]["links"], [{"task": "WEB-1", "type": "start_to_start"}])
        self.assertIn("must start first", err["message"])
        self.ok(self.set_status("WEB-1", "in_progress"))
        self.assertEqual(self.task("WEB-2")["blocked_by_open"], 0)
        self.ok(self.set_status("WEB-2", "in_progress"))  # the blocker is still running; that is the point
        self.ok(self.set_status("WEB-2", "done"))  # and the blocker need not be finished for this one to finish

    def test_a_blocker_that_is_flagged_blocked_counts_as_not_started(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.ok(self.set_status("WEB-1", "blocked"))
        self.error(self.set_status("WEB-2", "in_progress"), 409, "blocked_by_open_tasks")

    def test_a_cancelled_blocker_never_holds_anyone_up(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.ok(self.dep("WEB-3", blocked_by="WEB-1", type="finish_to_finish"))
        self.ok(self.set_status("WEB-1", "cancelled"))
        self.ok(self.set_status("WEB-2", "in_progress"))
        self.ok(self.set_status("WEB-3", "done"))

    def test_finish_to_finish_only_gates_finishing(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="finish_to_finish"))
        self.assertEqual(self.task("WEB-2")["blocked_by_open"], 1)
        self.ok(self.set_status("WEB-2", "in_progress"))
        self.ok(self.set_status("WEB-2", "in_review"))
        err = self.error(self.set_status("WEB-2", "done"), 409, "blocked_by_open_tasks")
        self.assertIn("must finish before this can finish", err["message"])
        self.ok(self.set_status("WEB-1", "done"))
        self.ok(self.set_status("WEB-2", "done"))

    def test_the_finish_to_start_message_is_unchanged(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        err = self.error(self.set_status("WEB-2", "in_progress"), 409, "blocked_by_open_tasks")
        self.assertEqual(err["message"], "WEB-2 is blocked by unfinished task(s): WEB-1")
        self.assertEqual(err["details"]["blockers"], ["WEB-1"])

    def test_several_kinds_of_blocker_are_all_reported(self):
        self.ok(self.dep("WEB-3", blocked_by="WEB-1"))
        self.ok(self.dep("WEB-3", blocked_by="WEB-2", type="start_to_start"))
        err = self.error(self.set_status("WEB-3", "done"), 409, "blocked_by_open_tasks")
        self.assertEqual(err["details"]["blockers"], ["WEB-1", "WEB-2"])
        self.ok(self.set_status("WEB-1", "done"))
        self.ok(self.set_status("WEB-2", "in_progress"))
        self.ok(self.set_status("WEB-3", "done"))

    def test_validation_of_type_and_lag(self):
        for body in ({"type": "start_to_finish"}, {"type": 3}, {"lag_days": 1.5}, {"lag_days": "2"}, {"lag_days": True}, {"lag_days": 366}, {"lag_days": -366}, {"lag_days": None}):
            r = self.dep("WEB-2", blocked_by="WEB-1", **body)
            self.assertEqual(r.status, 422, body)
        self.assertEqual(self.dep("WEB-2", type="start_to_start").status, 422)  # still needs exactly one end
        self.assertEqual(self.dep("WEB-2", blocked_by="WEB-1", blocks="WEB-3").status, 422)
        self.assertEqual(self.task("WEB-2")["blocked_by"], [])  # nothing was saved
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", lag_days=365))
        self.ok(self.dep("WEB-3", blocked_by="WEB-1", lag_days=-365))

    def test_blocks_direction_takes_a_type_too(self):
        self.ok(self.dep("WEB-1", blocks="WEB-2", type="finish_to_finish", lag_days=2))
        entry = self.task("WEB-2")["blocked_by"][0]
        self.assertEqual((entry["key"], entry["type"], entry["lag_days"]), ("WEB-1", "finish_to_finish", 2))

    def test_the_second_link_between_the_same_two_tasks_is_refused_whatever_its_type(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.error(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"), 409, "dependency_exists")

    def test_existing_dependencies_survive_as_plain_finish_to_start(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        with self.app.session(write=False) as ctx:
            row = ctx.conn.execute("SELECT type, lag_days FROM task_dependencies").fetchone()
        self.assertEqual(tuple(row), ("finish_to_start", 0))

    def test_history_records_type_and_lag(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start", lag_days=3))
        act = self.ok(self.alice.get("/api/tasks/WEB-2/activity"))["items"][0]
        self.assertEqual(act["action"], "task.dependency_added")
        self.assertEqual((act["details"]["type"], act["details"]["lag_days"]), ("start_to_start", 3))


class ScheduleTests(Base):
    """Lag and lead do not move any dates; they say when the dates *should* fall and flag the ones that do not."""

    def hint(self, key, index=0):
        return self.task(key)["blocked_by"][index]["schedule"]

    def test_finish_to_start_with_lag(self):
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"due_date": "2026-10-20"}))
        self.ok(self.alice.patch("/api/tasks/WEB-2", {"start_date": "2026-10-21"}))
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", lag_days=2))
        self.assertEqual(self.hint("WEB-2"), {"applies_to": "start", "not_before": "2026-10-22", "conflict": True})
        self.ok(self.alice.patch("/api/tasks/WEB-2", {"start_date": "2026-10-22"}))
        self.assertFalse(self.hint("WEB-2")["conflict"])

    def test_the_same_day_handover_is_allowed_and_a_lead_allows_overlap(self):
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"due_date": "2026-10-20"}))
        self.ok(self.alice.patch("/api/tasks/WEB-2", {"start_date": "2026-10-20"}))
        self.ok(self.alice.patch("/api/tasks/WEB-3", {"start_date": "2026-10-17"}))
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.ok(self.dep("WEB-3", blocked_by="WEB-1", lag_days=-3))
        self.assertFalse(self.hint("WEB-2")["conflict"])
        self.assertEqual(self.hint("WEB-3"), {"applies_to": "start", "not_before": "2026-10-17", "conflict": False})

    def test_start_to_start_looks_at_start_dates_and_finish_to_finish_at_due_dates(self):
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"start_date": "2026-10-05", "due_date": "2026-10-30"}))
        self.ok(self.alice.patch("/api/tasks/WEB-2", {"start_date": "2026-10-04", "due_date": "2026-11-01"}))
        self.ok(self.alice.patch("/api/tasks/WEB-3", {"start_date": "2026-10-10", "due_date": "2026-10-29"}))
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.ok(self.dep("WEB-3", blocked_by="WEB-1", type="finish_to_finish", lag_days=1))
        self.assertEqual(self.hint("WEB-2"), {"applies_to": "start", "not_before": "2026-10-05", "conflict": True})
        self.assertEqual(self.hint("WEB-3"), {"applies_to": "finish", "not_before": "2026-10-31", "conflict": True})

    def test_the_other_end_sees_the_same_hint(self):
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"due_date": "2026-10-20"}))
        self.ok(self.alice.patch("/api/tasks/WEB-2", {"start_date": "2026-10-18"}))
        self.ok(self.dep("WEB-1", blocks="WEB-2"))
        self.assertEqual(self.task("WEB-1")["blocks"][0]["schedule"], self.hint("WEB-2"))
        self.assertTrue(self.hint("WEB-2")["conflict"])

    def test_no_hint_without_the_anchoring_date_and_no_conflict_without_a_planned_date(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.assertIsNone(self.hint("WEB-2"))
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"due_date": "2026-10-20"}))
        self.assertEqual(self.hint("WEB-2"), {"applies_to": "start", "not_before": "2026-10-20", "conflict": False})

    def test_month_ends_and_leap_years_are_handled_by_the_calendar(self):
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"due_date": "2028-02-28"}))
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", lag_days=2))
        self.assertEqual(self.hint("WEB-2")["not_before"], "2028-03-01")


class UpdateTests(Base):
    def test_change_type_and_lag(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        t = self.ok(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {"type": "start_to_start", "lag_days": 4}))
        self.assertEqual((t["blocked_by"][0]["type"], t["blocked_by"][0]["lag_days"]), ("start_to_start", 4))
        t = self.ok(self.alice.patch("/api/tasks/WEB-1/dependencies/WEB-2", {"lag_days": -1}))  # from either end
        self.assertEqual((t["blocks"][0]["type"], t["blocks"][0]["lag_days"]), ("start_to_start", -1))
        first = self.ok(self.alice.get("/api/tasks/WEB-2/activity"))["items"][0]  # logged on the task the edit was made from
        self.assertEqual(first["action"], "task.dependency_updated")
        self.assertEqual(first["details"]["changes"], {"type": ["finish_to_start", "start_to_start"], "lag_days": [0, 4]})
        second = self.ok(self.alice.get("/api/tasks/WEB-1/activity"))["items"][0]
        self.assertEqual(second["details"]["changes"], {"lag_days": [4, -1]})

    def test_saving_the_same_values_changes_nothing(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", lag_days=2))
        before = len(self.ok(self.alice.get("/api/tasks/WEB-2/activity"))["items"])
        self.ok(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {"type": "finish_to_start", "lag_days": 2}))
        self.assertEqual(len(self.ok(self.alice.get("/api/tasks/WEB-2/activity"))["items"]), before)

    def test_errors(self):
        self.error(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {"lag_days": 1}), 404, "dependency_not_found")
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.assertEqual(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {}).status, 422)
        self.assertEqual(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {"type": "nope"}).status, 422)
        self.assertEqual(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {"owner": "x"}).status, 422)
        self.assertEqual(self.client("vera").patch("/api/tasks/WEB-2/dependencies/WEB-1", {"lag_days": 1}).status, 403)

    def test_a_type_change_that_would_create_a_deadlock_is_refused_and_leaves_the_link_alone(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.ok(self.dep("WEB-1", blocked_by="WEB-2", type="finish_to_finish"))  # fine, see the cycle tests
        err = self.error(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {"type": "finish_to_start"}), 409, "dependency_cycle")
        self.assertEqual(err["details"]["cycle"][0], err["details"]["cycle"][-1])
        self.assertEqual(self.task("WEB-2")["blocked_by"][0]["type"], "start_to_start")


class CycleTests(Base):
    def test_start_to_start_one_way_and_finish_to_finish_back_is_not_a_deadlock(self):
        # B starts after A starts, and A finishes after B finishes: A starts, B starts, B finishes, A finishes.
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.ok(self.dep("WEB-1", blocked_by="WEB-2", type="finish_to_finish"))

    def test_loops_of_a_single_kind_are_deadlocks(self):
        for kind in ("start_to_start", "finish_to_finish"):
            self.ok(self.dep("WEB-2", blocked_by="WEB-1", type=kind))
            err = self.error(self.dep("WEB-1", blocked_by="WEB-2", type=kind), 409, "dependency_cycle")
            self.assertEqual(err["details"]["cycle"], ["WEB-2", "WEB-1", "WEB-2"], kind)
            self.ok(self.alice.delete("/api/tasks/WEB-2/dependencies/WEB-1"))

    def test_mixed_loops_are_found(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))  # 1 finishes -> 2 starts
        self.error(self.dep("WEB-1", blocked_by="WEB-2", type="start_to_start"), 409, "dependency_cycle")  # 2 starts -> 1 starts
        self.error(self.dep("WEB-1", blocked_by="WEB-2", type="finish_to_finish"), 409, "dependency_cycle")  # 2 finishes -> 1 finishes
        self.assertEqual(self.task("WEB-1")["blocked_by"], [])

    def test_a_longer_loop_through_three_tasks(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.ok(self.dep("WEB-3", blocked_by="WEB-2", type="start_to_start"))
        err = self.error(self.dep("WEB-1", blocked_by="WEB-3", type="start_to_start"), 409, "dependency_cycle")
        self.assertEqual(err["details"]["cycle"], ["WEB-3", "WEB-1", "WEB-2", "WEB-3"])

    def test_subtasks_still_have_to_finish_before_their_parent(self):
        child = self.make_task("WEB", "Child", parent="WEB-1")
        # parent finishing waits for the child, so the parent cannot also make the child wait for the parent to finish
        self.error(self.dep(child["key"], blocked_by="WEB-1"), 409, "dependency_cycle")
        self.error(self.dep(child["key"], blocked_by="WEB-1", type="finish_to_finish"), 409, "dependency_cycle")
        # but a child may start with, or after, its parent starts
        self.ok(self.dep(child["key"], blocked_by="WEB-1", type="start_to_start"))
        # and a parent may be made to finish after something the child waits on
        self.ok(self.dep("WEB-1", blocked_by="WEB-2", type="finish_to_finish"))

    def test_reparenting_respects_the_new_edge_kinds(self):
        self.ok(self.dep("WEB-1", blocked_by="WEB-2", type="start_to_start"))  # 2 starts -> 1 starts
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"parent": "WEB-2"}))  # 1 finishes -> 2 finishes: no loop
        self.ok(self.alice.patch("/api/tasks/WEB-1", {"parent": None}))
        self.ok(self.alice.delete("/api/tasks/WEB-1/dependencies/WEB-2"))
        self.ok(self.dep("WEB-1", blocked_by="WEB-2"))  # 2 finishes -> 1 starts
        self.error(self.alice.patch("/api/tasks/WEB-1", {"parent": "WEB-2"}), 409, "dependency_cycle")

    def test_critical_path_only_follows_finish_to_start_links(self):
        for key in ("WEB-1", "WEB-2"):
            self.ok(self.alice.patch("/api/tasks/" + key, {"estimate_hours": 4}))
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        cp = self.ok(self.alice.get("/api/projects/WEB/critical-path"))
        self.assertEqual((cp["path"], cp["total_hours"]), ([], 0))
        self.ok(self.alice.delete("/api/tasks/WEB-2/dependencies/WEB-1"))
        self.ok(self.dep("WEB-2", blocked_by="WEB-1"))
        self.assertEqual(self.ok(self.alice.get("/api/projects/WEB/critical-path"))["path"], ["WEB-1", "WEB-2"])

    def test_waiting_on_a_started_blocker_is_not_reported_as_waiting(self):
        self.ok(self.dep("WEB-2", blocked_by="WEB-1", type="start_to_start"))
        self.assertEqual(self.ok(self.alice.get("/api/projects/WEB/summary"))["metrics"]["blocked"], 1)
        self.ok(self.set_status("WEB-1", "in_progress"))
        self.assertEqual(self.ok(self.alice.get("/api/projects/WEB/summary"))["metrics"]["blocked"], 0)


class LinkTests(Base):
    def link(self, key, type_, other, user="alice"):
        return self.client(user).post("/api/tasks/%s/links" % key, {"type": type_, "task": other})

    def links(self, key):
        return [(entry["type"], entry["task"]["key"]) for entry in self.task(key)["links"]]

    def test_each_side_reads_the_relationship_in_its_own_words(self):
        self.assertEqual(self.link("WEB-2", "duplicates", "WEB-1").status, 201)
        self.assertEqual(self.link("WEB-3", "cloned_from", "WEB-1").status, 201)
        self.assertEqual(self.link("WEB-4", "relates_to", "WEB-1").status, 201)
        self.assertEqual(self.links("WEB-2"), [("duplicates", "WEB-1")])
        self.assertEqual(self.links("WEB-1"), [("relates_to", "WEB-4"), ("duplicated_by", "WEB-2"), ("cloned_to", "WEB-3")])

    def test_the_inverse_wordings_can_be_used_when_creating_too(self):
        self.ok(self.link("WEB-1", "duplicated_by", "WEB-2"), 201)
        self.ok(self.link("WEB-1", "cloned_to", "WEB-3"), 201)
        self.assertEqual(self.links("WEB-2"), [("duplicates", "WEB-1")])
        self.assertEqual(self.links("WEB-3"), [("cloned_from", "WEB-1")])

    def test_relates_to_reads_the_same_from_both_ends(self):
        self.ok(self.link("WEB-1", "relates_to", "WEB-2"), 201)
        self.assertEqual((self.links("WEB-1"), self.links("WEB-2")), ([("relates_to", "WEB-2")], [("relates_to", "WEB-1")]))

    def test_the_response_is_the_updated_task(self):
        t = self.ok(self.link("WEB-1", "relates_to", "WEB-2"), 201)
        self.assertEqual((t["key"], t["links"][0]["task"]["title"], t["links"][0]["task"]["status"]), ("WEB-1", "Build", "todo"))

    def test_one_link_per_pair_whatever_the_direction_or_type(self):
        self.ok(self.link("WEB-1", "duplicates", "WEB-2"), 201)
        for args in (("WEB-1", "duplicates", "WEB-2"), ("WEB-2", "duplicates", "WEB-1"), ("WEB-1", "relates_to", "WEB-2"), ("WEB-2", "cloned_from", "WEB-1")):
            self.error(self.link(*args), 409, "link_exists")

    def test_validation(self):
        self.assertEqual(self.link("WEB-1", "blocks", "WEB-2").status, 422)
        self.assertEqual(self.link("WEB-1", "relates_to", "WEB-1").status, 422)
        self.assertEqual(self.link("WEB-1", "relates_to", "WEB-99").status, 422)
        self.assertEqual(self.link("WEB-1", "relates_to", "nonsense").status, 422)
        self.assertEqual(self.link("WEB-1", "relates_to", 5).status, 422)
        self.assertEqual(self.alice.post("/api/tasks/WEB-1/links", {"type": "relates_to"}).status, 422)
        self.assertEqual(self.alice.post("/api/tasks/WEB-1/links", {"task": "WEB-2"}).status, 422)
        self.assertEqual(self.link("WEB-99", "relates_to", "WEB-1").status, 404)
        self.assertEqual(self.task("WEB-1")["links"], [])

    def test_removing_from_either_end(self):
        self.ok(self.link("WEB-1", "duplicated_by", "WEB-2"), 201)
        t = self.ok(self.alice.delete("/api/tasks/WEB-2/links/WEB-1"))
        self.assertEqual((t["key"], t["links"]), ("WEB-2", []))
        self.assertEqual(self.links("WEB-1"), [])
        self.error(self.alice.delete("/api/tasks/WEB-2/links/WEB-1"), 404, "link_not_found")
        self.ok(self.link("WEB-1", "relates_to", "WEB-2"), 201)  # and the pair can be linked again
        self.ok(self.alice.delete("/api/tasks/WEB-1/links/WEB-2"))

    def test_links_across_projects_need_write_access_to_both(self):
        self.make_project("API", members=("carol",))
        self.make_task("API", "Clone target")
        self.ok(self.link("API-1", "cloned_from", "WEB-1"), 201)
        self.assertEqual(self.links("WEB-1"), [("cloned_to", "API-1")])
        self.make_task("API", "Second")
        r = self.link("WEB-2", "relates_to", "API-2", user="bob")  # bob is in WEB but not API
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        r = self.link("API-2", "relates_to", "WEB-2", user="bob")
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        self.assertEqual(self.link("WEB-2", "relates_to", "WEB-3", user="bob").status, 201)
        self.assertEqual(self.link("WEB-2", "relates_to", "WEB-4", user="vera").status, 403)

    def test_an_archived_project_cannot_be_linked_into(self):
        self.make_project("OLD", members=("bob",))
        self.make_task("OLD", "Legacy")
        self.ok(self.alice.patch("/api/projects/OLD", {"status": "archived"}))
        self.error(self.link("WEB-1", "relates_to", "OLD-1"), 409, "project_archived")

    def test_links_never_affect_the_workflow(self):
        self.ok(self.link("WEB-2", "duplicates", "WEB-1"), 201)
        self.ok(self.link("WEB-3", "relates_to", "WEB-1"), 201)
        self.ok(self.set_status("WEB-2", "in_progress"))
        self.ok(self.set_status("WEB-2", "done"))
        self.assertEqual(self.task("WEB-2")["blocked_by_open"], 0)
        self.ok(self.link("WEB-1", "relates_to", "WEB-4"), 201)

    def test_deleting_a_task_removes_its_links(self):
        self.ok(self.link("WEB-2", "duplicates", "WEB-1"), 201)
        self.ok(self.alice.delete("/api/tasks/WEB-2"), 204)
        self.assertEqual(self.task("WEB-1")["links"], [])

    def test_history(self):
        self.ok(self.link("WEB-2", "duplicates", "WEB-1"), 201)
        self.ok(self.alice.delete("/api/tasks/WEB-2/links/WEB-1"))
        acts = self.ok(self.alice.get("/api/tasks/WEB-2/activity"))["items"][:2]
        self.assertEqual([(a["action"], a["details"]) for a in acts][::-1], [
            ("task.link_added", {"type": "duplicates", "other": "WEB-1"}),
            ("task.link_removed", {"type": "duplicates", "other": "WEB-1"}),
        ])  # fmt: skip
