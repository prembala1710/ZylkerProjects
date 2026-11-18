"""Sprints: planning against capacity, the lifecycle, scope change, burndown, velocity and the forecast."""

import json
import os
import sqlite3
from datetime import timedelta

from helpers import FIXED_NOW, ApiTestCase

from zylker_pm import db
from zylker_pm.db import init_db
from zylker_pm.services import sprints

# FIXED_NOW is Friday 2026-10-16. The default sprint is the two working weeks around it.
START, END = "2026-10-12", "2026-10-23"


class SprintBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))

    def sprint(self, name="S1", start=START, end=END, project="WEB", who="alice", status=201, **extra):
        r = self.client(who).post("/api/projects/%s/sprints" % project, {"name": name, "start_date": start, "end_date": end, **extra})
        self.assertEqual(r.status, status, r.text)
        return r.json

    def task(self, title="A task", hours=None, project="WEB", **extra):
        if hours is not None:
            extra["estimate_hours"] = hours
        return self.make_task(project, title, **extra)["key"]

    def plan(self, sprint_id, *keys, who="alice", status=200):
        r = self.client(who).post("/api/sprints/%d/tasks" % sprint_id, {"tasks": list(keys)})
        self.assertEqual(r.status, status, r.text)
        return r.json

    def start(self, sprint_id, who="alice", status=200):
        r = self.client(who).post("/api/sprints/%d/start" % sprint_id)
        self.assertEqual(r.status, status, r.text)
        return r.json

    def close(self, sprint_id, who="alice", status=200, **body):
        r = self.client(who).post("/api/sprints/%d/close" % sprint_id, body)
        self.assertEqual(r.status, status, r.text)
        return r.json

    def running(self, *hours, **kw):
        """An active sprint holding one task per entry of ``hours``; returns (sprint_id, [task keys])."""
        s = self.sprint(**kw)
        keys = [self.task("Task %d" % i, h) for i, h in enumerate(hours)]
        self.plan(s["id"], *keys)
        self.start(s["id"])
        return s["id"], keys

    def get(self, sprint_id, who="alice"):
        return self.ok(self.client(who).get("/api/sprints/%d" % sprint_id))

    def audit(self, **query):
        return self.ok(self.admin.get("/api/audit", query=query))["items"]

    def person(self, plan, username):
        return next(p for p in plan["people"] if p["user"]["username"] == username)


class CreationTests(SprintBase):
    def test_a_manager_creates_a_planned_sprint(self):
        s = self.sprint(goal="Ship the login")
        self.assertEqual((s["name"], s["goal"], s["status"], s["project"]), ("S1", "Ship the login", "planned", "WEB"))
        self.assertEqual((s["start_date"], s["end_date"], s["working_days"]), (START, END, 10))
        self.assertEqual((s["committed"], s["days_left"], s["health"], s["result"]), (None, None, None, None))
        self.assertEqual(s["scope"]["tasks"], 0)
        self.assertEqual(s["items"], [])

    def test_only_managers_create_sprints(self):
        for who in ("bob", "vera"):
            r = self.client(who).post("/api/projects/WEB/sprints", {"name": "x", "start_date": START, "end_date": END})
            self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), who)
        self.sprint("by-admin", who="admin")
        self.assertEqual(self.client().post("/api/projects/WEB/sprints", {}).status, 401)

    def test_validation(self):
        base = {"name": "x", "start_date": START, "end_date": END}
        for bad, field in (
            ({**base, "name": ""}, "name"), ({**base, "name": "n" * 61}, "name"), ({**base, "start_date": "12/10/2026"}, "start_date"),
            ({**base, "end_date": "2026-02-30"}, "end_date"), ({**base, "end_date": None}, "end_date"), ({**base, "goal": "g" * 501}, "goal"),
            ({**base, "colour": "red"}, "colour"), ({"name": "x"}, "start_date"),
        ):
            r = self.client("alice").post("/api/projects/WEB/sprints", bad)
            self.assertEqual(r.status, 422, bad)
            self.assertIn(field, r.json["error"]["details"], bad)
        r = self.client("alice").post("/api/projects/WEB/sprints", {**base, "start_date": END, "end_date": START})
        self.assertEqual(r.json["error"]["details"], {"end_date": "must not be before start_date"})
        r = self.client("alice").post("/api/projects/WEB/sprints", {**base, "start_date": "2026-01-01", "end_date": "2026-03-02"})
        self.assertEqual(r.json["error"]["details"], {"end_date": "a sprint can run at most 60 days"})
        self.assertEqual(self.sprint("max", start="2026-01-01", end="2026-03-01")["working_days"], 42)
        self.assertEqual(self.sprint("one-day", start="2026-04-01", end="2026-04-01")["working_days"], 1)

    def test_names_are_unique_per_project_ignoring_case(self):
        self.sprint("Alpha")
        r = self.client("alice").post("/api/projects/WEB/sprints", {"name": "alpha", "start_date": "2026-11-02", "end_date": "2026-11-13"})
        self.assertEqual((r.status, r.error_code), (409, "sprint_name_taken"))
        self.make_project("OPS", members=("bob",))
        self.assertEqual(self.sprint("Alpha", project="OPS")["project"], "OPS")

    def test_open_sprints_of_a_project_cannot_overlap(self):
        self.sprint("S1")
        for start, end in (("2026-10-20", "2026-10-30"), ("2026-10-01", "2026-10-12"), ("2026-10-14", "2026-10-15"), ("2026-10-01", "2026-11-15")):
            r = self.client("alice").post("/api/projects/WEB/sprints", {"name": "x", "start_date": start, "end_date": end})
            self.assertEqual((r.status, r.error_code), (409, "sprint_overlap"), (start, end))
            self.assertEqual(r.json["error"]["details"], {"sprint": "S1"})
        self.assertEqual(self.sprint("next", start="2026-10-24", end="2026-11-06")["status"], "planned")  # back to back is fine
        self.make_project("OPS", members=("bob",))
        self.assertEqual(self.sprint("S1", project="OPS")["status"], "planned")  # another project does not matter

    def test_a_closed_sprint_does_not_block_its_dates(self):
        s, _ = self.running(5)
        self.close(s)
        self.assertEqual(self.sprint("again")["status"], "planned")

    def test_archived_and_missing_projects(self):
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "archived"}))
        r = self.client("alice").post("/api/projects/WEB/sprints", {"name": "x", "start_date": START, "end_date": END})
        self.assertEqual((r.status, r.error_code), (409, "project_archived"))
        r = self.client("alice").post("/api/projects/NOPE/sprints", {"name": "x", "start_date": START, "end_date": END})
        self.assertEqual((r.status, r.error_code), (404, "project_not_found"))

    def test_listing_and_filtering(self):
        a = self.sprint("A", start="2026-09-01", end="2026-09-14")
        b = self.sprint("B")
        self.assertEqual([s["name"] for s in self.ok(self.client("vera").get("/api/projects/WEB/sprints"))["items"]], ["B", "A"])  # newest first
        self.assertEqual(self.ok(self.client("vera").get("/api/projects/WEB/sprints"))["items"][0]["id"], b["id"])
        self.assertEqual(self.ok(self.client("vera").get("/api/projects/WEB/sprints", query={"status": "active"}))["items"], [])
        self.assertEqual([s["id"] for s in self.ok(self.client("vera").get("/api/projects/WEB/sprints", query={"status": "planned,closed"}))["items"]], [b["id"], a["id"]])
        self.assertEqual(self.client("vera").get("/api/projects/WEB/sprints", query={"status": "bogus"}).status, 422)

    def test_bad_ids(self):
        for path in ("/api/sprints/abc", "/api/sprints/999", "/api/sprints/0", "/api/sprints/999/burndown"):
            r = self.client("alice").get(path)
            self.assertEqual((r.status, r.error_code), (404, "sprint_not_found"), path)
        self.assertEqual(self.client("alice").post("/api/sprints/999/start").status, 404)
        self.assertEqual(self.client("alice").delete("/api/sprints/999").status, 404)

    def test_editing_a_planned_sprint(self):
        s = self.sprint()
        self.sprint("other", start="2026-11-02", end="2026-11-13")
        r = self.ok(self.client("alice").patch("/api/sprints/%d" % s["id"], {"name": "Renamed", "goal": "New goal", "start_date": "2026-10-13", "end_date": "2026-10-22"}))
        self.assertEqual((r["name"], r["goal"], r["start_date"], r["end_date"], r["working_days"]), ("Renamed", "New goal", "2026-10-13", "2026-10-22", 8))
        self.assertEqual(self.client("alice").patch("/api/sprints/%d" % s["id"], {"name": "OTHER"}).error_code, "sprint_name_taken")
        self.assertEqual(self.client("alice").patch("/api/sprints/%d" % s["id"], {"end_date": "2026-11-05"}).error_code, "sprint_overlap")
        self.assertEqual(self.client("alice").patch("/api/sprints/%d" % s["id"], {"end_date": "2026-10-01"}).status, 422)
        self.assertEqual(self.client("alice").patch("/api/sprints/%d" % s["id"], {"end_date": "2026-12-31"}).status, 422)
        self.assertEqual(self.client("bob").patch("/api/sprints/%d" % s["id"], {"goal": "x"}).status, 403)
        same = self.ok(self.client("alice").patch("/api/sprints/%d" % s["id"], {"goal": "New goal"}))
        self.assertEqual(same["goal"], "New goal")
        self.assertEqual(len(self.audit(action="sprint.updated")), 1)  # a no-op is not an event

    def test_what_may_change_once_it_runs(self):
        s, _ = self.running(5)
        r = self.client("alice").patch("/api/sprints/%d" % s, {"start_date": "2026-10-13"})
        self.assertEqual((r.status, r.json["error"]["details"]), (422, {"start_date": "unknown field"}))
        self.assertEqual(self.ok(self.client("alice").patch("/api/sprints/%d" % s, {"end_date": "2026-10-30", "goal": "Stretch"}))["end_date"], "2026-10-30")
        self.assertEqual(self.client("alice").patch("/api/sprints/%d" % s, {"end_date": "2026-10-15"}).status, 422)  # not before today
        self.close(s)
        r = self.client("alice").patch("/api/sprints/%d" % s, {"goal": "x"})
        self.assertEqual((r.status, r.error_code), (409, "sprint_closed"))

    def test_only_a_planned_sprint_can_be_deleted(self):
        a = self.sprint("A")
        key = self.task("T", 3)
        self.plan(a["id"], key)
        self.assertEqual(self.client("alice").delete("/api/sprints/%d" % a["id"]).status, 204)
        self.assertEqual(self.client("alice").get("/api/sprints/%d" % a["id"]).status, 404)
        self.assertEqual(self.ok(self.client("alice").get("/api/tasks/" + key))["title"], "T")  # the task is untouched
        self.assertEqual(self.ok(self.client("alice").get("/api/projects/WEB/backlog"))["total"], 1)  # and free again
        gone = self.audit(action="sprint.deleted")[0]
        self.assertEqual((gone["severity"], gone["details"]["released_tasks"]), ("notice", 1))
        running, _ = self.running(5, name="R")
        r = self.client("alice").delete("/api/sprints/%d" % running)
        self.assertEqual((r.status, r.error_code), (409, "sprint_not_deletable"))
        self.close(running)
        self.assertEqual(self.client("alice").delete("/api/sprints/%d" % running).error_code, "sprint_not_deletable")
        self.assertEqual(self.client("bob").delete("/api/sprints/%d" % running).status, 403)

    def test_events_are_audited(self):
        s = self.sprint()
        self.assertEqual([e["action"] for e in self.audit(category="sprint")], ["sprint.created"])
        self.assertEqual(self.ok(self.client("vera").get("/api/meta"))["audit_categories"]["sprint"], "Sprints")
        self.assertEqual(self.audit(action="sprint.created")[0]["project"], "WEB")
        self.assertEqual(s["id"], self.audit(action="sprint.created")[0]["details"]["id"])


class PlanningTests(SprintBase):
    def test_tasks_join_with_their_estimate_as_it_is_now(self):
        s = self.sprint()
        a, b = self.task("A", 8), self.task("B")
        out = self.plan(s["id"], a, b)
        self.assertEqual((out["scope"]["tasks"], out["scope"]["hours"], out["unestimated_tasks"]), (2, 8, 1))
        by_key = {i["task"]: i for i in out["items"]}
        self.assertEqual((by_key[a]["hours"], by_key[a]["estimated"], by_key[b]["hours"], by_key[b]["estimated"]), (8, True, 0, False))
        self.ok(self.client("alice").patch("/api/tasks/" + a, {"estimate_hours": 20}))
        self.assertEqual(self.get(s["id"])["scope"]["hours"], 8)  # the sprint keeps what was planned

    def test_who_may_plan(self):
        s = self.sprint()
        key = self.task("A", 2)
        self.plan(s["id"], key, who="bob")  # a project member
        self.assertEqual(self.get(s["id"])["scope"]["tasks"], 1)
        other = self.task("B", 2)
        r = self.client("vera").post("/api/sprints/%d/tasks" % s["id"], {"tasks": [other]})
        self.assertEqual((r.status, r.error_code), (403, "insufficient_role"))
        self.make_user("zed", "member")
        r = self.client("zed").post("/api/sprints/%d/tasks" % s["id"], {"tasks": [other]})
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        self.assertEqual(self.client("zed").get("/api/sprints/%d" % s["id"]).status, 200)  # reading is open

    def test_a_request_is_all_or_nothing(self):
        s = self.sprint()
        good, closed = self.task("Good", 2), self.task("Closed", 2)
        self.set_status(closed, "done")
        r = self.client("alice").post("/api/sprints/%d/tasks" % s["id"], {"tasks": [good, closed]})
        self.assertEqual((r.status, r.error_code), (409, "task_closed"))
        self.assertEqual(r.json["error"]["details"], {"task": closed})
        self.assertEqual(self.get(s["id"])["scope"]["tasks"], 0)  # the good one was not kept either

    def test_refusals(self):
        a, b = self.sprint("A"), self.sprint("B", start="2026-10-26", end="2026-11-06")
        key = self.task("T", 2)
        self.plan(a["id"], key)
        r = self.client("alice").post("/api/sprints/%d/tasks" % b["id"], {"tasks": [key]})
        self.assertEqual((r.status, r.error_code, r.json["error"]["details"]), (409, "task_in_sprint", {"task": key, "sprint": "A"}))
        self.make_project("OPS", members=("bob",))
        foreign = self.make_task("OPS", "Elsewhere")["key"]
        self.assertEqual(self.client("alice").post("/api/sprints/%d/tasks" % a["id"], {"tasks": [foreign]}).error_code, "task_other_project")
        self.assertEqual(self.client("alice").post("/api/sprints/%d/tasks" % a["id"], {"tasks": ["WEB-999"]}).error_code, "task_not_found")
        for bad in ({}, {"tasks": []}, {"tasks": "WEB-1"}, {"tasks": [1]}, {"tasks": ["WEB-%d" % i for i in range(101)]}, {"tasks": [None]}):
            r = self.client("alice").post("/api/sprints/%d/tasks" % a["id"], bad)
            self.assertEqual(r.status, 422, bad)
        self.assertEqual(self.client("alice").post("/api/sprints/%d/tasks" % a["id"], {"tasks": ["nonsense"]}).status, 400)
        self.assertEqual(self.client("alice").post("/api/sprints/%d/tasks" % a["id"], {"tasks": [key], "extra": 1}).status, 422)

    def test_listing_a_task_twice_in_one_request_adds_it_once(self):
        s = self.sprint()
        key = self.task("T", 4)
        self.assertEqual(self.plan(s["id"], key, key, key.lower())["scope"], {"hours": 4, "tasks": 1, "added_hours": 0, "added_tasks": 0, "removed_tasks": 0})

    def test_a_parent_and_its_subtasks_are_never_both_planned(self):
        s = self.sprint()
        parent = self.task("Epic", 10, type="epic")
        child = self.task("Story", 4, parent=parent)
        grandchild = self.task("Sub", 2, parent=child)
        self.plan(s["id"], child)
        for key, other in ((parent, child), (grandchild, child)):
            r = self.client("alice").post("/api/sprints/%d/tasks" % s["id"], {"tasks": [key]})
            self.assertEqual((r.status, r.error_code, r.json["error"]["details"]), (409, "overlapping_scope", {"task": key, "related": other}))
        sibling = self.task("Sibling", 3, parent=parent)
        self.assertEqual(self.plan(s["id"], sibling)["scope"]["tasks"], 2)  # siblings are independent work
        t = self.sprint("later", start="2026-11-02", end="2026-11-13")
        self.assertEqual(self.plan(t["id"], parent)["scope"]["tasks"], 1)  # another sprint may hold the parent

    def test_removing_a_task(self):
        s = self.sprint()
        a, b = self.task("A", 3), self.task("B", 4)
        self.plan(s["id"], a, b)
        out = self.ok(self.client("alice").delete("/api/sprints/%d/tasks/%s" % (s["id"], a)))
        self.assertEqual((out["scope"]["tasks"], out["scope"]["hours"], out["scope"]["removed_tasks"]), (1, 4, 1))
        self.assertEqual([i["outcome"] for i in out["items"]], ["removed", None])
        r = self.client("alice").delete("/api/sprints/%d/tasks/%s" % (s["id"], a))
        self.assertEqual((r.status, r.error_code), (404, "task_not_in_sprint"))
        self.assertEqual(self.plan(s["id"], a)["scope"]["tasks"], 2)  # it can come back
        self.assertEqual(self.client("vera").delete("/api/sprints/%d/tasks/%s" % (s["id"], b)).status, 403)
        self.assertEqual(self.client("alice").delete("/api/sprints/%d/tasks/WEB-999" % s["id"]).error_code, "task_not_found")

    def test_task_activity_shows_the_planning(self):
        s = self.sprint()
        key = self.task("T", 2)
        self.plan(s["id"], key)
        self.client("alice").delete("/api/sprints/%d/tasks/%s" % (s["id"], key))
        actions = [a["action"] for a in self.ok(self.client("alice").get("/api/tasks/%s/activity" % key))["items"]]
        self.assertIn("sprint.task_added", actions)
        self.assertIn("sprint.task_removed", actions)

    def test_capacity_against_planned_work(self):
        s = self.sprint()
        self.make_user("dan", "member", weekly_capacity_hours=20)
        self.ok(self.client("alice").post("/api/projects/WEB/members", {"username": "dan"}))
        self.plan(s["id"], self.task("B1", 50, assignee="bob"), self.task("B2", 40, assignee="bob"), self.task("D1", 10, assignee="dan"), self.task("U", 6), self.task("N", assignee="carol"))
        plan = self.get(s["id"])["plan"]
        self.assertEqual((plan["working_days"], plan["capacity_hours"], plan["planned_hours"], plan["unassigned_hours"], plan["unestimated_tasks"]), (10, 80 * 3 + 40, 106, 6, 1))  # alice (the owner), bob, carol and dan
        bob, dan, carol = self.person(plan, "bob"), self.person(plan, "dan"), self.person(plan, "carol")
        self.assertEqual((bob["capacity_hours"], bob["planned_hours"], bob["utilisation"], bob["overallocated"]), (80, 90, 1.12, True))
        self.assertEqual((dan["capacity_hours"], dan["planned_hours"], dan["utilisation"], dan["overallocated"]), (40, 10, 0.25, False))
        self.assertEqual((carol["planned_hours"], carol["overallocated"]), (0, False))
        self.assertEqual(plan["utilisation"], 0.38)
        self.assertEqual(plan["people"][0]["user"]["username"], "bob")  # heaviest first

    def test_work_for_someone_without_capacity_is_overallocated(self):
        s = self.sprint()
        self.make_user("zero", "member", weekly_capacity_hours=0)
        self.ok(self.client("alice").post("/api/projects/WEB/members", {"username": "zero"}))
        self.plan(s["id"], self.task("T", 1, assignee="zero"))
        zero = self.person(self.get(s["id"])["plan"], "zero")
        self.assertEqual((zero["capacity_hours"], zero["utilisation"], zero["overallocated"]), (0, None, True))

    def test_an_assignee_who_left_the_project_still_appears(self):
        s = self.sprint()
        key = self.task("T", 6, assignee="carol")
        self.plan(s["id"], key)
        self.ok(self.client("alice").patch("/api/tasks/" + key, {"assignee": None}))
        self.ok(self.client("alice").patch("/api/tasks/" + key, {"assignee": "bob"}))
        plan = self.get(s["id"])["plan"]
        self.assertEqual((self.person(plan, "bob")["planned_hours"], self.person(plan, "carol")["planned_hours"]), (6, 0))

    def test_the_backlog(self):
        s = self.sprint()
        epic = self.task("Epic", 99, type="epic")
        self.task("Child", 5, parent=epic, priority="low")
        planned = self.task("Planned", 3)
        done = self.task("Done", 2)
        self.set_status(done, "done")
        crit = self.task("Critical", 7, priority="critical")
        self.task("Unsized")
        self.plan(s["id"], planned)
        b = self.ok(self.client("vera").get("/api/projects/WEB/backlog"))
        self.assertEqual([i["title"] for i in b["items"]], ["Critical", "Unsized", "Child"])  # parents, planned and closed tasks are not backlog
        self.assertEqual((b["total"], b["hours"], b["unestimated_tasks"]), (3, 12, 1))
        self.assertEqual(b["items"][0]["task"], crit)
        self.assertEqual(self.client("vera").get("/api/projects/NOPE/backlog").status, 404)


class LifecycleTests(SprintBase):
    def test_starting_records_the_commitment(self):
        s = self.sprint()
        self.plan(s["id"], self.task("A", 8), self.task("B", 4), self.task("C"))
        out = self.start(s["id"])
        self.assertEqual((out["status"], out["committed"], out["days_left"]), ("active", {"hours": 12, "tasks": 3}, 7))
        self.assertEqual(out["started_at"], "2026-10-16T09:00:00Z")
        self.assertEqual(out["health"], {"status": "on_track", "reasons": []})
        self.assertEqual(self.get(s["id"])["plan"]["working_days"], 6)  # capacity counts from today, not from the first day
        event = self.audit(action="sprint.started")[0]
        self.assertEqual((event["details"]["committed_hours"], event["details"]["committed_tasks"]), (12, 3))

    def test_who_may_start_and_when(self):
        s = self.sprint()
        self.plan(s["id"], self.task("A", 1))
        self.assertEqual(self.client("bob").post("/api/sprints/%d/start" % s["id"]).status, 403)
        future = self.sprint("future", start="2026-10-26", end="2026-11-06")
        self.plan(future["id"], self.task("F", 1))
        r = self.client("alice").post("/api/sprints/%d/start" % future["id"])
        self.assertEqual((r.status, r.error_code), (409, "sprint_not_due"))
        past = self.sprint("past", start="2026-09-01", end="2026-09-14")
        self.plan(past["id"], self.task("P", 1))
        self.assertEqual(self.client("alice").post("/api/sprints/%d/start" % past["id"]).error_code, "sprint_expired")
        self.start(s["id"])
        self.assertEqual(self.client("alice").post("/api/sprints/%d/start" % s["id"]).error_code, "sprint_not_planned")

    def test_an_empty_sprint_cannot_start(self):
        s = self.sprint()
        self.assertEqual(self.client("alice").post("/api/sprints/%d/start" % s["id"]).error_code, "empty_sprint")

    def test_tasks_finished_during_planning_do_not_count_towards_the_commitment(self):
        s = self.sprint()
        a, b = self.task("A", 5), self.task("B", 7)
        self.plan(s["id"], a, b)
        self.set_status(b, "done")
        out = self.start(s["id"])
        self.assertEqual(out["committed"], {"hours": 5, "tasks": 1})
        self.assertEqual(out["scope"]["removed_tasks"], 1)
        only = self.sprint("only-closed", start="2026-10-26", end="2026-11-06")
        c = self.task("C", 1)
        self.plan(only["id"], c)
        self.set_status(c, "done")
        self.close(s["id"])
        self.assertEqual(self.client("alice").post("/api/sprints/%d/start" % only["id"]).status, 409)

    def test_one_active_sprint_per_project(self):
        s, _ = self.running(5)
        n = self.sprint("next", start="2026-10-26", end="2026-11-06")
        self.plan(n["id"], self.task("N", 2))
        r = self.client("alice").post("/api/sprints/%d/start" % n["id"])
        self.assertEqual((r.status, r.error_code), (409, "sprint_already_active"))
        self.make_project("OPS", members=("bob",))
        o = self.sprint("S1", project="OPS")
        self.plan(o["id"], self.task("O", 1, project="OPS"))
        self.start(o["id"])  # another project has its own
        self.assertEqual(self.get(s)["status"], "active")

    def test_closing_sorts_the_outcomes(self):
        s, (a, b, c, d) = self.running(8, 4, 6, 2)
        self.set_status(a, "done")
        self.set_status(b, "in_progress")
        self.set_status(d, "cancelled")
        out = self.close(s)
        self.assertEqual(out["status"], "closed")
        self.assertEqual(out["result"], {"completed_hours": 8, "completed_tasks": 1, "carried_tasks": 2, "dropped_tasks": 1, "completion_rate": 0.4})
        self.assertEqual({i["task"]: i["outcome"] for i in out["items"]}, {a: "done", b: "carried", c: "carried", d: "dropped"})
        self.assertIsNone(out["plan"])
        self.assertEqual(out["closed_at"], "2026-10-16T09:00:00Z")
        closed = self.audit(action="sprint.closed")[0]
        self.assertEqual((closed["severity"], closed["details"]["carried_tasks"], closed["details"]["carried_into"]), ("notice", 2, None))
        backlog = self.ok(self.client("alice").get("/api/projects/WEB/backlog"))
        self.assertEqual(sorted(i["task"] for i in backlog["items"]), sorted([b, c]))  # unfinished work goes back to the backlog

    def test_carrying_unfinished_work_into_the_next_sprint(self):
        s, (a, b, c) = self.running(8, 4, 6)
        nxt = self.sprint("next", start="2026-10-26", end="2026-11-06")
        later = self.sprint("later", start="2026-11-09", end="2026-11-20")
        self.set_status(a, "done")
        self.ok(self.client("alice").patch("/api/tasks/" + b, {"estimate_hours": 9}))
        out = self.close(s, carry_over="next")
        self.assertEqual(out["result"]["carried_tasks"], 2)
        into = self.get(nxt["id"])
        self.assertEqual({i["task"]: (i["hours"], i["added_after_start"]) for i in into["items"]}, {b: (9, False), c: (6, False)})  # re-estimated when carried
        self.assertEqual(self.get(later["id"])["scope"]["tasks"], 0)
        self.assertEqual(self.ok(self.client("alice").get("/api/projects/WEB/backlog"))["total"], 0)
        self.assertEqual(self.audit(action="sprint.closed")[0]["details"]["carried_into"], "next")

    def test_carrying_into_a_chosen_sprint(self):
        s, (a,) = self.running(8)
        later = self.sprint("later", start="2026-11-09", end="2026-11-20")
        self.sprint("nearer", start="2026-10-26", end="2026-11-06")
        self.close(s, carry_over=later["id"])
        self.assertEqual([i["task"] for i in self.get(later["id"])["items"]], [a])

    def test_close_refusals(self):
        s, _ = self.running(8)
        for bad in ("nowhere", 12345, True, 1.5, None, ["x"]):
            r = self.client("alice").post("/api/sprints/%d/close" % s, {"carry_over": bad})
            self.assertIn(r.status, (422,), bad)
        self.assertEqual(self.client("alice").post("/api/sprints/%d/close" % s, {"carry_over": "next"}).error_code, "no_next_sprint")
        self.assertEqual(self.client("alice").post("/api/sprints/%d/close" % s, {"other": 1}).status, 422)
        self.assertEqual(self.client("bob").post("/api/sprints/%d/close" % s, {}).status, 403)
        other = self.sprint("later", start="2026-11-09", end="2026-11-20")
        self.make_project("OPS", members=("bob",))
        foreign = self.sprint("F", project="OPS")
        self.assertEqual(self.client("alice").post("/api/sprints/%d/close" % s, {"carry_over": foreign["id"]}).status, 422)
        self.assertEqual(self.get(s)["status"], "active")  # nothing happened
        self.close(s)
        r = self.client("alice").post("/api/sprints/%d/close" % s, {})
        self.assertEqual((r.status, r.error_code), (409, "sprint_not_active"))
        done = self.client("alice").post("/api/sprints/%d/close" % other["id"], {})
        self.assertEqual(done.error_code, "sprint_not_active")

    def test_carry_over_must_target_a_planned_sprint(self):
        first, _ = self.running(3, name="first", start="2026-10-14", end="2026-10-16")
        self.close(first)
        self.now = FIXED_NOW + timedelta(days=3)  # Monday the 19th
        s, _ = self.running(8, name="second", start="2026-10-19", end="2026-10-30")
        for target in (first, s):  # a closed sprint, and the sprint being closed itself
            r = self.client("alice").post("/api/sprints/%d/close" % s, {"carry_over": target})
            self.assertEqual((r.status, r.error_code), (409, "target_not_planned"), target)
        self.assertEqual(self.get(s)["status"], "active")

    def test_a_closed_sprint_is_final(self):
        s, (a,) = self.running(8)
        self.close(s)
        r = self.client("alice").post("/api/sprints/%d/tasks" % s, {"tasks": [self.task("N", 1)]})
        self.assertEqual((r.status, r.error_code), (409, "sprint_closed"))
        self.assertEqual(self.client("alice").delete("/api/sprints/%d/tasks/%s" % (s, a)).error_code, "sprint_closed")

    def test_tasks_can_be_planned_again_after_their_sprint_closes(self):
        s, (a,) = self.running(8)
        self.close(s)
        n = self.sprint("next", start="2026-10-26", end="2026-11-06")
        self.assertEqual(self.plan(n["id"], a)["scope"]["tasks"], 1)

    def test_work_added_after_the_start_is_scope_creep(self):
        s, _ = self.running(10)
        extra = self.task("Late", 4)
        out = self.plan(s, extra)
        self.assertEqual((out["scope"]["added_hours"], out["scope"]["added_tasks"], out["committed"]["hours"]), (4, 1, 10))
        self.assertEqual(out["health"]["status"], "at_risk")  # 40% more than was committed
        self.assertIn("Scope grew by 4 h after the start (40% of the commitment)", out["health"]["reasons"])
        self.assertTrue(self.get(s)["items"][-1]["added_after_start"])

    def test_archived_projects_are_read_only(self):
        s = self.sprint()
        key = self.task("T", 2)
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "archived"}))
        self.assertEqual(self.client("alice").post("/api/sprints/%d/tasks" % s["id"], {"tasks": [key]}).error_code, "project_archived")
        self.assertEqual(self.client("alice").delete("/api/sprints/%d" % s["id"]).error_code, "project_archived")
        self.assertEqual(self.client("alice").get("/api/sprints/%d" % s["id"]).status, 200)

    def test_deleting_a_task_takes_it_out_of_its_sprint(self):
        s = self.sprint()
        a, b = self.task("A", 3), self.task("B", 4)
        self.plan(s["id"], a, b)
        self.assertEqual(self.client("alice").delete("/api/tasks/" + a).status, 204)
        self.assertEqual((self.get(s["id"])["scope"]["tasks"], self.get(s["id"])["scope"]["hours"]), (1, 4))


class BurndownTests(SprintBase):
    def test_not_before_the_sprint_starts(self):
        s = self.sprint()
        r = self.client("alice").get("/api/sprints/%d/burndown" % s["id"])
        self.assertEqual((r.status, r.error_code), (409, "sprint_not_started"))

    def test_the_ideal_line_follows_working_days(self):
        self.now = FIXED_NOW - timedelta(days=4)  # started on its first day, Monday the 12th
        s, _ = self.running(20, 20)
        days = self.ok(self.client("vera").get("/api/sprints/%d/burndown" % s))["days"]
        self.assertEqual(len(days), 12)
        ideal = {d["date"]: d["ideal"] for d in days}
        self.assertEqual(ideal["2026-10-12"], 36)  # 9 working days still to come of 10
        self.assertEqual(ideal["2026-10-16"], 20)
        self.assertEqual((ideal["2026-10-17"], ideal["2026-10-18"]), (20, 20))  # the weekend does not move it
        self.assertEqual((ideal["2026-10-22"], ideal["2026-10-23"]), (4, 0))
        self.assertEqual([d["working_day"] for d in days][:7], [True] * 5 + [False] * 2)

    def test_actual_days_stop_at_today(self):
        s, _ = self.running(10, 6)
        body = self.ok(self.client("alice").get("/api/sprints/%d/burndown" % s))
        self.assertEqual(body["baseline_hours"], 16)
        by = {d["date"]: d for d in body["days"]}
        self.assertEqual((by["2026-10-12"]["remaining"], by["2026-10-16"]["remaining"], by["2026-10-16"]["scope"]), (16, 16, 16))
        self.assertIsNone(by["2026-10-17"]["remaining"])

    def test_finishing_work_moves_the_line_on_the_day_it_happens(self):
        s, (a, b) = self.running(10, 6)
        self.set_status(a, "done")
        self.now = FIXED_NOW + timedelta(days=3)  # Monday 19th
        self.set_status(b, "in_progress")
        self.now = FIXED_NOW + timedelta(days=4)
        self.set_status(b, "done")
        by = {d["date"]: d for d in self.ok(self.client("alice").get("/api/sprints/%d/burndown" % s))["days"]}
        self.assertEqual((by["2026-10-16"]["remaining"], by["2026-10-16"]["completed"]), (6, 10))
        self.assertEqual((by["2026-10-19"]["remaining"], by["2026-10-19"]["completed"]), (6, 10))
        self.assertEqual((by["2026-10-20"]["remaining"], by["2026-10-20"]["completed"]), (0, 16))

    def test_cancelled_work_leaves_the_remaining_line_but_is_not_completed(self):
        s, (a, b) = self.running(10, 6)
        self.set_status(b, "cancelled")
        today = {d["date"]: d for d in self.ok(self.client("alice").get("/api/sprints/%d/burndown" % s))["days"]}["2026-10-16"]
        self.assertEqual((today["remaining"], today["completed"], today["scope"]), (10, 0, 16))

    def test_scope_added_and_removed_shows_on_its_day(self):
        s, (a, b) = self.running(10, 6)
        self.now = FIXED_NOW + timedelta(days=3)
        late = self.task("Late", 5)
        self.plan(s, late)
        self.now = FIXED_NOW + timedelta(days=4)
        self.client("alice").delete("/api/sprints/%d/tasks/%s" % (s, b))
        by = {d["date"]: d for d in self.ok(self.client("alice").get("/api/sprints/%d/burndown" % s))["days"]}
        self.assertEqual((by["2026-10-16"]["scope"], by["2026-10-19"]["scope"], by["2026-10-19"]["remaining"]), (16, 21, 21))
        self.assertEqual((by["2026-10-20"]["scope"], by["2026-10-20"]["remaining"]), (15, 15))

    def test_tasks_planned_before_the_start_count_from_the_first_day(self):
        s = self.sprint()
        self.plan(s["id"], self.task("A", 8))
        self.start(s["id"])
        first = self.ok(self.client("alice").get("/api/sprints/%d/burndown" % s["id"]))["days"][0]
        self.assertEqual((first["date"], first["scope"], first["remaining"]), (START, 8, 8))

    def test_a_closed_sprint_keeps_its_picture_even_if_a_task_is_reopened(self):
        s, (a, b) = self.running(10, 6)
        self.set_status(a, "done")
        self.now = FIXED_NOW + timedelta(days=3)
        self.close(s)
        self.set_status(a, "todo")  # reopened after the sprint
        body = self.ok(self.client("alice").get("/api/sprints/%d/burndown" % s))
        by = {d["date"]: d for d in body["days"]}
        self.assertEqual((by["2026-10-16"]["completed"], by["2026-10-19"]["completed"], by["2026-10-19"]["remaining"]), (10, 10, 6))
        self.assertIsNone(by["2026-10-20"]["remaining"])  # nothing after the day it closed

    def test_unestimated_tasks_are_reported(self):
        s, _ = self.running(None, 4)
        self.assertEqual(self.ok(self.client("alice").get("/api/sprints/%d/burndown" % s))["unestimated_tasks"], 1)


class HealthTests(SprintBase):
    def test_a_sprint_started_late_is_judged_from_the_day_it_started(self):
        s, _ = self.running(20, 20)  # started on Friday the 16th, with 6 working days left
        self.assertEqual(self.get(s)["health"]["status"], "on_track")

    def test_behind_the_ideal_line_is_at_risk(self):
        s, (a, b) = self.running(20, 20)
        self.now = FIXED_NOW + timedelta(days=4)  # Tuesday the 20th: ideal 20 h, nothing done
        health = self.get(s)["health"]
        self.assertEqual(health["status"], "at_risk")
        self.assertEqual(health["reasons"], ["40 h remaining against an ideal of 20 h"])
        self.set_status(a, "done")
        self.assertEqual(self.get(s)["health"]["status"], "on_track")  # 20 h left is on the line

    def test_overdue_is_off_track(self):
        s, _ = self.running(5)
        self.now = FIXED_NOW + timedelta(days=10)  # Monday 26th, after the end
        health = self.get(s)["health"]
        self.assertEqual(health["status"], "off_track")
        self.assertIn("still open", health["reasons"][0])

    def test_finishing_in_time_is_on_track(self):
        s, (a,) = self.running(5)
        self.set_status(a, "done")
        self.now = FIXED_NOW + timedelta(days=10)
        self.assertEqual(self.get(s)["health"]["status"], "on_track")
        self.assertEqual(self.get(s)["progress"]["percent"], 100)

    def test_growing_scope_is_at_risk(self):
        s, (a, b) = self.running(10, 10)
        self.set_status(a, "done")
        self.set_status(b, "done")
        self.plan(s, self.task("Late", 6))  # 30% of the 20 h commitment
        health = self.get(s)["health"]
        self.assertEqual(health["status"], "at_risk")
        self.assertIn("Scope grew by 6 h after the start (30% of the commitment)", health["reasons"])

    def test_only_active_sprints_have_health(self):
        s = self.sprint()
        self.assertIsNone(s["health"])
        self.assertIsNone(self.get(s["id"])["health"])


class VelocityTests(SprintBase):
    def finish_sprint(self, name, start, end, done_hours, carried_hours=0, late=0):
        """Run a whole sprint: plan and start it on its first day, finish the work on its last, close it. The clock
        is put back to "today" afterwards."""
        day = lambda text: FIXED_NOW.replace(year=int(text[:4]), month=int(text[5:7]), day=int(text[8:]))  # noqa: E731
        self.now = day(start)
        s = self.sprint(name, start=start, end=end)
        done = [self.task("%s done" % name, done_hours)]
        self.plan(s["id"], *done, *([self.task("%s open" % name, carried_hours)] if carried_hours else []))
        self.start(s["id"])
        if late:
            late_key = self.task("%s late" % name, late)
            self.plan(s["id"], late_key)
            done.append(late_key)
        self.now = day(end)
        for key in done:
            self.set_status(key, "done")
        out = self.close(s["id"])
        self.now = FIXED_NOW
        return out

    def test_with_no_history_there_is_no_forecast(self):
        self.task("Open", 10)
        v = self.ok(self.client("vera").get("/api/projects/WEB/velocity"))
        self.assertEqual((v["sprints"], v["average_velocity"], v["forecast"]), ([], None, None))
        self.assertIn("Close at least one sprint", v["note"])

    def test_history_average_and_forecast(self):
        self.finish_sprint("C", "2026-08-31", "2026-09-13", 10)
        self.finish_sprint("B", "2026-09-14", "2026-09-27", 30, late=10)
        self.finish_sprint("A", "2026-09-28", "2026-10-16", 20, carried_hours=5)
        self.task("Backlog 1", 25)
        self.task("Backlog 2", 15)
        self.task("Backlog 3")
        v = self.ok(self.client("vera").get("/api/projects/WEB/velocity"))
        self.assertEqual([h["name"] for h in v["sprints"]], ["A", "B", "C"])  # newest first
        a, b = v["sprints"][0], v["sprints"][1]
        self.assertEqual((a["committed_hours"], a["completed_hours"], a["carried_tasks"], a["completion_rate"], a["days"]), (25, 20, 1, 0.8, 19))
        self.assertEqual((b["committed_hours"], b["added_hours"], b["completed_hours"], b["completion_rate"]), (30, 10, 40, 1))
        self.assertEqual(v["average_velocity"], 23.33)  # (20 + 40 + 10) / 3
        f = v["forecast"]
        # open work: the 5 h task sprint A carried back, plus three backlog tasks (25 h, 15 h and one without an estimate)
        self.assertEqual((f["backlog_hours"], f["in_sprints_hours"], f["remaining_hours"], f["unestimated_tasks"]), (45, 0, 45, 1))
        self.assertEqual((f["expected"]["sprints"], f["optimistic"]["sprints"], f["pessimistic"]["sprints"]), (2, 2, 5))  # 45 h at 23.3, 40 and 10 h per sprint
        self.assertEqual((f["velocity"], f["sprint_length_days"]), (23.33, 16))  # sprints of 19, 14 and 14 days
        self.assertEqual(f["expected"]["finish_date"], "2026-11-16")  # two 16-day sprints from today
        self.assertEqual(f["pessimistic"]["finish_date"], "2027-01-03")

    def test_work_already_in_sprints_counts_as_remaining(self):
        self.finish_sprint("A", "2026-09-28", "2026-10-15", 20)
        n = self.sprint("next", start="2026-10-26", end="2026-11-06")
        self.plan(n["id"], self.task("Planned", 30))
        self.task("Unplanned", 10)
        f = self.ok(self.client("vera").get("/api/projects/WEB/velocity"))["forecast"]
        self.assertEqual((f["in_sprints_hours"], f["backlog_hours"], f["remaining_hours"]), (30, 10, 40))
        self.assertEqual((f["expected"]["sprints"], f["expected"]["finish_date"]), (2, "2026-11-30"))  # from the planned sprint's first day, in 18-day sprints

    def test_nothing_left_means_zero_more_sprints(self):
        self.finish_sprint("A", "2026-09-28", "2026-10-15", 20)
        f = self.ok(self.client("vera").get("/api/projects/WEB/velocity"))["forecast"]
        self.assertEqual((f["remaining_hours"], f["expected"]["sprints"]), (0, 0))

    def test_a_sprint_that_finished_nothing_gives_no_forecast(self):
        s, _ = self.running(10)
        self.close(s)
        v = self.ok(self.client("vera").get("/api/projects/WEB/velocity"))
        self.assertEqual((v["average_velocity"], v["forecast"]), (0, None))

    def test_the_window_parameter(self):
        self.finish_sprint("A", "2026-09-28", "2026-10-15", 20)
        self.finish_sprint("B", "2026-09-14", "2026-09-27", 30)
        v = self.ok(self.client("vera").get("/api/projects/WEB/velocity", query={"sprints": "1"}))
        self.assertEqual([h["name"] for h in v["sprints"]], ["A"])
        for bad in ("0", "21", "x", "-1", "1.5"):
            self.assertEqual(self.client("vera").get("/api/projects/WEB/velocity", query={"sprints": bad}).status, 422, bad)
        self.assertEqual(self.client("vera").get("/api/projects/NOPE/velocity").status, 404)


class WorkingDayTests(ApiTestCase):
    def test_counting(self):
        from datetime import date

        self.assertEqual(sprints.working_days(date(2026, 10, 12), date(2026, 10, 23)), 10)
        self.assertEqual(sprints.working_days(date(2026, 10, 17), date(2026, 10, 18)), 0)  # a weekend
        self.assertEqual(sprints.working_days(date(2026, 10, 16), date(2026, 10, 16)), 1)
        self.assertEqual(sprints.working_days(date(2026, 10, 23), date(2026, 10, 12)), 0)


class RoutesAndSafetyTests(SprintBase):
    def test_sprints_in_other_projects_are_independent(self):
        self.make_project("OPS", members=("bob",))
        a = self.sprint("S1")
        b = self.sprint("S1", project="OPS")
        key = self.task("T", 2, project="OPS")
        r = self.client("alice").post("/api/sprints/%d/tasks" % a["id"], {"tasks": [key]})
        self.assertEqual(r.error_code, "task_other_project")
        self.assertEqual(self.plan(b["id"], key)["scope"]["tasks"], 1)

    def test_every_write_needs_a_token(self):
        s = self.sprint()
        for method, path in (("post", "/api/sprints/%d/start" % s["id"]), ("post", "/api/sprints/%d/close" % s["id"]), ("delete", "/api/sprints/%d" % s["id"]), ("get", "/api/sprints/%d" % s["id"])):
            self.assertEqual(getattr(self.client(), method)(path).status, 401, path)

    def test_a_read_only_api_token_cannot_change_sprints(self):
        s = self.sprint()
        made = self.ok(self.client("alice").post("/api/tokens", {"name": "ro", "read_only": True}), 201)
        c = self.client(token=made["token"])
        self.assertEqual(c.get("/api/sprints/%d" % s["id"]).status, 200)
        self.assertEqual(c.post("/api/sprints/%d/start" % s["id"]).error_code, "token_read_only")

    def test_hostile_input_never_causes_a_server_error(self):
        s = self.sprint()
        payloads = [None, [], "x", 5, {"name": {"a": 1}}, {"name": "x", "start_date": [1], "end_date": END}, {"name": "x", "start_date": "9999-99-99", "end_date": END},
                    {"name": "\x00" * 10, "start_date": START, "end_date": END}, {"name": "x", "start_date": "0001-01-01", "end_date": "9999-12-31"}]
        for body in payloads:
            r = self.client("alice").request("POST", "/api/projects/WEB/sprints", raw=json.dumps(body).encode(), content_type="application/json")
            self.assertLess(r.status, 500, body)
        for body in payloads + [{"tasks": [{"k": 1}]}, {"tasks": [["WEB-1"]]}]:
            r = self.client("alice").request("POST", "/api/sprints/%d/tasks" % s["id"], raw=json.dumps(body).encode(), content_type="application/json")
            self.assertLess(r.status, 500, body)
            r = self.client("alice").request("PATCH", "/api/sprints/%d" % s["id"], raw=json.dumps(body).encode(), content_type="application/json")
            self.assertLess(r.status, 500, body)
        self.assertLess(self.client("alice").get("/api/projects/WEB/sprints", query={"status": "%" * 300}).status, 500)

    def test_sprint_events_can_be_sent_to_a_webhook(self):
        hook = self.ok(self.admin.post("/api/webhooks", {"name": "s", "url": "https://hooks.example.com/in", "events": ["sprint.*"]}), 201)
        s = self.sprint()
        self.plan(s["id"], self.task("T", 2))
        events = [d["event"] for d in self.ok(self.admin.get("/api/webhooks/%d/deliveries" % hook["id"]))["items"]]
        self.assertEqual(sorted(events), ["sprint.created", "sprint.task_added"])


class UpgradeTests(SprintBase):
    def v7_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        for version in range(1, 7):
            db._migrate(conn, version)
        conn.close()

    def names(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_an_older_database_upgrades_to_the_same_schema_as_a_new_one(self):
        old = os.path.join(self.tmp, "v7.db")
        self.v7_database(old)
        self.assertNotIn("sprints", self.names(old)[0])
        init_db(old)
        fresh = os.path.join(self.tmp, "fresh8.db")
        init_db(fresh)
        self.assertEqual(self.names(old), self.names(fresh))
        self.assertEqual(self.names(old)[1], db.SCHEMA_VERSION)

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v7b.db")
        self.v7_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sprint_items (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        names, version = self.names(path)
        self.assertEqual(version, 7)
        self.assertNotIn("sprints", names)

    def test_the_database_itself_enforces_the_invariants(self):
        s, (a,) = self.running(5)
        conn = db.connect(self.config.db_path)
        project_id = conn.execute("SELECT id FROM projects WHERE key = 'WEB'").fetchone()[0]
        task_id = conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):  # a second active sprint
            conn.execute("INSERT INTO sprints (project_id, name, start_date, end_date, status, created_at) VALUES (?, 'x', '2026-11-01', '2026-11-02', 'active', 'now')", (project_id,))
        with self.assertRaises(sqlite3.IntegrityError):  # the same task live twice
            conn.execute("INSERT INTO sprint_items (sprint_id, task_id, added_at) VALUES (?, ?, 'now')", (s, task_id))
        with self.assertRaises(sqlite3.IntegrityError):  # dates the wrong way round
            conn.execute("INSERT INTO sprints (project_id, name, start_date, end_date, created_at) VALUES (?, 'y', '2026-11-02', '2026-11-01', 'now')", (project_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sprint_items (sprint_id, task_id, added_at, outcome) VALUES (?, ?, 'now', 'bogus')", (s, task_id))
        conn.close()
