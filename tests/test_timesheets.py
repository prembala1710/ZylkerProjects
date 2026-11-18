"""Timesheet approval: submitting a week of time, deciding on it, and what an approved week protects."""

import os
import sqlite3
import threading

from helpers import ApiTestCase

from zylker_pm import db
from zylker_pm.db import init_db

THIS_WEEK = "2026-10-12"  # the fixed clock is Friday 2026-10-16
LAST_WEEK = "2026-10-05"


class TimesheetBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.make_project("OPS", members=("bob",))
        self.a = self.make_task("WEB", "Task A")["key"]
        self.b = self.make_task("WEB", "Task B")["key"]
        self.o = self.make_task("OPS", "Ops task")["key"]

    def log(self, who, task, hours, day, note="", status=201):
        r = self.client(who).post("/api/tasks/%s/time" % task, {"hours": hours, "work_date": day, "note": note})
        self.assertEqual(r.status, status, r.text)
        return r.json

    def bobs_week(self):
        """Bob, current week: 4 + 3.5 + 2 + 6 = 15.5 h over Mon-Wed."""
        self.log("bob", self.a, 4, "2026-10-12")
        self.log("bob", self.b, 3.5, "2026-10-13")
        self.log("bob", self.o, 2, "2026-10-13")
        self.log("bob", self.a, 6, "2026-10-14")

    def bobs_last_week(self):
        """Bob, previous week: 8 + 7.5 on weekdays, 2 on the Sunday."""
        self.log("bob", self.a, 8, "2026-10-05")
        self.log("bob", self.b, 7.5, "2026-10-06")
        self.log("bob", self.o, 2, "2026-10-11")

    def submit(self, who="bob", week=THIS_WEEK, status=201, **extra):
        r = self.client(who).post("/api/timesheets/submit", {"week": week, **extra})
        self.assertEqual(r.status, status, r.text)
        return r.json

    def decide(self, who, sheet_id, verb, status=200, **body):
        r = self.client(who).post("/api/timesheets/%s/%s" % (sheet_id, verb), body)
        self.assertEqual(r.status, status, r.text)
        return r.json

    def week(self, who="bob", week=THIS_WEEK, **query):
        return self.ok(self.client(who).get("/api/timesheets/week", query={"week": week, **query}))

    def conn(self):
        conn = db.connect(self.config.db_path)
        self.addCleanup(conn.close)
        return conn

    def audit(self, **query):
        return self.ok(self.admin.get("/api/audit", query=query))["items"]


class WeekViewTests(TimesheetBase):
    def test_an_open_week_is_hand_computed(self):
        self.bobs_week()
        w = self.week()
        self.assertEqual((w["status"], w["locked"], w["id"], w["revision"]), ("open", False, None, 0))
        self.assertEqual((w["week_start"], w["week_end"], w["total_hours"]), (THIS_WEEK, "2026-10-18", 15.5))
        self.assertEqual([(d["weekday"], d["hours"]) for d in w["by_day"]], [("Monday", 4), ("Tuesday", 5.5), ("Wednesday", 6), ("Thursday", 0), ("Friday", 0), ("Saturday", 0), ("Sunday", 0)])
        self.assertEqual(w["by_project"], [{"project": "OPS", "hours": 2}, {"project": "WEB", "hours": 13.5}])
        self.assertEqual(len(w["entries"]), 4)
        self.assertEqual(w["entries"][0]["task"], self.a)
        self.assertTrue(w["can_submit"])
        self.assertEqual((w["blocking"], w["warnings"], w["integrity"], w["events"]), ([], [], None, []))

    def test_any_day_of_the_week_names_it_and_the_default_is_this_week(self):
        self.bobs_week()
        self.assertEqual(self.week(week="2026-10-18")["week_start"], THIS_WEEK)  # a Sunday
        self.assertEqual(self.week(week="2026-10-12")["week_start"], THIS_WEEK)  # a Monday
        r = self.ok(self.client("bob").get("/api/timesheets/week"))
        self.assertEqual(r["week_start"], THIS_WEEK)

    def test_a_week_with_nothing_logged_cannot_be_submitted(self):
        w = self.week()
        self.assertFalse(w["can_submit"])
        self.assertEqual([b["code"] for b in w["blocking"]], ["no_time"])

    def test_things_worth_a_look(self):
        self.bobs_last_week()
        self.log("bob", self.a, 11, "2026-10-07")
        self.log("bob", self.b, 3, "2026-10-07")  # 14 h that Wednesday
        w = self.week(week=LAST_WEEK)
        self.assertEqual(sorted(x["code"] for x in w["warnings"]), ["long_day", "weekend_work"])  # 8 + 7.5 + 2 + 14 = 31.5 h is not over the 40 h capacity
        self.assertEqual(w["total_hours"], 31.5)

    def test_over_capacity(self):
        for day in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"):
            self.log("bob", self.a, 9, day)
        w = self.week(week=LAST_WEEK)
        self.assertEqual([x["code"] for x in w["warnings"]], ["over_capacity"])  # 45 h against 40
        self.assertTrue(w["can_submit"])  # a warning, not a block

    def test_the_week_in_the_future_has_no_submit(self):
        w = self.week(week="2026-10-19")
        self.assertFalse(w["can_submit"])
        self.assertIn("future_week", [b["code"] for b in w["blocking"]])

    def test_whose_week_you_may_see(self):
        self.bobs_week()
        self.ok(self.client("bob").get("/api/timesheets/week", query={"week": THIS_WEEK, "user": "bob"}))
        self.ok(self.client("bob").get("/api/timesheets/week", query={"week": THIS_WEEK, "user": "me"}))
        self.assertEqual(self.client("bob").get("/api/timesheets/week", query={"user": "carol"}).status, 403)
        self.assertEqual(self.week("alice", user="bob")["total_hours"], 15.5)
        self.assertEqual(self.week("admin", user="bob")["user"]["username"], "bob")
        self.assertEqual(self.client("alice").get("/api/timesheets/week", query={"user": "nobody"}).status, 404)
        self.assertEqual(self.week("vera")["status"], "open")  # a viewer has a (empty) week too

    def test_bad_weeks(self):
        for bad in ("2026-13-01", "yesterday", "1999-12-31", "2026-10-1"):
            r = self.client("bob").get("/api/timesheets/week", query={"week": bad})
            self.assertEqual(r.status, 422, bad)


class SubmitTests(TimesheetBase):
    def test_submitting_locks_the_week_and_records_it(self):
        self.bobs_week()
        s = self.submit(note="Done for the week")
        self.assertEqual((s["status"], s["locked"], s["revision"], s["total_hours"]), ("submitted", True, 1, 15.5))
        self.assertEqual((s["note"], s["integrity"], s["submitted"]), ("Done for the week", "ok", {"hours": 15.5, "entries": 4}))
        self.assertIsNotNone(s["id"])
        self.assertFalse(s["can_submit"])
        self.assertEqual([e["action"] for e in s["events"]], ["submitted"])
        self.assertEqual(s["events"][0]["actor"]["username"], "bob")

    def test_the_default_week_is_this_one(self):
        self.bobs_week()
        r = self.client("bob").post("/api/timesheets/submit", {})
        self.assertEqual(r.status, 201, r.text)
        self.assertEqual(r.json["week_start"], THIS_WEEK)

    def test_nothing_to_submit(self):
        r = self.client("bob").post("/api/timesheets/submit", {"week": THIS_WEEK})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_empty"))

    def test_a_week_that_has_not_started(self):
        self.assertEqual(self.client("bob").post("/api/timesheets/submit", {"week": "2026-10-19"}).status, 422)

    def test_more_than_a_day_holds(self):
        self.log("bob", self.a, 14, "2026-10-12")
        self.log("bob", self.b, 11, "2026-10-12")
        r = self.client("bob").post("/api/timesheets/submit", {"week": THIS_WEEK})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_invalid"))
        self.assertEqual(r.json["error"]["details"]["problems"][0]["date"], "2026-10-12")

    def test_twice(self):
        self.bobs_week()
        self.submit()
        r = self.client("bob").post("/api/timesheets/submit", {"week": THIS_WEEK})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_locked"))

    def test_who_may_submit(self):
        self.assertEqual(self.client("vera").post("/api/timesheets/submit", {}).status, 403)
        self.assertEqual(self.client().post("/api/timesheets/submit", {}).status, 401)

    def test_validation(self):
        self.bobs_week()
        for body in ({"week": "nope"}, {"week": 5}, {"note": "x" * 501}, {"note": 5}, {"bogus": 1}, {"week": None}):
            self.assertEqual(self.client("bob").post("/api/timesheets/submit", body).status, 422, body)
        self.assertEqual(self.client("bob").post("/api/timesheets/submit", [1]).status, 400)

    def test_only_your_own_time_is_in_your_sheet(self):
        self.bobs_week()
        self.log("carol", self.a, 5, "2026-10-12")
        self.assertEqual(self.submit("bob")["total_hours"], 15.5)
        self.assertEqual(self.week("carol")["status"], "open")

    def test_a_manager_submits_too(self):
        self.log("alice", self.a, 3, "2026-10-12")
        self.assertEqual(self.submit("alice")["status"], "submitted")

    def test_a_read_only_api_token_cannot_submit(self):
        self.bobs_week()
        t = self.ok(self.client("bob").post("/api/tokens", {"name": "ro", "read_only": True}), 201)
        r = self.client(token=t["token"]).post("/api/timesheets/submit", {"week": THIS_WEEK})
        self.assertEqual((r.status, r.json["error"]["code"]), (403, "token_read_only"))

    def test_concurrent_submissions_make_exactly_one_sheet(self):
        self.bobs_week()
        results = []

        def go():
            results.append(self.client("bob").post("/api/timesheets/submit", {"week": THIS_WEEK}).status)

        threads = [threading.Thread(target=go) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [201, 409, 409, 409, 409])
        self.assertEqual(self.conn().execute("SELECT COUNT(*) FROM timesheets").fetchone()[0], 1)


class LockTests(TimesheetBase):
    def setUp(self):
        super().setUp()
        self.bobs_week()
        self.bobs_last_week()
        self.sheet = self.submit()

    def test_no_time_can_be_added_to_a_submitted_week(self):
        r = self.client("bob").post("/api/tasks/%s/time" % self.a, {"hours": 1, "work_date": "2026-10-15"})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_locked"))
        self.assertEqual(r.json["error"]["details"]["week_start"], THIS_WEEK)
        self.assertEqual(self.week()["total_hours"], 15.5)

    def test_nor_removed(self):
        entry = self.week()["entries"][0]["id"]
        for who in ("bob", "alice", "admin"):
            r = self.client(who).delete("/api/time/%d" % entry)
            self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_locked"), who)

    def test_other_weeks_and_other_people_are_free(self):
        self.log("bob", self.a, 1, "2026-10-09")  # last week
        self.log("carol", self.a, 1, "2026-10-14")
        entry = self.log("bob", self.a, 1, "2026-10-08")["id"]
        self.assertEqual(self.client("bob").delete("/api/time/%d" % entry).status, 204)

    def test_the_week_boundary_is_monday(self):
        self.log("bob", self.a, 1, "2026-10-11")  # the Sunday before: still last week, still open
        self.assertEqual(self.client("bob").post("/api/tasks/%s/time" % self.a, {"hours": 1, "work_date": "2026-10-17"}).status, 409)  # Saturday after

    def test_a_task_with_locked_hours_cannot_be_deleted(self):
        r = self.client("alice").delete("/api/tasks/%s" % self.b)
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_locked"))
        self.assertEqual(self.client("alice").get("/api/tasks/%s" % self.b).status, 200)
        self.decide("bob", self.sheet["id"], "withdraw")
        self.assertEqual(self.client("alice").delete("/api/tasks/%s" % self.b).status, 204)

    def test_subtasks_count_too(self):
        parent = self.make_task("WEB", "Parent")["key"]
        child = self.make_task("WEB", "Child", parent=parent)["key"]
        self.log("bob", child, 1, "2026-10-07")  # last week, which is open until it is submitted
        self.submit(week=LAST_WEEK)
        r = self.client("alice").delete("/api/tasks/%s" % parent, query={"cascade": "true"})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_locked"))
        self.assertEqual(self.client("alice").get("/api/tasks/%s" % child).status, 200)

    def test_a_project_with_locked_hours_cannot_be_deleted(self):
        r = self.admin.delete("/api/projects/OPS", query={"confirm": "OPS"})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_locked"))
        self.decide("bob", self.sheet["id"], "withdraw")
        self.assertEqual(self.admin.delete("/api/projects/OPS", query={"confirm": "OPS"}).status, 204)

    def test_open_time_does_not_protect_a_task(self):
        t = self.make_task("WEB", "Scratch")["key"]
        self.log("bob", t, 1, "2026-10-09")  # last week, never submitted
        self.assertEqual(self.client("alice").delete("/api/tasks/%s" % t).status, 204)


class WithdrawTests(TimesheetBase):
    def setUp(self):
        super().setUp()
        self.bobs_week()
        self.sheet = self.submit()

    def test_the_owner_takes_it_back(self):
        w = self.decide("bob", self.sheet["id"], "withdraw")
        self.assertEqual((w["status"], w["locked"]), ("withdrawn", False))
        self.log("bob", self.a, 1, "2026-10-15")  # editable again
        again = self.submit()
        self.assertEqual((again["revision"], again["total_hours"], again["id"]), (2, 16.5, self.sheet["id"]))
        self.assertEqual([e["action"] for e in again["events"]], ["submitted", "withdrawn", "submitted"][::-1])

    def test_only_the_owner(self):
        for who in ("carol", "alice", "admin"):
            r = self.client(who).post("/api/timesheets/%d/withdraw" % self.sheet["id"], {})
            self.assertEqual((r.status, r.json["error"]["code"]), (403, "not_owner"), who)

    def test_only_while_waiting(self):
        self.decide("alice", self.sheet["id"], "approve")
        r = self.client("bob").post("/api/timesheets/%d/withdraw" % self.sheet["id"], {})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_not_submitted"))


class DecisionTests(TimesheetBase):
    def setUp(self):
        super().setUp()
        self.bobs_week()
        self.sheet = self.submit(note="please")
        self.sid = self.sheet["id"]

    def test_approving(self):
        a = self.decide("alice", self.sid, "approve", note="Looks right")
        self.assertEqual((a["status"], a["locked"], a["integrity"]), ("approved", True, "ok"))
        self.assertEqual((a["decided_by"]["username"], a["decision_note"], a["decided_at"]), ("alice", "Looks right", "2026-10-16T09:00:00Z"))
        self.assertEqual([e["action"] for e in a["events"]], ["approved", "submitted"])
        self.assertEqual(a["events"][0]["actor"]["username"], "alice")

    def test_nobody_decides_on_their_own_sheet(self):
        self.log("alice", self.a, 2, "2026-10-12")
        mine = self.submit("alice")["id"]
        r = self.client("alice").post("/api/timesheets/%d/approve" % mine, {})
        self.assertEqual((r.status, r.json["error"]["code"]), (403, "own_timesheet"))
        self.log("admin", self.a, 2, "2026-10-12")
        theirs = self.submit("admin")["id"]
        self.assertEqual(self.client("admin").post("/api/timesheets/%d/approve" % theirs, {}).json["error"]["code"], "own_timesheet")
        self.assertEqual(self.client("admin").post("/api/timesheets/%d/reject" % theirs, {"note": "x"}).json["error"]["code"], "own_timesheet")

    def test_an_approver_needs_at_least_the_role_of_the_owner(self):
        self.log("admin", self.a, 2, "2026-10-12")
        admins = self.submit("admin")["id"]
        r = self.client("alice").post("/api/timesheets/%d/approve" % admins, {})
        self.assertEqual((r.status, r.json["error"]["code"]), (403, "approver_role_too_low"))
        self.log("alice", self.a, 2, "2026-10-12")
        managers = self.submit("alice")["id"]
        self.assertEqual(self.decide("admin", managers, "approve")["status"], "approved")  # an admin may decide on a manager

    def test_members_cannot_decide(self):
        for verb, body in (("approve", {}), ("reject", {"note": "x"})):
            self.assertEqual(self.client("carol").post("/api/timesheets/%d/%s" % (self.sid, verb), body).status, 403)
        self.assertEqual(self.client("vera").post("/api/timesheets/%d/approve" % self.sid, {}).status, 403)

    def test_only_a_submitted_sheet_is_decided(self):
        self.decide("alice", self.sid, "approve")
        for verb in ("approve", "reject"):
            r = self.client("alice").post("/api/timesheets/%d/%s" % (self.sid, verb), {"note": "x"})
            self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_not_submitted"), verb)

    def test_naming_the_revision_you_read(self):
        r = self.client("alice").post("/api/timesheets/%d/approve" % self.sid, {"revision": 7})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_changed"))
        self.assertEqual(self.decide("alice", self.sid, "approve", revision=1)["status"], "approved")

    def test_a_resubmission_invalidates_the_decision_in_flight(self):
        self.decide("bob", self.sid, "withdraw")
        self.log("bob", self.a, 1, "2026-10-15")
        self.submit()
        r = self.client("alice").post("/api/timesheets/%d/approve" % self.sid, {"revision": 1})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_changed"))
        self.assertEqual(r.json["error"]["details"]["revision"], 2)

    def test_changed_behind_the_applications_back(self):
        conn = self.conn()
        conn.execute("UPDATE time_entries SET hours = hours + 1 WHERE user_id = (SELECT id FROM users WHERE username = 'bob') AND work_date = '2026-10-12'")
        conn.commit()
        self.assertEqual(self.week()["integrity"], "changed")
        r = self.client("alice").post("/api/timesheets/%d/approve" % self.sid, {})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_integrity"))
        self.assertEqual(self.client("alice").get("/api/timesheets/%d" % self.sid).json["status"], "submitted")

    def test_integrity_of_an_approved_week(self):
        self.decide("alice", self.sid, "approve")
        self.assertEqual(self.week()["integrity"], "ok")
        conn = self.conn()
        conn.execute("DELETE FROM time_entries WHERE work_date = '2026-10-14'")
        conn.commit()
        self.assertEqual(self.client("alice").get("/api/timesheets/%d" % self.sid).json["integrity"], "changed")

    def test_sending_it_back(self):
        r = self.client("alice").post("/api/timesheets/%d/reject" % self.sid, {})
        self.assertEqual((r.status, r.json["error"]["details"]["note"]), (422, "a reason is required"))
        self.assertEqual(self.client("alice").post("/api/timesheets/%d/reject" % self.sid, {"note": "  "}).status, 422)
        rej = self.decide("alice", self.sid, "reject", note="Tuesday is missing the review")
        self.assertEqual((rej["status"], rej["locked"], rej["decision_note"]), ("rejected", False, "Tuesday is missing the review"))
        self.log("bob", self.a, 1.5, "2026-10-15")
        again = self.submit()
        self.assertEqual((again["status"], again["revision"], again["decision_note"], again["decided_by"]), ("submitted", 2, "", None))
        self.assertEqual([e["action"] for e in again["events"]], ["submitted", "rejected", "submitted"])
        self.assertEqual(again["events"][1]["note"], "Tuesday is missing the review")

    def test_reopening_an_approved_week(self):
        self.decide("alice", self.sid, "approve")
        for who in ("alice", "bob"):
            self.assertEqual(self.client(who).post("/api/timesheets/%d/reopen" % self.sid, {"note": "x"}).status, 403)
        r = self.client("admin").post("/api/timesheets/%d/reopen" % self.sid, {})
        self.assertEqual(r.status, 422)
        o = self.decide("admin", self.sid, "reopen", note="Missed a day")
        self.assertEqual((o["status"], o["locked"]), ("reopened", False))
        self.log("bob", self.a, 1, "2026-10-15")
        self.assertEqual(self.submit()["revision"], 2)

    def test_only_an_approved_week_is_reopened(self):
        r = self.client("admin").post("/api/timesheets/%d/reopen" % self.sid, {"note": "x"})
        self.assertEqual((r.status, r.json["error"]["code"]), (409, "timesheet_not_approved"))

    def test_nobody_reopens_their_own_week(self):
        self.log("admin", self.a, 2, "2026-10-12")
        s = self.submit("admin")["id"]
        self.decide("alice", s, "approve", status=403)  # the role rule
        conn = self.conn()
        conn.execute("UPDATE timesheets SET status = 'approved' WHERE id = ?", (s,))
        conn.commit()
        r = self.client("admin").post("/api/timesheets/%d/reopen" % s, {"note": "x"})
        self.assertEqual((r.status, r.json["error"]["code"]), (403, "own_timesheet"))

    def test_unknown_sheets(self):
        for path in ("/api/timesheets/999", "/api/timesheets/abc"):
            self.assertEqual(self.client("alice").get(path).status, 404, path)
        for verb in ("approve", "reject", "withdraw", "reopen"):
            self.assertEqual(self.client("admin").post("/api/timesheets/999/%s" % verb, {"note": "x"}).status, 404, verb)

    def test_validation(self):
        for body in ({"note": 5}, {"note": "x" * 501}, {"revision": "1"}, {"revision": 0}, {"revision": True}, {"bogus": 1}):
            self.assertEqual(self.client("alice").post("/api/timesheets/%d/approve" % self.sid, body).status, 422, body)


class BulkApprovalTests(TimesheetBase):
    def setUp(self):
        super().setUp()
        self.log("bob", self.a, 5, "2026-10-12")
        self.log("carol", self.a, 6, "2026-10-12")
        self.log("alice", self.a, 7, "2026-10-12")
        self.bob_sheet = self.submit("bob")["id"]
        self.carol_sheet = self.submit("carol")["id"]
        self.alice_sheet = self.submit("alice")["id"]

    def test_each_one_is_judged_on_its_own(self):
        before = len(self.notifier.messages)
        r = self.ok(self.client("alice").post("/api/timesheets/approve-many", {"ids": [self.bob_sheet, self.alice_sheet, self.carol_sheet, 9999, self.bob_sheet]}))
        self.assertEqual(r["approved"], [self.bob_sheet, self.carol_sheet])
        self.assertEqual(r["hours"], 11)
        self.assertEqual([(s["id"], s["code"]) for s in r["skipped"]], [(self.alice_sheet, "own_timesheet"), (9999, "timesheet_not_found")])
        self.assertEqual(self.client("alice").get("/api/timesheets/%d" % self.alice_sheet).json["status"], "submitted")
        self.assertEqual(len(self.notifier.messages) - before, 1)  # one message for the lot
        self.assertIn("approved 2 timesheets (11 h)", self.notifier.messages[-1])

    def test_a_skipped_one_leaves_no_trace(self):
        before = self.conn().execute("SELECT COUNT(*) FROM timesheet_events").fetchone()[0]
        self.ok(self.client("alice").post("/api/timesheets/approve-many", {"ids": [self.alice_sheet]}))
        self.assertEqual(self.conn().execute("SELECT COUNT(*) FROM timesheet_events").fetchone()[0], before)

    def test_a_shared_note(self):
        self.ok(self.client("alice").post("/api/timesheets/approve-many", {"ids": [self.bob_sheet], "note": "Batch"}))
        self.assertEqual(self.client("alice").get("/api/timesheets/%d" % self.bob_sheet).json["decision_note"], "Batch")

    def test_nothing_approved_says_nothing(self):
        before = len(self.notifier.messages)
        r = self.ok(self.client("alice").post("/api/timesheets/approve-many", {"ids": [self.alice_sheet]}))
        self.assertEqual(r["approved"], [])
        self.assertEqual(len(self.notifier.messages), before)

    def test_bad_requests(self):
        for body in ({}, {"ids": []}, {"ids": "1"}, {"ids": ["1"]}, {"ids": [True]}, {"ids": list(range(1, 102))}, {"ids": [1], "x": 1}):
            self.assertEqual(self.client("alice").post("/api/timesheets/approve-many", body).status, 422, body)
        self.assertEqual(self.client("bob").post("/api/timesheets/approve-many", {"ids": [1]}).status, 403)


class ListTests(TimesheetBase):
    def setUp(self):
        super().setUp()
        self.bobs_last_week()
        self.bobs_week()
        self.log("carol", self.a, 3, "2026-10-12")
        self.old = self.submit("bob", LAST_WEEK)["id"]
        self.now_bob = self.submit("bob")["id"]
        self.now_carol = self.submit("carol")["id"]
        self.decide("alice", self.old, "approve")

    def ids(self, who="alice", **query):
        return [i["id"] for i in self.ok(self.client(who).get("/api/timesheets", query=query))["items"]]

    def test_the_queue_is_the_oldest_submission_first(self):
        self.assertEqual(self.ids(status="submitted"), [self.now_bob, self.now_carol])

    def test_everything_is_newest_week_first(self):
        self.assertEqual(self.ids(), [self.now_carol, self.now_bob, self.old])

    def test_filters(self):
        self.assertEqual(self.ids(status="approved"), [self.old])
        self.assertEqual(self.ids(status="approved,submitted"), [self.now_carol, self.now_bob, self.old])
        self.assertEqual(self.ids(user="carol"), [self.now_carol])
        self.assertEqual(self.ids(**{"from": THIS_WEEK}), [self.now_carol, self.now_bob])
        self.assertEqual(self.ids(to=LAST_WEEK), [self.old])
        self.assertEqual(self.ids(**{"from": "2026-10-14", "to": "2026-10-14"}), [self.now_carol, self.now_bob])  # any day names its week

    def test_paging(self):
        page = self.ok(self.client("alice").get("/api/timesheets", query={"limit": 2, "offset": 1}))
        self.assertEqual((page["total"], page["limit"], page["offset"], [i["id"] for i in page["items"]]), (3, 2, 1, [self.now_bob, self.old]))

    def test_a_row(self):
        item = self.ok(self.client("alice").get("/api/timesheets", query={"status": "approved"}))["items"][0]
        self.assertEqual((item["user"]["username"], item["week_start"], item["week_end"], item["total_hours"], item["entry_count"]), ("bob", LAST_WEEK, "2026-10-11", 17.5, 3))
        self.assertEqual((item["decided_by"]["username"], item["status"]), ("alice", "approved"))

    def test_people_see_only_their_own(self):
        self.assertEqual(self.ids("bob"), [self.now_bob, self.old])
        self.assertEqual(self.ids("bob", user="bob"), [self.now_bob, self.old])
        self.assertEqual(self.ids("bob", user="me"), [self.now_bob, self.old])
        self.assertEqual(self.client("bob").get("/api/timesheets", query={"user": "carol"}).status, 403)
        self.assertEqual(self.ids("vera"), [])
        self.assertEqual(self.client("bob").get("/api/timesheets/%d" % self.now_carol).status, 403)
        self.assertEqual(self.client("bob").get("/api/timesheets/%d" % self.now_bob).status, 200)

    def test_bad_filters(self):
        for q in ({"status": "open"}, {"status": "bogus"}, {"limit": 0}, {"limit": 500}, {"limit": "x"}, {"offset": -1}, {"from": "no"}, {"to": "2026-02-30"}):
            self.assertEqual(self.client("alice").get("/api/timesheets", query=q).status, 422, q)
        self.assertEqual(self.client("alice").get("/api/timesheets", query={"user": "nobody"}).status, 404)


class MissingTests(TimesheetBase):
    def test_who_to_chase(self):
        self.bobs_last_week()
        self.log("carol", self.a, 4, "2026-09-28")  # two weeks back
        self.bobs_week()  # the current week is not finished: not listed
        r = self.ok(self.client("alice").get("/api/timesheets/missing"))
        self.assertEqual([(i["user"]["username"], i["week_start"], i["hours"], i["entries"], i["status"]) for i in r["items"]],
                         [("bob", LAST_WEEK, 17.5, 3, "open"), ("carol", "2026-09-28", 4, 1, "open")])
        self.assertEqual((r["people"], r["hours"], r["from"], r["weeks"]), (2, 21.5, "2026-09-14", 4))
        self.assertEqual(r["items"][0]["days_since_week_end"], 5)

    def test_submitted_and_approved_weeks_are_not_missing_but_sent_back_ones_are(self):
        self.bobs_last_week()
        sid = self.submit("bob", LAST_WEEK)["id"]
        self.assertEqual(self.ok(self.client("alice").get("/api/timesheets/missing"))["items"], [])
        self.decide("alice", sid, "reject", note="again")
        item = self.ok(self.client("alice").get("/api/timesheets/missing"))["items"][0]
        self.assertEqual((item["week_start"], item["status"]), (LAST_WEEK, "rejected"))
        self.decide("bob", self.submit("bob", LAST_WEEK)["id"], "withdraw")
        self.assertEqual(self.ok(self.client("alice").get("/api/timesheets/missing"))["items"][0]["status"], "withdrawn")

    def test_how_far_back(self):
        self.log("carol", self.a, 4, "2026-09-07")  # five weeks before this one
        self.assertEqual(self.ok(self.client("alice").get("/api/timesheets/missing"))["items"], [])  # the default is four
        self.assertEqual(len(self.ok(self.client("alice").get("/api/timesheets/missing", query={"weeks": 5}))["items"]), 1)

    def test_people_who_left_are_not_chased(self):
        self.bobs_last_week()
        bob = self.ok(self.admin.get("/api/users", query={"limit": 100}))["items"]
        bob_id = [u["id"] for u in bob if u["username"] == "bob"][0]
        self.ok(self.admin.patch("/api/users/%d" % bob_id, {"active": False}))
        self.assertEqual(self.ok(self.client("alice").get("/api/timesheets/missing"))["items"], [])

    def test_rules(self):
        self.assertEqual(self.client("bob").get("/api/timesheets/missing").status, 403)
        for w in (0, 27, "x"):
            self.assertEqual(self.client("alice").get("/api/timesheets/missing", query={"weeks": w}).status, 422, w)


class SummaryTests(TimesheetBase):
    def test_hours_by_approval_state(self):
        self.bobs_last_week()  # 17.5 h: WEB 15.5, OPS 2
        self.bobs_week()  # 15.5 h: WEB 13.5, OPS 2
        self.log("carol", self.a, 3, "2026-10-12")
        last = self.submit("bob", LAST_WEEK)["id"]
        self.decide("alice", last, "approve")
        self.submit("bob")  # submitted
        # carol's 3 h stay open
        r = self.ok(self.client("alice").get("/api/timesheets/summary", query={"from": "2026-10-05", "to": "2026-10-18"}))
        self.assertEqual(r["totals"], {"approved": 17.5, "submitted": 15.5, "open": 3, "total": 36})
        self.assertEqual([(p["project"], p["approved"], p["submitted"], p["open"], p["total"]) for p in r["by_project"]], [("OPS", 2, 2, 0, 4), ("WEB", 15.5, 13.5, 3, 32)])
        self.assertEqual([(u["username"], u["approved"], u["submitted"], u["open"]) for u in r["by_user"]], [("bob", 17.5, 15.5, 0), ("carol", 0, 0, 3)])

    def test_filters(self):
        self.bobs_week()
        self.log("carol", self.a, 3, "2026-10-12")
        self.submit("bob")
        r = self.ok(self.client("alice").get("/api/timesheets/summary", query={"project": "ops", "from": THIS_WEEK}))
        self.assertEqual(r["totals"], {"approved": 0, "submitted": 2, "open": 0, "total": 2})
        r = self.ok(self.client("alice").get("/api/timesheets/summary", query={"user": "carol", "from": THIS_WEEK}))
        self.assertEqual(r["totals"]["open"], 3)
        self.assertEqual(self.ok(self.client("alice").get("/api/timesheets/summary", query={"user": "nobody"}))["totals"]["total"], 0)

    def test_reopened_time_is_open_again(self):
        self.bobs_week()
        sid = self.submit()["id"]
        self.decide("alice", sid, "approve")
        self.decide("admin", sid, "reopen", note="oops")
        self.assertEqual(self.ok(self.client("alice").get("/api/timesheets/summary", query={"from": THIS_WEEK}))["totals"], {"approved": 0, "submitted": 0, "open": 15.5, "total": 15.5})

    def test_rules(self):
        self.assertEqual(self.client("bob").get("/api/timesheets/summary").status, 403)
        self.assertEqual(self.client("alice").get("/api/timesheets/summary", query={"from": "2026-10-10", "to": "2026-10-01"}).status, 422)


class AuditAndNotificationTests(TimesheetBase):
    def setUp(self):
        super().setUp()
        self.bobs_week()

    def test_the_trail(self):
        sid = self.submit()["id"]
        self.decide("alice", sid, "reject", note="Needs the review time")
        self.submit()
        self.decide("alice", sid, "approve")
        self.decide("admin", sid, "reopen", note="Late entry")
        events = self.audit(category="timesheet")
        self.assertEqual([e["action"] for e in events], ["timesheet.reopened", "timesheet.approved", "timesheet.submitted", "timesheet.rejected", "timesheet.submitted"])
        self.assertEqual(events[0]["severity"], "notice")
        self.assertEqual(events[1]["severity"], "info")
        self.assertEqual(events[1]["actor"]["username"], "alice")
        self.assertEqual((events[1]["details"]["user"], events[1]["details"]["hours"], events[1]["details"]["week_start"]), ("bob", 15.5, THIS_WEEK))

    def test_failed_attempts_are_not_events(self):
        self.client("bob").post("/api/timesheets/submit", {"week": "2026-10-05"})
        self.assertEqual(self.audit(category="timesheet"), [])

    def test_webhooks_can_follow_it(self):
        hook = self.ok(self.admin.post("/api/webhooks", {"name": "t", "url": "https://hooks.example.com/in", "events": ["timesheet.*"]}), 201)
        sid = self.submit()["id"]
        self.decide("alice", sid, "approve")
        events = [d["event"] for d in self.ok(self.admin.get("/api/webhooks/%d/deliveries" % hook["id"]))["items"]]
        self.assertEqual(sorted(events), ["timesheet.approved", "timesheet.submitted"])

    def test_chat_messages(self):
        sid = self.submit()["id"]
        self.assertIn("\U0001f4dd Bob submitted their timesheet for the week of %s (15.5 h)" % THIS_WEEK, self.notifier.messages[-1])
        self.decide("alice", sid, "reject", note="Needs *bold* [x](http://evil) work")
        self.assertIn("Alice sent back Bob's timesheet", self.notifier.messages[-1])
        self.assertNotIn("](", self.notifier.messages[-1])  # no link survives
        self.assertNotIn("*bold*", self.notifier.messages[-1])
        self.submit()
        self.decide("alice", sid, "approve")
        self.assertIn("Alice approved Bob's timesheet for the week of %s (15.5 h)" % THIS_WEEK, self.notifier.messages[-1])
        self.decide("admin", sid, "reopen", note="Late entry")
        self.assertIn("reopened Bob's approved timesheet", self.notifier.messages[-1])

    def test_a_refused_request_sends_nothing(self):
        sid = self.submit()["id"]
        before = len(self.notifier.messages)
        self.client("alice").post("/api/timesheets/%d/approve" % sid, {"revision": 9})
        self.client("carol").post("/api/timesheets/%d/approve" % sid, {})
        self.assertEqual(len(self.notifier.messages), before)


class SafetyTests(TimesheetBase):
    def test_hostile_input_never_causes_a_server_error(self):
        self.bobs_week()
        sid = self.submit()["id"]
        junk = [None, "", "0", "-1", "1e9", "' OR 1=1 --", "\x00", "2026-10-12'; DROP TABLE timesheets;--", "9" * 400, [], {}, True, 10**30]
        for who in ("bob", "alice", "admin"):
            for v in junk:
                for path in ("/api/timesheets", "/api/timesheets/week", "/api/timesheets/missing", "/api/timesheets/summary"):
                    for key in ("week", "user", "status", "from", "to", "weeks", "limit", "offset", "project"):
                        if isinstance(v, (list, dict, bool, int)):
                            continue
                        r = self.client(who).get(path, query={key: v})
                        self.assertLess(r.status, 500, (path, key, v, r.text))
                for verb in ("submit", "approve-many", "%d/approve" % sid, "%d/reject" % sid, "%d/reopen" % sid, "%d/withdraw" % sid):
                    for body in ({"week": v}, {"note": v}, {"ids": v}, {"revision": v}, v):
                        r = self.client(who).post("/api/timesheets/" + verb, body)
                        self.assertLess(r.status, 500, (verb, body, r.text))
        self.assertEqual(self.conn().execute("SELECT COUNT(*) FROM timesheets").fetchone()[0], 1)
        self.assertIn(self.conn().execute("SELECT status FROM timesheets").fetchone()[0], ("submitted", "withdrawn", "approved", "rejected", "reopened"))


class UpgradeTests(TimesheetBase):
    def v9_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        for version in range(1, 9):
            db._migrate(conn, version)
        conn.close()

    def names(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_an_older_database_upgrades_to_the_same_schema_as_a_new_one(self):
        old = os.path.join(self.tmp, "v9.db")
        self.v9_database(old)
        self.assertNotIn("timesheets", self.names(old)[0])
        init_db(old)
        fresh = os.path.join(self.tmp, "fresh10.db")
        init_db(fresh)
        self.assertEqual(self.names(old), self.names(fresh))
        self.assertEqual(self.names(old)[1], db.SCHEMA_VERSION)

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v9b.db")
        self.v9_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE timesheet_events (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        names, version = self.names(path)
        self.assertEqual(version, 9)
        self.assertNotIn("timesheets", names)
