"""Baselines: freezing a project's plan and comparing the live project with it (``/api/projects/{key}/baselines``)."""

import dataclasses
import random
import sqlite3
from datetime import date, timedelta
from unittest import mock

from helpers import ApiTestCase
from webhook_helpers import WebhookCase

from zylker_pm.app import App
from zylker_pm.services import baselines


class BaselineCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.alice = self.client("alice")

    def post(self, key="WEB", user="alice", **body):
        return self.client(user).post("/api/projects/%s/baselines" % key, body)

    def baseline(self, key="WEB", user="alice", **body):
        return self.ok(self.post(key, user, **body), 201)

    def variance(self, baseline_id, key="WEB", user="alice"):
        return self.ok(self.client(user).get("/api/projects/%s/baselines/%s" % (key, baseline_id)))

    def sql(self, query, args=()):
        conn = sqlite3.connect(self.config.db_path)
        try:
            return conn.execute(query, args).fetchall()
        finally:
            conn.close()

    def reconfigure(self, **changes):
        self.config = dataclasses.replace(self.config, **changes)
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.alice = self.client("alice")

    def milestone(self, name, due):
        return self.ok(self.alice.post("/api/projects/WEB/milestones", {"name": name, "due_date": due}), 201)["id"]

    def edit(self, key, **fields):
        return self.ok(self.alice.patch("/api/tasks/" + key, fields))


class PlanScenario(BaselineCase):
    """One plan, one baseline on Friday 16 Oct 2026, and everything that can happen to a plan afterwards."""

    def setUp(self):
        super().setUp()
        t = self.make_task
        t("WEB", "Phase 1", type="epic", due_date="2026-11-06")  # 1, a container
        t("WEB", "Design", parent="WEB-1", start_date="2026-10-19", due_date="2026-10-23", estimate_hours=8)  # 2
        t("WEB", "Build", parent="WEB-1", start_date="2026-10-26", due_date="2026-11-06", estimate_hours=20)  # 3
        t("WEB", "Test", due_date="2026-11-13", estimate_hours=10)  # 4
        t("WEB", "Docs")  # 5: no dates and no estimate
        t("WEB", "Deploy", due_date="2026-11-20", estimate_hours=4)  # 6
        t("WEB", "Legacy", due_date="2026-11-01", estimate_hours=6)  # 7
        t("WEB", "Polish", due_date="2026-11-10", estimate_hours=5)  # 8
        t("WEB", "Early", due_date="2026-10-20", estimate_hours=2)  # 9: done before the baseline
        t("WEB", "Dropped long ago")  # 10: cancelled before the baseline
        self.ok(self.set_status("WEB-9", "done"))
        self.ok(self.set_status("WEB-10", "cancelled"))
        self.beta, self.ga, self.archive = (self.milestone("Beta", "2026-11-10"), self.milestone("GA", "2026-11-27"), self.milestone("Archive", "2026-12-04"))
        self.made = self.baseline(name="Kickoff", note="the plan we committed to")

    def after(self):
        """Wednesday 21 Oct: the plan has been through the wars."""
        self.advance_days(3)  # Monday 19th
        self.ok(self.set_status("WEB-2", "done"))  # finished four days before it was due
        self.advance_days(2)  # Wednesday 21st
        self.edit("WEB-3", due_date="2026-11-13")  # a week later
        self.ok(self.set_status("WEB-4", "cancelled"))
        self.ok(self.alice.delete("/api/tasks/WEB-7"), 204)
        self.make_task("WEB", "Polish A", parent="WEB-8", due_date="2026-11-10", estimate_hours=3)  # 11: WEB-8 becomes a container
        self.make_task("WEB", "Polish B", parent="WEB-8", due_date="2026-11-12", estimate_hours=4)  # 12
        self.edit("WEB-9", due_date="2026-11-02")  # it was done already: this changes nothing
        self.make_task("WEB", "Hardening", due_date="2026-11-25", estimate_hours=6)  # 13
        self.edit("WEB-5", due_date="2026-11-27", estimate_hours=3)  # it had no dates in the baseline
        self.ok(self.alice.patch("/api/milestones/%d" % self.beta, {"due_date": "2026-11-20"}))
        self.ok(self.alice.delete("/api/milestones/%d" % self.ga), 204)
        self.ok(self.alice.patch("/api/milestones/%d" % self.archive, {"due_date": "2026-12-01"}))
        self.retro = self.milestone("Retro", "2026-12-10")

    def test_what_a_baseline_records(self):
        self.assertEqual(
            self.made,
            {
                "id": 1,
                "project": "WEB",
                "name": "Kickoff",
                "note": "the plan we committed to",
                "created_at": "2026-10-16T09:00:00Z",
                "created_by": {"id": self.alice_id(), "username": "alice", "display_name": "Alice"},
                "tasks": 9,  # the cancelled one is not part of the plan
                "leaf_tasks": 8,
                "milestones": 3,
                "estimate_hours": 55.0,
                "finish": "2026-11-20",
            },
        )
        rows = self.sql("SELECT number, title, is_leaf, status, start_date, due_date, estimate_hours, completed_date FROM baseline_tasks ORDER BY number")
        self.assertEqual([r[0] for r in rows], [1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(rows[0], (1, "Phase 1", 0, "todo", None, "2026-11-06", None, None))
        self.assertEqual(rows[2], (3, "Build", 1, "todo", "2026-10-26", "2026-11-06", 20.0, None))
        self.assertEqual(rows[4], (5, "Docs", 1, "todo", None, None, None, None))
        self.assertEqual(rows[8], (9, "Early", 1, "done", None, "2026-10-20", 2.0, "2026-10-16"))
        self.assertEqual(self.sql("SELECT name, due_date FROM baseline_milestones ORDER BY milestone_id"), [("Beta", "2026-11-10"), ("GA", "2026-11-27"), ("Archive", "2026-12-04")])

    def alice_id(self):
        return self.sql("SELECT id FROM users WHERE username = 'alice'")[0][0]

    def test_taking_a_baseline_changes_nothing_about_the_project(self):
        self.assertEqual([r[0] for r in self.sql("SELECT action FROM activity WHERE action LIKE 'task.%' AND created_at > '2026-10-16T09:00:00Z'")], [])
        listing = self.ok(self.alice.get("/api/tasks", query={"project": "WEB", "limit": 200}))["items"]
        self.assertTrue(all(t["updated_at"] <= "2026-10-16T09:00:00Z" for t in listing))
        self.baseline(name="Second")
        self.assertEqual(self.ok(self.alice.get("/api/tasks", query={"project": "WEB", "limit": 200}))["items"], listing)

    def test_nothing_has_moved_straight_after_setting_it(self):
        v = self.variance(self.made["id"])
        self.assertEqual(
            v["summary"],
            {
                "tasks": {"planned": 8, "now": 8, "compared": 8, "on_plan": 7, "slipped": 0, "ahead": 0, "no_dates": 1, "added": 0, "dropped": 0, "split": 0},
                "slip": {"max_days": 0, "average_days": 0.0},
                "overdue": 0,
                "finish": {"baseline": "2026-11-20", "now": "2026-11-20", "shift_days": 0},
                "scope_hours": {"baseline": 55.0, "now": 55.0, "change": 0.0},
                "milestones": {"planned": 3, "on_plan": 3, "slipped": 0, "ahead": 0, "dropped": 0, "added": 0},
            },
        )
        self.assertEqual((v["added"], v["dropped"]), ([], []))

    def test_how_the_plan_has_drifted(self):
        self.after()
        v = self.variance(self.made["id"])
        self.assertEqual(v["as_of"], "2026-10-21")
        self.assertEqual(
            v["summary"],
            {
                "tasks": {"planned": 8, "now": 8, "compared": 5, "on_plan": 2, "slipped": 1, "ahead": 1, "no_dates": 1, "added": 3, "dropped": 2, "split": 1},
                "slip": {"max_days": 7, "average_days": 7.0},
                "overdue": 0,
                "finish": {"baseline": "2026-11-20", "now": "2026-11-27", "shift_days": 7},
                "scope_hours": {"baseline": 55.0, "now": 50.0, "change": -5.0},
                "milestones": {"planned": 3, "on_plan": 0, "slipped": 1, "ahead": 1, "dropped": 1, "added": 1},
            },
        )
        by_key = {r["key"]: r for r in v["tasks"]}
        self.assertEqual({k: (r["state"], r["slip_days"]) for k, r in by_key.items()},
                         {"WEB-2": ("ahead", -4), "WEB-3": ("slipped", 7), "WEB-5": ("no_dates", None), "WEB-6": ("on_plan", 0), "WEB-9": ("on_plan", 0)})
        self.assertEqual([r["key"] for r in v["tasks"]], ["WEB-3", "WEB-2", "WEB-6", "WEB-9", "WEB-5"], "worst slip first, then ahead, on plan, undated")
        build = by_key["WEB-3"]
        self.assertEqual((build["start_shift_days"], build["due_shift_days"]), (0, 7))
        self.assertEqual(build["baseline"], {"status": "todo", "start_date": "2026-10-26", "due_date": "2026-11-06", "finish": "2026-11-06", "estimate_hours": 20.0})
        self.assertEqual(build["now"], {"start_date": "2026-10-26", "due_date": "2026-11-13", "finish": "2026-11-13", "finished": False, "estimate_hours": 20.0})
        design = by_key["WEB-2"]  # done on the 19th: the finish is the day it was done, not the due date
        self.assertEqual((design["baseline"]["finish"], design["now"]["finish"], design["now"]["finished"], design["due_shift_days"]), ("2026-10-23", "2026-10-19", True, 0))
        early = by_key["WEB-9"]  # done before the baseline: moving its due date later does not count against it
        self.assertEqual((early["slip_days"], early["baseline"]["finish"], early["now"]["finish"], early["due_shift_days"]), (0, "2026-10-16", "2026-10-16", 13))
        docs = by_key["WEB-5"]  # no dates in the baseline, so nothing to compare
        self.assertEqual((docs["state"], docs["now"]["due_date"], docs["now"]["estimate_hours"]), ("no_dates", "2026-11-27", 3.0))
        self.assertEqual([(a["key"], a["title"], a["estimate_hours"], a["due_date"]) for a in v["added"]], [("WEB-11", "Polish A", 3.0, "2026-11-10"), ("WEB-12", "Polish B", 4.0, "2026-11-12"), ("WEB-13", "Hardening", 6.0, "2026-11-25")])
        self.assertEqual([(d["key"], d["title"], d["reason"], d["baseline"]) for d in v["dropped"]],
                         [("WEB-4", "Test", "cancelled", {"due_date": "2026-11-13", "estimate_hours": 10.0}), ("WEB-7", "Legacy", "deleted", {"due_date": "2026-11-01", "estimate_hours": 6.0})])
        self.assertEqual(
            v["milestones"],
            {
                "items": [
                    {"id": self.beta, "name": "Beta", "state": "slipped", "baseline_due_date": "2026-11-10", "due_date": "2026-11-20", "slip_days": 10},
                    {"id": self.ga, "name": "GA", "state": "dropped", "baseline_due_date": "2026-11-27", "due_date": None, "slip_days": None},
                    {"id": self.archive, "name": "Archive", "state": "ahead", "baseline_due_date": "2026-12-04", "due_date": "2026-12-01", "slip_days": -3},
                ],
                "added": [{"id": self.retro, "name": "Retro", "due_date": "2026-12-10"}],
            },
        )

    def test_the_baseline_itself_never_changes(self):
        before = self.sql("SELECT * FROM baseline_tasks ORDER BY number")
        self.after()
        self.assertEqual(self.sql("SELECT * FROM baseline_tasks ORDER BY number"), before)
        listed = self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"][0]
        self.assertEqual({k: listed[k] for k in ("tasks", "leaf_tasks", "estimate_hours", "finish", "milestones")}, {"tasks": 9, "leaf_tasks": 8, "estimate_hours": 55.0, "finish": "2026-11-20", "milestones": 3})

    def test_two_baselines_of_one_project_each_say_what_moved_since_they_were_set(self):
        self.after()
        later = self.baseline(name="After the wars")
        self.edit("WEB-6", due_date="2026-11-23")  # three days later than both baselines had it
        first, second = self.variance(self.made["id"]), self.variance(later["id"])
        self.assertEqual({r["key"]: r["slip_days"] for r in first["tasks"]}["WEB-6"], 3)
        self.assertEqual({r["key"]: r["slip_days"] for r in second["tasks"]}["WEB-6"], 3)
        self.assertEqual({r["key"]: r["slip_days"] for r in second["tasks"]}["WEB-3"], 0, "the second baseline already had the later date")
        self.assertEqual((second["summary"]["tasks"]["added"], second["summary"]["tasks"]["dropped"]), (0, 0))
        self.assertEqual(second["summary"]["tasks"]["planned"], 8)  # WEB-5, 6, 9, 3, 2, 11, 12, 13 are leaves now
        self.assertEqual([b["name"] for b in self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"]], ["After the wars", "Kickoff"], "newest first")

    def test_an_open_task_that_is_overdue_is_flagged_but_has_not_slipped_until_somebody_moves_it(self):
        self.advance_days(10)  # Monday 26 Oct: Design (due 23 Oct) is still open
        v = self.variance(self.made["id"])
        design = next(r for r in v["tasks"] if r["key"] == "WEB-2")
        self.assertEqual((design["overdue"], design["state"], design["slip_days"]), (True, "on_plan", 0))
        self.assertEqual(v["summary"]["overdue"], 1)
        self.edit("WEB-2", due_date="2026-10-28")
        design = next(r for r in self.variance(self.made["id"])["tasks"] if r["key"] == "WEB-2")
        self.assertEqual((design["overdue"], design["state"], design["slip_days"]), (False, "slipped", 5))

    def test_a_task_that_is_reopened_and_finished_again_is_measured_by_the_last_time(self):
        self.advance_days(1)
        self.ok(self.set_status("WEB-6", "done"))  # Deploy, due 20 Nov, done on the 17th
        self.assertEqual(self.variance(self.made["id"])["tasks"][0]["slip_days"], -34)
        self.advance_days(40)
        self.ok(self.set_status("WEB-6", "todo"))
        self.ok(self.set_status("WEB-6", "done"))  # done again on 26 Nov
        row = next(r for r in self.variance(self.made["id"])["tasks"] if r["key"] == "WEB-6")
        self.assertEqual((row["now"]["finish"], row["slip_days"]), ("2026-11-26", 6))

    def test_deleting_a_baseline(self):
        other = self.baseline(name="Other")
        self.ok(self.alice.delete("/api/projects/WEB/baselines/%d" % self.made["id"]), 204)
        self.assertEqual([b["name"] for b in self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"]], ["Other"])
        self.assertEqual(self.alice.get("/api/projects/WEB/baselines/%d" % self.made["id"]).status, 404)
        self.assertEqual(self.sql("SELECT COUNT(DISTINCT baseline_id) FROM baseline_tasks")[0][0], 1)
        self.assertEqual(self.sql("SELECT COUNT(DISTINCT baseline_id) FROM baseline_milestones")[0][0], 1)
        self.assertEqual(self.variance(other["id"])["summary"]["tasks"]["compared"], 8)

    def test_deleting_the_project_deletes_its_baselines(self):
        self.baseline(name="Second")
        self.ok(self.client("admin").delete("/api/projects/WEB", query={"confirm": "WEB"}), 204)
        self.assertEqual(self.sql("SELECT (SELECT COUNT(*) FROM baselines), (SELECT COUNT(*) FROM baseline_tasks), (SELECT COUNT(*) FROM baseline_milestones)")[0], (0, 0, 0))

    def test_history_records_who_set_and_deleted_a_baseline(self):
        self.ok(self.alice.delete("/api/projects/WEB/baselines/%d" % self.made["id"]), 204)
        entries = [e for e in self.ok(self.alice.get("/api/projects/WEB/activity", query={"limit": 200}))["items"] if e["action"].startswith("baseline.")]
        self.assertEqual(
            [(e["action"], e["actor"]["username"], e["task"], e["details"]) for e in reversed(entries)],
            [
                ("baseline.created", "alice", None, {"id": 1, "name": "Kickoff", "tasks": 9, "milestones": 3}),
                ("baseline.deleted", "alice", None, {"id": 1, "name": "Kickoff"}),
            ],
        )


class TimeZoneTests(BaselineCase):
    def test_a_task_is_finished_on_the_day_it_was_done_where_the_organisation_is(self):
        self.reconfigure(timezone="Asia/Kolkata")  # UTC+5:30
        self.make_task("WEB", "Late shift", due_date="2026-10-18")
        self.make_task("WEB", "Already done", due_date="2026-10-18")
        self.advance_days(0)
        self.now = self.now.replace(hour=20)  # 20:00 UTC on the 16th is 01:30 on the 17th in Kolkata
        self.ok(self.set_status("WEB-2", "done"))
        made = self.baseline(name="Night")
        self.assertEqual(self.sql("SELECT completed_date FROM baseline_tasks WHERE number = 2")[0][0], "2026-10-17")
        self.assertEqual(made["finish"], "2026-10-18")
        self.ok(self.set_status("WEB-1", "done"))  # the same moment
        row = next(r for r in self.variance(made["id"])["tasks"] if r["key"] == "WEB-1")
        self.assertEqual((row["now"]["finish"], row["slip_days"]), ("2026-10-17", -1))
        self.assertEqual(self.variance(made["id"])["as_of"], "2026-10-17")


class CompareTests(ApiTestCase):
    """``compare`` is a pure function: the odd corners are easier to reach without a database."""

    def task(self, number, **fields):
        base = {"number": number, "title": "T%d" % number, "status": "todo", "start_date": None, "due_date": None, "estimate_hours": None, "completed_date": None, "is_leaf": True}
        return {**base, **fields}

    def compare(self, planned, current, planned_milestones=(), current_milestones=(), today="2026-10-16"):
        return baselines.compare("WEB", list(planned), list(current), list(planned_milestones), list(current_milestones), today)

    def test_an_empty_project(self):
        r = self.compare([], [])
        self.assertEqual(r["summary"]["tasks"], {"planned": 0, "now": 0, "compared": 0, "on_plan": 0, "slipped": 0, "ahead": 0, "no_dates": 0, "added": 0, "dropped": 0, "split": 0})
        self.assertEqual((r["summary"]["finish"], r["summary"]["scope_hours"], r["summary"]["slip"]), ({"baseline": None, "now": None, "shift_days": None}, {"baseline": 0, "now": 0, "change": 0}, {"max_days": 0, "average_days": 0.0}))
        self.assertEqual((r["tasks"], r["added"], r["dropped"], r["milestones"]), ([], [], [], {"items": [], "added": []}))

    def test_a_task_without_dates_on_either_side_cannot_slip(self):
        r = self.compare([self.task(1)], [self.task(1)])
        self.assertEqual((r["tasks"][0]["state"], r["tasks"][0]["slip_days"]), ("no_dates", None))
        r = self.compare([self.task(1, due_date="2026-10-20")], [self.task(1)])  # the date was taken away
        self.assertEqual((r["tasks"][0]["state"], r["tasks"][0]["slip_days"]), ("no_dates", None))

    def test_a_done_task_without_a_recorded_day_falls_back_to_its_due_date(self):
        r = self.compare([self.task(1, due_date="2026-10-20")], [self.task(1, due_date="2026-10-22", status="done", completed_date=None)])
        self.assertEqual(r["tasks"][0]["slip_days"], 2)

    def test_a_task_that_was_done_when_the_baseline_was_set_is_measured_from_that_day(self):
        before = self.task(1, due_date="2026-10-20", status="done", completed_date="2026-10-14")
        r = self.compare([before], [self.task(1, due_date="2026-12-31", status="done", completed_date="2026-10-14")])
        self.assertEqual((r["tasks"][0]["slip_days"], r["tasks"][0]["state"]), (0, "on_plan"))

    def test_leaves_that_became_parents_and_parents_that_became_leaves(self):
        r = self.compare(
            [self.task(1), self.task(2, is_leaf=False)],
            [self.task(1, is_leaf=False), self.task(2), self.task(3, due_date="2026-10-20")],
        )
        self.assertEqual((r["summary"]["tasks"]["split"], r["summary"]["tasks"]["compared"]), (1, 0))
        self.assertEqual([a["key"] for a in r["added"]], ["WEB-2", "WEB-3"], "a parent that is a leaf now was never counted as work before")

    def test_a_task_that_was_cancelled_when_the_baseline_was_set_is_new_work_if_it_comes_back(self):
        r = self.compare([], [self.task(1, due_date="2026-10-20")])
        self.assertEqual([a["key"] for a in r["added"]], ["WEB-1"])

    def test_the_order_of_the_rows(self):
        planned = [self.task(n, due_date="2026-10-20") for n in range(1, 9)]
        current = [
            self.task(1, due_date="2026-10-21"),  # +1
            self.task(2, due_date="2026-10-20"),  # on plan
            self.task(3, due_date="2026-10-25"),  # +5
            self.task(4, due_date="2026-10-18"),  # -2
            self.task(5, due_date="2026-10-25"),  # +5, the same slip: the lower number first
            self.task(6, due_date="2026-10-10"),  # -10
            self.task(7),  # no dates now
            self.task(8, due_date="2026-10-20"),
        ]
        order = [(r["key"], r["slip_days"]) for r in self.compare(planned, current)["tasks"]]
        self.assertEqual(order, [("WEB-3", 5), ("WEB-5", 5), ("WEB-1", 1), ("WEB-6", -10), ("WEB-4", -2), ("WEB-2", 0), ("WEB-8", 0), ("WEB-7", None)])

    def test_the_average_slip_counts_only_the_tasks_that_slipped(self):
        planned = [self.task(n, due_date="2026-10-20") for n in (1, 2, 3, 4)]
        current = [self.task(1, due_date="2026-10-21"), self.task(2, due_date="2026-10-23"), self.task(3, due_date="2026-10-10"), self.task(4, due_date="2026-10-20")]
        s = self.compare(planned, current)["summary"]["slip"]
        self.assertEqual(s, {"max_days": 3, "average_days": 2.0})

    def test_milestones_are_matched_by_id_not_by_name(self):
        r = self.compare([], [], [{"id": 1, "name": "A", "due_date": "2026-10-20"}], [{"id": 1, "name": "A renamed", "due_date": "2026-10-19"}, {"id": 2, "name": "A", "due_date": "2026-10-20"}])
        self.assertEqual([(m["name"], m["state"], m["slip_days"]) for m in r["milestones"]["items"]], [("A renamed", "ahead", -1)])
        self.assertEqual([m["name"] for m in r["milestones"]["added"]], ["A"])

    def test_long_lists_are_cut_and_say_so(self):
        planned = [self.task(n, due_date="2026-10-20") for n in range(1, 8)]
        current = [self.task(n, due_date="2026-10-%d" % (21 + n)) for n in range(1, 8)] + [self.task(n, due_date="2026-11-01") for n in range(8, 14)]
        with mock.patch.object(baselines, "MAX_REPORT_TASKS", 3), mock.patch.object(baselines, "MAX_LISTED_CHANGES", 2):
            r = self.compare(planned, current)
        self.assertEqual((len(r["tasks"]), r["tasks_truncated"], len(r["added"])), (3, True, 2))
        self.assertEqual([x["key"] for x in r["tasks"]], ["WEB-7", "WEB-6", "WEB-5"], "the worst slips are the ones that are kept")
        self.assertEqual(r["summary"]["tasks"]["added"], 6, "the counts are of everything, not of what is listed")

    def test_a_list_that_just_fits_is_not_cut(self):
        planned = [self.task(n, due_date="2026-10-20") for n in range(1, 4)]
        current = [self.task(n, due_date="2026-10-21") for n in range(1, 4)] + [self.task(n, due_date="2026-11-01") for n in range(4, 6)]
        with mock.patch.object(baselines, "MAX_REPORT_TASKS", 3), mock.patch.object(baselines, "MAX_LISTED_CHANGES", 2):
            r = self.compare(planned, current)
        self.assertEqual((len(r["tasks"]), r["tasks_truncated"], len(r["added"])), (3, False, 2))


class ErrorAndAccessTests(BaselineCase):
    def setUp(self):
        super().setUp()
        self.make_task("WEB", "One", due_date="2026-10-30")

    def test_who_may_do_what(self):
        made = self.baseline(name="Mine")
        for user in ("admin", "alice", "bob", "carol", "vera"):
            r = self.client(user).get("/api/projects/WEB/baselines")
            self.assertEqual(r.status, 200, user)
            self.assertEqual(self.client(user).get("/api/projects/WEB/baselines/%d" % made["id"]).status, 200, user)
        for user in ("bob", "carol", "vera"):
            r = self.post(user=user, name="Nope")
            self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), user)
            r = self.client(user).delete("/api/projects/WEB/baselines/%d" % made["id"])
            self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), user)
        self.assertEqual(self.baseline(user="admin", name="By the admin")["created_by"]["username"], "admin")
        self.assertEqual(len(self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"]), 2)

    def test_not_without_signing_in(self):
        anon = self.client(token="nonsense")
        for method, path in (("GET", "/api/projects/WEB/baselines"), ("POST", "/api/projects/WEB/baselines"), ("GET", "/api/projects/WEB/baselines/1"), ("DELETE", "/api/projects/WEB/baselines/1")):
            self.assertEqual(self.client().request(method, path, {} if method == "POST" else None).status, 401, method)
            self.assertEqual(anon.request(method, path, {} if method == "POST" else None).status, 401, method)

    def test_unknown_things(self):
        for path in ("/api/projects/NOPE/baselines", "/api/projects/NOPE/baselines/1"):
            r = self.alice.get(path)
            self.assertEqual((r.status, r.error_code), (404, "project_not_found"), path)
        self.assertEqual(self.post(key="NOPE").status, 404)
        self.baseline()  # so that a number can be too big for the database, not only unknown
        too_big = ("9223372036854775808", "99999999999999999999", "1" * 5000)  # past what SQLite can hold: a 404, not a crash
        for bad in ("99", "abc", "1.5", "-1", "0", "9223372036854775807", "-9223372036854775809", *too_big):
            for method in ("get", "delete"):
                r = getattr(self.alice, method)("/api/projects/WEB/baselines/" + bad)
                self.assertEqual((r.status, r.error_code), (404, "baseline_not_found"), (method, bad[:30]))
        self.assertEqual(len(self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"]), 1, "nothing was deleted")
        r = self.alice.delete("/api/projects/WEB/baselines/99")
        self.assertEqual((r.status, r.error_code), (404, "baseline_not_found"))

    def test_a_baseline_of_one_project_cannot_be_read_or_deleted_through_another(self):
        self.make_project("OPS", members=("bob",))
        made = self.baseline(name="Mine")
        self.assertEqual(self.alice.get("/api/projects/OPS/baselines/%d" % made["id"]).status, 404)
        self.assertEqual(self.alice.delete("/api/projects/OPS/baselines/%d" % made["id"]).status, 404)
        self.assertEqual(self.ok(self.alice.get("/api/projects/OPS/baselines"))["items"], [])
        self.assertEqual(self.alice.get("/api/projects/WEB/baselines/%d" % made["id"]).status, 200)

    def test_the_project_key_may_be_written_in_lower_case(self):
        made = self.baseline(key="web", name="Mine")
        self.assertEqual(made["project"], "WEB")
        self.assertEqual(self.alice.get("/api/projects/web/baselines/%d" % made["id"]).json["project"], "WEB")

    def test_an_archived_project_can_be_read_but_not_changed(self):
        made = self.baseline(name="Mine")
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "archived"}))
        r = self.post(name="Late")
        self.assertEqual((r.status, r.error_code), (409, "project_archived"))
        r = self.alice.delete("/api/projects/WEB/baselines/%d" % made["id"])
        self.assertEqual((r.status, r.error_code), (409, "project_archived"))
        self.assertEqual(self.variance(made["id"])["summary"]["tasks"]["compared"], 1)
        self.assertEqual(len(self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"]), 1)

    def test_names(self):
        self.assertEqual(self.baseline()["name"], "Baseline 1")
        self.assertEqual(self.baseline()["name"], "Baseline 2")
        self.assertEqual(self.baseline(name="  Kickoff  ", note="  why  ")["name"], "Kickoff")
        self.assertEqual(self.baseline(name="")["name"], "Baseline 4")
        r = self.post(name="kickoff")
        self.assertEqual((r.status, r.error_code), (409, "baseline_name_taken"), "the same name ignoring case")
        self.assertEqual(self.post(name="BASELINE 2").status, 409)
        self.make_project("OPS", members=("bob",))
        self.assertEqual(self.baseline(key="OPS", name="Kickoff")["name"], "Kickoff", "another project may use it")
        self.assertEqual(self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"][2]["note"], "", "the note defaults to empty")

    def test_a_default_name_never_collides_with_a_name_somebody_chose(self):
        self.baseline(name="Baseline 2")
        self.assertEqual(self.baseline()["name"], "Baseline 3")  # one exists, so the next would be 2: taken

    def test_validation(self):
        cases = [
            ({"name": "x" * 61}, "name"),
            ({"name": "bad\x07name"}, "name"),
            ({"name": "line\nbreak"}, "name"),
            ({"name": "evil\u202ename"}, "name"),
            ({"name": 5}, "name"),
            ({"note": "n" * 501}, "note"),
            ({"note": "bell\x07"}, "note"),
            ({"note": ["a"]}, "note"),
            ({"colour": "red"}, "colour"),
        ]
        for body, field in cases:
            r = self.post(**body)
            self.assertEqual((r.status, r.error_code), (422, "validation_error"), body)
            self.assertIn(field, r.json["error"]["details"], body)
        r = self.post(name="x" * 61, note="n" * 501, colour="red")
        self.assertEqual(sorted(r.json["error"]["details"]), ["colour", "name", "note"], "every problem at once")
        self.assertEqual(self.client("alice").request("POST", "/api/projects/WEB/baselines", ["a"]).status, 400)
        self.assertEqual(self.baseline(name="x" * 60)["name"], "x" * 60)
        self.assertEqual(self.baseline(name="With ünïcode ✓", note="two\nlines\tand a tab")["note"], "two\nlines\tand a tab")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM baselines")[0][0], 2, "refused requests stored nothing")

    def test_at_most_ten_baselines(self):
        ids = [self.baseline()["id"] for _ in range(10)]
        r = self.post()
        self.assertEqual((r.status, r.error_code), (409, "too_many_baselines"))
        self.assertEqual(self.ok(self.alice.get("/api/projects/WEB/baselines"))["limits"], {"max_baselines": 10})
        self.ok(self.alice.delete("/api/projects/WEB/baselines/%d" % ids[2]), 204)
        self.assertEqual(self.baseline()["name"], "Baseline 11")
        self.assertEqual(self.post().status, 409)

    def test_a_project_too_big_to_baseline_is_refused_and_nothing_is_stored(self):
        self.make_task("WEB", "Two")
        with mock.patch.object(baselines, "MAX_BASELINE_TASKS", 1):
            r = self.post(name="Big")
        self.assertEqual((r.status, r.error_code), (409, "baseline_too_large"))
        self.assertEqual(self.sql("SELECT (SELECT COUNT(*) FROM baselines), (SELECT COUNT(*) FROM baseline_tasks)")[0], (0, 0))
        with mock.patch.object(baselines, "MAX_BASELINE_TASKS", 2):
            self.assertEqual(self.post(name="Just right").status, 201)

    def test_an_empty_project_can_be_baselined(self):
        self.make_project("OPS", members=("bob",))
        made = self.baseline(key="OPS", name="Empty")
        self.assertEqual((made["tasks"], made["milestones"], made["estimate_hours"], made["finish"]), (0, 0, 0.0, None))
        v = self.variance(made["id"], key="OPS")
        self.assertEqual(v["summary"]["tasks"]["planned"], 0)

    def test_a_database_without_the_tables_says_so_until_the_server_restarts(self):
        self.sql("SELECT 1")
        conn = sqlite3.connect(self.config.db_path)
        conn.executescript("DROP TABLE baseline_milestones; DROP TABLE baseline_tasks; DROP TABLE baselines;")
        conn.close()
        for response in (self.alice.get("/api/projects/WEB/baselines"), self.post(name="x"), self.alice.get("/api/projects/WEB/baselines/1")):
            self.assertEqual((response.status, response.error_code), (409, "baselines_not_initialised"))
        self.reconfigure()  # a new App creates them again
        self.assertEqual(self.baseline(name="Back")["name"], "Back")

    def test_the_tables_are_only_created_once(self):
        self.baseline(name="Mine")
        self.reconfigure()
        self.reconfigure()
        self.assertEqual([b["name"] for b in self.ok(self.alice.get("/api/projects/WEB/baselines"))["items"]], ["Mine"])

    def test_a_project_of_a_few_hundred_tasks_is_compared_in_full(self):
        for n in range(300):
            self.make_task("WEB", "Task %d" % n, due_date="2026-11-%02d" % (1 + n % 28), estimate_hours=1 + n % 5)
        made = self.baseline(name="Big")
        self.assertEqual(made["tasks"], 301)
        v = self.variance(made["id"])
        self.assertEqual((v["summary"]["tasks"]["compared"], len(v["tasks"])), (301, 301))


class WebhookTests(WebhookCase):
    def test_baseline_events_can_be_subscribed_to_and_are_queued(self):
        self.make_project("WEB")
        hook = self.make_hook(name="Plans", events=["baseline.*"])
        catalogue = self.ok(self.admin.get("/api/webhooks/events"))
        self.assertIn("baseline.created", catalogue["events"])
        self.assertIn("baseline.*", catalogue["patterns"])
        made = self.ok(self.client("alice").post("/api/projects/WEB/baselines", {"name": "Kickoff"}), 201)
        self.ok(self.client("alice").delete("/api/projects/WEB/baselines/%d" % made["id"]), 204)
        self.assertEqual(self.events_queued(hook["id"]), ["baseline.created", "baseline.deleted"])


# ------------------------------------------------------------------------------------------------ an independent oracle


class OracleTests(BaselineCase):
    """Random projects, random things happening to them, and a second implementation of the rules written from the
    README: the answer of the API has to be the answer of the oracle, for every row and every total."""

    DAYS = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(75)]

    def listing(self):
        items = self.ok(self.alice.get("/api/tasks", query={"project": "WEB", "limit": 200}))["items"]
        return {
            t["number"]: {
                "title": t["title"],
                "status": t["status"],
                "start": t["start_date"],
                "due": t["due_date"],
                "hours": t["estimate_hours"],
                "done_on": t["completed_at"][:10] if t["status"] == "done" else None,  # the organisation is on UTC here
                "leaf": t["subtasks"]["total"] == 0,
            }
            for t in items
        }

    def milestones(self):
        return {m["id"]: (m["name"], m["due_date"]) for m in self.ok(self.alice.get("/api/projects/WEB/milestones"))["items"]}

    @staticmethod
    def finish(t):
        return t["done_on"] if t["status"] == "done" and t["done_on"] else t["due"]

    def build(self, rng):
        def dates():
            if rng.random() < 0.2:
                return {}
            start = rng.choice(self.DAYS[:50])
            due = rng.choice([d for d in self.DAYS if d >= start])
            return {k: v for k, v in (("start_date", start if rng.random() < 0.6 else None), ("due_date", due)) if v}

        for p in range(3):
            self.make_task("WEB", "Parent %d" % p, type="epic")
        for n in range(1, 4):
            for c in range(2):
                self.make_task("WEB", "Child %d.%d" % (n, c), parent="WEB-%d" % n, estimate_hours=rng.choice([1, 2, 3, 5, 8]), **dates())
        for n in range(10):
            self.make_task("WEB", "Task %d" % n, estimate_hours=rng.choice([None, 1, 2, 4, 8, 13]), **dates())
        for n in sorted(self.listing()):
            if rng.random() < 0.15 and self.listing()[n]["leaf"]:
                self.set_status("WEB-%d" % n, rng.choice(["done", "cancelled"]))
        for _ in range(3):
            self.milestone("M%d" % _, rng.choice(self.DAYS))

    def mutate(self, rng, steps):
        for _ in range(steps):
            snapshot = self.listing()
            leaves = [n for n, t in snapshot.items() if t["leaf"]]
            op, n = rng.choice(["due", "due", "start", "hours", "done", "done", "cancel", "reopen", "delete", "add", "split", "wait", "milestone"]), rng.choice(leaves)
            key = "WEB-%d" % n
            if op == "due":
                self.alice.patch("/api/tasks/" + key, {"due_date": rng.choice(self.DAYS)})
            elif op == "start":
                self.alice.patch("/api/tasks/" + key, {"start_date": rng.choice(self.DAYS)})
            elif op == "hours":
                self.alice.patch("/api/tasks/" + key, {"estimate_hours": rng.choice([None, 1, 2, 4, 8])})
            elif op in ("done", "cancel", "reopen"):
                self.set_status(key, {"done": "done", "cancel": "cancelled", "reopen": "todo"}[op])
            elif op == "delete":
                self.alice.delete("/api/tasks/" + key)
            elif op == "add":
                self.make_task("WEB", "New %d" % rng.randint(0, 999), estimate_hours=rng.choice([None, 2, 6]), due_date=rng.choice(self.DAYS))
            elif op == "split":
                self.client("alice").post("/api/projects/WEB/tasks", {"title": "Sub %d" % rng.randint(0, 999), "parent": key, "due_date": rng.choice(self.DAYS), "estimate_hours": 2})
            elif op == "wait":
                self.advance_days(rng.randint(0, 4))
            elif op == "milestone":
                ids = list(self.milestones())
                if ids:
                    self.alice.patch("/api/milestones/%d" % rng.choice(ids), {"due_date": rng.choice(self.DAYS)})

    def expected(self, base, now, base_milestones, now_milestones, today):
        leaves = {n: t for n, t in base.items() if t["leaf"]}  # ``base`` never held a cancelled task
        rows, dropped, split = {}, set(), 0
        for n, before in leaves.items():
            after = now.get(n)
            if after is None or after["status"] == "cancelled":
                dropped.add(n)
            elif not after["leaf"]:
                split += 1
            else:
                a, b = self.finish(before), self.finish(after)
                slip = (date.fromisoformat(b) - date.fromisoformat(a)).days if a and b else None
                rows[n] = slip
        added = {n for n, t in now.items() if t["leaf"] and t["status"] != "cancelled" and n not in leaves}
        live = {n: t for n, t in now.items() if n in rows or n in added}
        finishes_before = [self.finish(t) for t in leaves.values() if self.finish(t)]
        finishes_now = [self.finish(t) for t in live.values() if self.finish(t)]
        slipped = [s for s in rows.values() if s is not None and s > 0]
        ms = {i: ("dropped" if i not in now_milestones else (date.fromisoformat(now_milestones[i][1]) - date.fromisoformat(d)).days) for i, (_, d) in base_milestones.items()}
        return {
            "rows": rows,
            "dropped": dropped,
            "added": added,
            "split": split,
            "slipped": len(slipped),
            "ahead": sum(1 for s in rows.values() if s is not None and s < 0),
            "on_plan": sum(1 for s in rows.values() if s == 0),
            "no_dates": sum(1 for s in rows.values() if s is None),
            "max": max(slipped, default=0),
            "average": round(sum(slipped) / len(slipped), 1) if slipped else 0.0,
            "overdue": sum(1 for t in live.values() if t["status"] in ("todo", "in_progress", "in_review", "blocked") and t["due"] and t["due"] < today),
            "finish": (max(finishes_before, default=None), max(finishes_now, default=None)),
            "hours": (round(sum(t["hours"] or 0 for t in leaves.values()), 2), round(sum(t["hours"] or 0 for t in live.values()), 2)),
            "milestones": ms,
            "milestones_added": sorted(set(now_milestones) - set(base_milestones)),
        }

    def check(self, baseline_id, base, base_milestones, seed):
        today = (self.now).date().isoformat()
        want = self.expected(base, self.listing(), base_milestones, self.milestones(), today)
        got = self.variance(baseline_id)
        s, t = got["summary"], got["summary"]["tasks"]
        context = "seed %d" % seed
        self.assertEqual({int(r["key"].split("-")[1]): r["slip_days"] for r in got["tasks"]}, want["rows"], context)
        self.assertEqual({int(r["key"].split("-")[1]) for r in got["dropped"]}, want["dropped"], context)
        self.assertEqual({int(r["key"].split("-")[1]) for r in got["added"]}, want["added"], context)
        self.assertEqual((t["slipped"], t["ahead"], t["on_plan"], t["no_dates"], t["added"], t["dropped"], t["split"]), (want["slipped"], want["ahead"], want["on_plan"], want["no_dates"], len(want["added"]), len(want["dropped"]), want["split"]), context)
        self.assertEqual((t["planned"], t["compared"], t["now"]), (len([1 for x in base.values() if x["leaf"]]), len(want["rows"]), len(want["rows"]) + len(want["added"])), context)
        self.assertEqual((s["slip"]["max_days"], s["slip"]["average_days"], s["overdue"]), (want["max"], want["average"], want["overdue"]), context)
        self.assertEqual((s["finish"]["baseline"], s["finish"]["now"]), want["finish"], context)
        self.assertEqual((s["scope_hours"]["baseline"], s["scope_hours"]["now"]), want["hours"], context)
        self.assertEqual({m["id"]: (m["slip_days"] if m["state"] != "dropped" else "dropped") for m in got["milestones"]["items"]}, want["milestones"], context)
        self.assertEqual([m["id"] for m in got["milestones"]["added"]], want["milestones_added"], context)
        order = [(r["state"], -abs(r["slip_days"] or 0), r["number"]) for r in got["tasks"]]
        self.assertEqual(order, sorted(order, key=lambda o: ({"slipped": 0, "ahead": 1, "on_plan": 2, "no_dates": 3}[o[0]], o[1], o[2])), context)

    def test_the_api_agrees_with_the_oracle_on_random_projects(self):
        for seed in range(1, 9):
            with self.subTest(seed=seed):
                self.tearDown_project()
                rng = random.Random(seed)
                self.build(rng)
                first_base = {n: t for n, t in self.listing().items() if t["status"] != "cancelled"}
                first_ms = self.milestones()
                first = self.baseline(name="First")
                self.check(first["id"], first_base, first_ms, seed)  # nothing has happened yet
                self.mutate(rng, 14)
                self.check(first["id"], first_base, first_ms, seed)
                second_base = {n: t for n, t in self.listing().items() if t["status"] != "cancelled"}
                second_ms = self.milestones()
                second = self.baseline(name="Second")
                self.mutate(rng, 14)
                self.check(second["id"], second_base, second_ms, seed)
                self.check(first["id"], first_base, first_ms, seed)  # the first one still says what it said

    def tearDown_project(self):
        self.ok(self.client("admin").delete("/api/projects/WEB", query={"confirm": "WEB"}), 204)
        self.make_project("WEB", members=("bob", "carol"))
