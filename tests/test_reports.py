"""Reports: summary, health rules, portfolio, workload, burndown, critical path, digest.

Every expected number below was worked out by hand (see the comments), with "today" fixed at
Friday 2026-10-16.
"""

from datetime import datetime, timezone

from helpers import ApiTestCase


class ReportBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.client("alice")

    def project(self, key, tasks=(), **fields):
        self.make_project(key, **fields)
        return [self.make_task(key, "t%d" % i, **t)["key"] for i, t in enumerate(tasks, 1)]

    def summary(self, key):
        return self.ok(self.alice.get("/api/projects/%s/summary" % key))

    def health(self, key):
        return self.summary(key)["health"]


class SummaryTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.project("RPT", members=("bob", "carol"), start_date="2026-10-01", due_date="2026-10-31")
        mk = lambda title, **f: self.make_task("RPT", title, **f)["key"]  # noqa: E731
        mk("done a", status="done", estimate_hours=4, assignee="bob")  # 1
        mk("done b", status="done", estimate_hours=6)  # 2
        mk("working", status="in_progress", estimate_hours=10, assignee="bob", due_date="2026-10-20")  # 3
        mk("late, unassigned, unestimated", due_date="2026-10-14")  # 4
        mk("dropped", status="cancelled", estimate_hours=5)  # 5
        mk("stuck", status="blocked", estimate_hours=2, assignee="carol")  # 6
        mk("waits on 3", estimate_hours=3, assignee="carol")  # 7
        self.ok(self.alice.post("/api/tasks/RPT-7/dependencies", {"blocked_by": "RPT-3"}))
        self.ok(self.client("bob").post("/api/tasks/RPT-3/time", {"hours": 4}), 201)

    def test_counts_metrics_progress_and_hours(self):
        s = self.summary("RPT")
        self.assertEqual(s["counts"]["total"], 7)
        self.assertEqual(s["counts"]["by_status"], {"todo": 2, "in_progress": 1, "in_review": 0, "blocked": 1, "done": 2, "cancelled": 1})
        self.assertEqual(s["counts"]["by_priority"]["medium"], 7)
        # Non-cancelled leaves: 1,2,3,4,6,7 -> 6 tasks, 2 done, 4 open.
        self.assertEqual(
            s["metrics"],
            {
                "leaf_tasks": 6, "done": 2, "open": 4, "cancelled": 1, "overdue": 1, "due_soon": 1,
                "blocked": 2,  # RPT-6 (flagged) + RPT-7 (waiting on RPT-3): "cannot start yet"
                "flagged_blocked": 1, "delayed_by_blockers": 0,  # RPT-3 is not overdue, so RPT-7 is merely queued
                "unassigned": 1, "unestimated_open": 1,
            },
        )
        self.assertEqual(s["progress"], {"tasks_pct": 33.3, "hours_pct": 40.0})  # 2/6 ; done 10h of 25h
        # remaining = (10-4 logged) + 2 + 3 = 11 ; RPT-4 has no estimate
        self.assertEqual(s["hours"], {"estimated": 25, "logged": 4, "remaining_estimate": 11})

    def test_health_explains_every_rule_that_fired(self):
        h = self.health("RPT")
        self.assertEqual(h["status"], "off_track")  # 1 of 4 open tasks overdue = 25%
        self.assertEqual(
            h["reasons"],
            ["1 of 4 open tasks are overdue", "1 of 4 open tasks are flagged as blocked", "Behind schedule: 33% complete vs 50% of time elapsed"],
        )

    def test_parents_are_containers_not_double_counted(self):
        self.make_task("RPT", "Epic", type="epic", estimate_hours=100)  # RPT-8, big estimate of its own
        self.make_task("RPT", "kid 1", parent="RPT-8", status="done", estimate_hours=1)  # RPT-9
        self.make_task("RPT", "kid 2", parent="RPT-8", estimate_hours=1)  # RPT-10
        s = self.summary("RPT")
        self.assertEqual(s["counts"]["total"], 10)  # counts show everything on the board
        self.assertEqual(s["metrics"]["leaf_tasks"], 8)  # ... but progress counts leaves only
        self.assertEqual(s["hours"]["estimated"], 27)  # 25 + 1 + 1, not +100

    def test_next_milestone_is_the_earliest_unachieved(self):
        self.ok(self.alice.post("/api/projects/RPT/milestones", {"name": "Old", "due_date": "2026-10-01"}), 201)
        self.ok(self.alice.post("/api/projects/RPT/milestones", {"name": "Next", "due_date": "2026-10-25"}), 201)
        self.assertEqual(self.summary("RPT")["next_milestone"]["name"], "Old")  # overdue ones come first
        self.assertEqual(self.summary("RPT")["next_milestone"]["status"], "overdue")


class HealthRuleTests(ReportBase):
    def assert_health(self, key, status, reasons):
        h = self.health(key)
        self.assertEqual((h["status"], h["reasons"]), (status, reasons), key)

    def test_not_started_complete_and_on_hold(self):
        self.project("EMP")
        self.assert_health("EMP", "not_started", ["No tasks yet"])
        self.project("FIN", [{"status": "done"}, {"status": "cancelled"}])
        self.assert_health("FIN", "complete", [])
        self.project("HLD", [{}])
        self.ok(self.alice.patch("/api/projects/HLD", {"status": "on_hold"}))
        self.assert_health("HLD", "on_hold", ["Project is on hold"])

    def test_on_track(self):
        self.project("OK", [{"status": "done"}, {}, {}, {}], start_date="2026-10-01", due_date="2026-12-31")
        self.assert_health("OK", "on_track", [])

    def test_one_overdue_task_among_many_is_at_risk(self):
        self.project("R1", [{"due_date": "2026-10-10"}] + [{}] * 4)  # 1 of 5 = 20% < 25%
        self.assert_health("R1", "at_risk", ["1 task(s) overdue"])

    def test_overdue_critical_task_is_off_track(self):
        self.project("R2", [{"due_date": "2026-10-10", "priority": "critical"}] + [{}] * 9)
        self.assert_health("R2", "off_track", ["1 critical task(s) overdue", "1 task(s) overdue"])

    def test_quarter_of_open_tasks_overdue_is_off_track(self):
        self.project("R3", [{"due_date": "2026-10-10"}, {}, {}, {}])
        self.assert_health("R3", "off_track", ["1 of 4 open tasks are overdue"])

    def test_passed_project_deadline_is_off_track(self):
        self.project("R4", [{}, {}], due_date="2026-10-10")
        self.assert_health("R4", "off_track", ["Project due date (2026-10-10) has passed with 2 open task(s)"])

    def test_a_quarter_of_open_tasks_flagged_blocked_is_at_risk(self):
        self.project("R5", [{"status": "blocked"}, {}, {}, {}])
        self.assert_health("R5", "at_risk", ["1 of 4 open tasks are flagged as blocked"])

    def test_imminent_deadline_with_little_done_is_at_risk(self):
        self.project("R6", [{}, {}], due_date="2026-10-26")
        self.assert_health("R6", "at_risk", ["Due in 10 day(s) but only 0% complete"])

    def test_falling_behind_the_calendar_is_at_risk(self):
        # 15 of 45 days elapsed (33%), nothing finished, deadline still >14 days away
        self.project("R7", [{}, {}], start_date="2026-10-01", due_date="2026-11-15")
        self.assert_health("R7", "at_risk", ["Behind schedule: 0% complete vs 33% of time elapsed"])

    def test_waiting_on_a_predecessor_is_normal_sequencing_not_a_risk(self):
        # Regression: a plain dependency chain used to make every fresh plan look "at risk".
        self.project("R8", [{}, {}, {}, {}])
        for blocked, blocker in (("R8-2", "R8-1"), ("R8-3", "R8-2"), ("R8-4", "R8-3")):
            self.ok(self.alice.post("/api/tasks/%s/dependencies" % blocked, {"blocked_by": blocker}))
        self.assert_health("R8", "on_track", [])
        self.assertEqual(self.summary("R8")["metrics"]["blocked"], 3)  # still reported as "cannot start yet"

    def test_waiting_on_an_overdue_predecessor_is_a_risk(self):
        self.project("R9", [{"due_date": "2026-10-10"}] + [{}] * 5)  # 1 of 6 overdue (<25%)
        self.ok(self.alice.post("/api/tasks/R9-2/dependencies", {"blocked_by": "R9-1"}))
        self.assert_health("R9", "at_risk", ["1 task(s) overdue", "1 task(s) are waiting on overdue predecessors"])
        self.ok(self.set_status("R9-1", "done"))  # the late predecessor finishes -> the risk clears
        self.assert_health("R9", "on_track", [])


class PortfolioTests(ReportBase):
    def test_worst_projects_first_with_totals(self):
        self.project("GOOD", [{"status": "done"}, {}, {}, {}], start_date="2026-10-01", due_date="2026-12-31")
        self.project("BAD", [{"due_date": "2026-10-01", "priority": "critical"}, {}])
        self.project("OLD", [{}])
        self.ok(self.alice.patch("/api/projects/OLD", {"status": "archived"}))
        p = self.ok(self.client("vera").get("/api/reports/portfolio"))  # read-only role is enough
        self.assertEqual([x["key"] for x in p["projects"]], ["BAD", "GOOD"])  # archived hidden, worst first
        self.assertEqual(p["as_of"], "2026-10-16")
        self.assertEqual(p["totals"], {"projects": 2, "open_tasks": 5, "overdue_tasks": 1, "by_health": {"off_track": 1, "on_track": 1}})
        good = p["projects"][1]
        self.assertEqual((good["progress"]["tasks_pct"], good["open_tasks"], good["days_remaining"]), (25.0, 3, 76))
        self.assertEqual(good["owner"]["username"], "alice")


class WorkloadTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.make_project("WL", members=("bob", "carol"))
        mk = lambda title, **f: self.make_task("WL", title, **f)["key"]  # noqa: E731
        # bob: capacity 40h/week; window = 2 weeks -> 80h, ends 2026-10-30
        mk("a", assignee="bob", estimate_hours=30, due_date="2026-10-20")  # WL-1: 30 - 10 logged = 20h
        mk("b", assignee="bob", estimate_hours=50, due_date="2026-10-29")  # WL-2: 50h
        mk("c", assignee="bob", estimate_hours=100, due_date="2026-11-30")  # WL-3: after the window, excluded
        mk("d", assignee="bob", estimate_hours=8)  # WL-4: no date -> unscheduled
        mk("e", assignee="bob")  # WL-5: no estimate
        mk("f", assignee="bob", estimate_hours=6, due_date="2026-10-10")  # WL-6: overdue, 6h
        mk("g", assignee="bob", estimate_hours=2, status="done", due_date="2026-10-20")  # WL-7: closed, ignored
        mk("Epic", assignee="bob", estimate_hours=99, due_date="2026-10-20")  # WL-8: container, ignored
        mk("kid", assignee="bob", estimate_hours=4, due_date="2026-10-21", parent="WL-8")  # WL-9: 4h
        # carol: capacity 20h/week -> 40h in the window
        mk("h", assignee="carol", estimate_hours=30, due_date="2026-10-25")  # WL-10
        mk("i", assignee="carol", estimate_hours=20, due_date="2026-10-28")  # WL-11
        self.ok(self.client("bob").post("/api/tasks/WL-1/time", {"hours": 10}), 201)
        carol = self.ok(self.client("carol").get("/api/me"))
        self.ok(self.admin.patch("/api/users/%d" % carol["id"], {"weekly_capacity_hours": 20}))

    def row(self, username, **q):
        data = self.ok(self.alice.get("/api/reports/workload", query=q))
        return {i["user"]["username"]: i for i in data["items"]}[username]

    def test_bob_is_exactly_at_capacity_and_not_over(self):
        bob = self.row("bob")
        # 20 (WL-1) + 50 (WL-2) + 6 (WL-6 overdue) + 4 (WL-9) = 80h of 80h
        self.assertEqual((bob["scheduled_hours"], bob["capacity_hours"], bob["utilization_pct"]), (80, 80, 100.0))
        self.assertFalse(bob["overallocated"])  # the boundary is strict: 80 > 80 is false
        self.assertEqual((bob["open_tasks"], bob["overdue"], bob["unestimated_tasks"], bob["unscheduled_hours"]), (7, 1, 1, 8))

    def test_carol_is_overallocated(self):
        carol = self.row("carol")
        self.assertEqual((carol["scheduled_hours"], carol["capacity_hours"], carol["utilization_pct"]), (50, 40, 125.0))
        self.assertTrue(carol["overallocated"])

    def test_window_length_changes_capacity_and_what_is_in_scope(self):
        bob = self.row("bob", weeks=1)  # window ends 2026-10-23: WL-2 (10-29) drops out
        self.assertEqual((bob["scheduled_hours"], bob["capacity_hours"]), (30, 40))  # 20 + 6 + 4
        self.assertEqual(self.row("bob", weeks=12)["scheduled_hours"], 180)  # WL-3 (100h) now inside

    def test_sorted_by_utilisation_and_validated(self):
        data = self.ok(self.alice.get("/api/reports/workload"))
        self.assertEqual([i["user"]["username"] for i in data["items"]][:2], ["carol", "bob"])
        self.assertEqual(data["window_end"], "2026-10-30")
        for bad in ("0", "13", "x"):
            self.assertEqual(self.alice.get("/api/reports/workload", query={"weeks": bad}).status, 422)
        self.assertEqual(self.alice.get("/api/reports/workload", query={"project": "NOPE"}).status, 404)


class BurndownTests(ReportBase):
    def at(self, day, hour=10):
        self.now = datetime(2026, 10, day, hour, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        self.at(12)
        self.make_project("BD", start_date="2026-10-12", due_date="2026-10-18")
        for title, hours in (("one", 2), ("two", 4), ("three", 6)):  # BD-1..3 created Monday
            self.make_task("BD", title, estimate_hours=hours)
        self.at(13)
        self.make_task("BD", "four", estimate_hours=8)  # BD-4 created Tuesday
        self.at(14)
        self.set_status("BD-1", "done")
        self.at(15)
        self.set_status("BD-2", "done")
        self.set_status("BD-3", "cancelled")
        self.at(16)

    def days(self, **q):
        return self.ok(self.alice.get("/api/projects/BD/burndown", query=q))

    def test_task_count_series(self):
        b = self.days()
        self.assertEqual((b["from"], b["to"], b["metric"]), ("2026-10-12", "2026-10-18", "tasks"))
        remaining = [d["remaining"] for d in b["days"]]
        self.assertEqual(remaining, [3, 4, 3, 1, 1, None, None])  # Mon..Sun; the future is not projected
        self.assertEqual([d["scope"] for d in b["days"]], [3, 4, 4, 4, 4, None, None])
        self.assertEqual([d["completed"] for d in b["days"]], [0, 0, 1, 2, 2, None, None])
        self.assertEqual([d["ideal"] for d in b["days"]], [3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.0])
        self.assertEqual(b["days"][0]["date"], "2026-10-12")

    def test_hours_series(self):
        b = self.days(metric="hours")
        self.assertEqual([d["remaining"] for d in b["days"]][:5], [12, 20, 18, 8, 8])
        self.assertEqual([d["completed"] for d in b["days"]][:5], [0, 0, 2, 6, 6])
        self.assertEqual(b["unestimated_tasks"], 0)

    def test_custom_range_and_validation(self):
        b = self.days(**{"from": "2026-10-14", "to": "2026-10-15"})
        self.assertEqual([d["remaining"] for d in b["days"]], [3, 1])
        self.assertEqual(b["days"][0]["ideal"], 3.0)
        self.assertEqual(self.alice.get("/api/projects/BD/burndown", query={"metric": "story_points"}).status, 422)
        self.assertEqual(self.alice.get("/api/projects/BD/burndown", query={"from": "2026-10-15", "to": "2026-10-01"}).status, 422)
        self.assertEqual(self.alice.get("/api/projects/BD/burndown", query={"from": "2024-01-01", "to": "2026-10-01"}).status, 422)
        self.assertEqual(self.alice.get("/api/projects/BD/burndown", query={"from": "soon"}).status, 422)


class CriticalPathTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.make_project("CP", members=("bob",))
        for title, hours in (("A", 4), ("B", 6), ("C", 3), ("D", 5), ("E isolated", 2)):  # CP-1..5
            self.make_task("CP", title, estimate_hours=hours)
        for blocked, blocker in (("CP-2", "CP-1"), ("CP-3", "CP-1"), ("CP-4", "CP-2"), ("CP-4", "CP-3")):
            self.ok(self.alice.post("/api/tasks/%s/dependencies" % blocked, {"blocked_by": blocker}))

    def cpm(self):
        return self.ok(self.alice.get("/api/projects/CP/critical-path"))

    def by_key(self, data):
        return {t["key"]: t for t in data["tasks"]}

    def test_classic_forward_and_backward_pass(self):
        data = self.cpm()
        # A(4) -> B(6) -> D(5) = 15h ; A -> C(3) -> D has 3h of slack ; E has no dependencies
        self.assertEqual((data["total_hours"], data["path"]), (15, ["CP-1", "CP-2", "CP-4"]))
        t = self.by_key(data)
        self.assertNotIn("CP-5", t)
        self.assertEqual([t[k]["slack_hours"] for k in ("CP-1", "CP-2", "CP-3", "CP-4")], [0, 0, 3, 0])
        self.assertEqual([t[k]["critical"] for k in ("CP-1", "CP-2", "CP-3", "CP-4")], [True, True, False, True])
        self.assertEqual((t["CP-3"]["earliest_start"], t["CP-3"]["earliest_finish"], t["CP-3"]["latest_start"], t["CP-3"]["latest_finish"]), (4, 7, 7, 10))
        self.assertEqual([x["key"] for x in data["tasks"]], ["CP-1", "CP-2", "CP-3", "CP-4"])

    def test_logged_time_shortens_remaining_work_and_shifts_the_slack(self):
        self.ok(self.client("alice").post("/api/tasks/CP-2/time", {"hours": 2}), 201)
        data = self.cpm()  # B now has 4h left: A(4)+B(4)+D(5)=13 ; C path is 4+3+5=12 -> slack 1
        self.assertEqual(data["total_hours"], 13)
        self.assertEqual(self.by_key(data)["CP-3"]["slack_hours"], 1)

    def test_finished_blockers_drop_out_of_the_analysis(self):
        self.ok(self.set_status("CP-1", "done"))
        data = self.cpm()  # B(6)+D(5)=11 beats C(3)+D(5)=8
        self.assertEqual((data["total_hours"], data["path"]), (11, ["CP-2", "CP-4"]))

    def test_unestimated_tasks_use_the_default_duration_and_say_so(self):
        self.make_task("CP", "mystery")  # CP-6, no estimate
        self.ok(self.alice.post("/api/tasks/CP-6/dependencies", {"blocked_by": "CP-4"}))
        data = self.cpm()
        self.assertEqual(data["total_hours"], 23)  # 15 + default 8h
        self.assertEqual(data["path"], ["CP-1", "CP-2", "CP-4", "CP-6"])
        self.assertEqual(data["assumptions"]["default_task_hours"], 8.0)

    def test_a_parent_lasts_as_long_as_its_open_subtasks(self):
        self.make_task("CP", "Epic", type="epic")  # CP-6
        self.make_task("CP", "k1", parent="CP-6", estimate_hours=3)
        self.make_task("CP", "k2", parent="CP-6", estimate_hours=4)
        self.ok(self.alice.post("/api/tasks/CP-6/dependencies", {"blocked_by": "CP-4"}))
        data = self.cpm()
        epic = self.by_key(data)["CP-6"]
        self.assertEqual((epic["container"], epic["duration_hours"], data["total_hours"]), (True, 7, 22))

    def test_no_dependencies_means_no_critical_path(self):
        self.make_project("FLAT")
        self.make_task("FLAT", "solo", estimate_hours=5)
        data = self.ok(self.alice.get("/api/projects/FLAT/critical-path"))
        self.assertEqual((data["path"], data["tasks"], data["total_hours"]), ([], [], 0))


class DigestTests(ReportBase):
    def test_groups_by_assignee_with_overdue_first(self):
        self.make_project("DG", members=("bob", "carol"))
        mk = lambda title, **f: self.make_task("DG", title, **f)["key"]  # noqa: E731
        mk("late bob", assignee="bob", due_date="2026-10-14", priority="high")  # DG-1: 2 days overdue
        mk("late nobody", due_date="2026-10-15")  # DG-2
        mk("today bob", assignee="bob", due_date="2026-10-16")  # DG-3
        mk("soon carol", assignee="carol", due_date="2026-10-18")  # DG-4: 2 days away
        mk("later carol", assignee="carol", due_date="2026-10-25")  # DG-5: outside the 3-day window
        mk("finished", assignee="bob", due_date="2026-10-01", status="done")  # DG-6
        self.make_project("HOLD")
        self.make_task("HOLD", "ignored", due_date="2026-10-01")
        self.ok(self.alice.patch("/api/projects/HOLD", {"status": "on_hold"}))
        d = self.ok(self.client("bob").get("/api/reports/digest"))
        self.assertEqual((d["as_of"], d["days"], d["overdue_count"], d["due_soon_count"]), ("2026-10-16", 3, 2, 2))
        names = [g["assignee"]["username"] if g["assignee"] else None for g in d["groups"]]
        self.assertEqual(names, ["bob", "carol", None])
        bob, carol, nobody = d["groups"]
        self.assertEqual([(t["key"], t["days_overdue"]) for t in bob["overdue"]], [("DG-1", 2)])
        self.assertEqual([(t["key"], t["days_until_due"]) for t in bob["due_soon"]], [("DG-3", 0)])
        self.assertEqual([(t["key"], t["days_until_due"]) for t in carol["due_soon"]], [("DG-4", 2)])
        self.assertEqual([t["key"] for t in nobody["overdue"]], ["DG-2"])
        wide = self.ok(self.client("bob").get("/api/reports/digest", query={"days": 10}))
        self.assertEqual(wide["due_soon_count"], 3)
        self.assertEqual(self.client("bob").get("/api/reports/digest", query={"days": 99}).status, 422)
