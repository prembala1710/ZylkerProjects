"""Delivery projection, part 2: from database rows to a schedule, an explanation and an API.

Every expected date below was worked out by hand. The clock is Friday 2026-10-16, so working day 0 is that Friday, day 1 is
Monday the 19th, day 2 Tuesday the 20th and so on; people work their weekly capacity (40 hours = 8 a day unless stated) on one task at a time.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from helpers import ApiTestCase

from zylker_pm.services import projection


class ProjectionCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"), due_date="2026-10-20")

    def task(self, title, hours, assignee, due=None, key="WEB", **fields):
        data = {"estimate_hours": hours, "assignee": assignee, **fields}
        if due:
            data["due_date"] = due
        return self.make_task(key, title, **data)["key"]

    def view(self, key="WEB", user="vera", **query):
        return self.ok(self.client(user).get("/api/projects/%s/projection" % key, query=query))

    def tasks(self, key="WEB", **query):
        return {t["key"]: t for t in self.view(key, **query)["tasks"]}

    def block(self, task, blocked_by):
        self.ok(self.client("alice").post("/api/tasks/%s/dependencies" % task, {"blocked_by": blocked_by}))

    def finish_dates(self, key="WEB", **query):
        return {k: t["projected_finish"] for k, t in self.tasks(key, **query).items()}

    def user_id(self, username):
        return next(u["id"] for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == username)


class SingleQueueTests(ProjectionCase):
    def setUp(self):
        super().setUp()
        # bob: 16h + 8h + 4h on one 8-hour-a-day person. Priority is equal for the first two, so the earlier due date goes first.
        self.task("Build it", 16, "bob", "2026-10-19")  # WEB-1: day 0 -> 2
        self.task("Test it", 8, "bob", "2026-10-20")  # WEB-2: 2 -> 3
        self.task("Polish it", 4, "bob", "2026-10-20", priority="low")  # WEB-3: 3 -> 3.5

    def test_the_queue_is_played_forward_day_by_day(self):
        tasks = self.tasks()
        self.assertEqual({k: (t["projected_start"], t["projected_finish"]) for k, t in tasks.items()}, {
            "WEB-1": ("2026-10-16", "2026-10-19"), "WEB-2": ("2026-10-20", "2026-10-20"), "WEB-3": ("2026-10-21", "2026-10-21"),
        })  # fmt: skip
        self.assertEqual({k: t["late_by_days"] for k, t in tasks.items()}, {"WEB-1": 0, "WEB-2": 0, "WEB-3": 1})
        self.assertEqual({k: t["margin_days"] for k, t in tasks.items()}, {"WEB-1": 0, "WEB-2": 0, "WEB-3": -1})

    def test_the_project_headline_and_counts(self):
        v = self.view()
        self.assertEqual((v["state"], v["verdict"], v["finish"], v["due_date"], v["late_by_days"]), ("projected", "will_miss", "2026-10-21", "2026-10-20", 1))
        self.assertEqual(v["headline"], "Projected to finish Wed 21 Oct, 1 day after the due date (Tue 20 Oct). 1 of 3 open tasks will miss its own due date.")
        self.assertEqual(v["counts"], {"open_tasks": 3, "late_tasks": 1, "unprojectable": 0, "overdue_tasks": 0})
        self.assertEqual((v["project"], v["name"], v["as_of"], v["status"], v["reason"]), ("WEB", "Project WEB", "2026-10-16", "active", None))

    def test_late_tasks_come_first_and_every_task_is_explained(self):
        v = self.view()
        self.assertEqual([t["key"] for t in v["tasks"]], ["WEB-3", "WEB-1", "WEB-2"])
        by_key = {t["key"]: t for t in v["tasks"]}
        self.assertEqual(by_key["WEB-1"]["explanation"], "Starts right away: Bob is free and nothing blocks it.")
        self.assertEqual(by_key["WEB-1"]["why"], {"kind": "immediately"})
        self.assertEqual(by_key["WEB-3"]["explanation"], "Bob is busy with WEB-2 (Test it) until Tue 20 Oct, so this starts after it.")
        self.assertEqual(by_key["WEB-3"]["why"], {"kind": "queue", "task": "WEB-2", "until": "2026-10-20"})

    def test_a_task_entry_has_everything_the_ui_needs(self):
        entry = self.tasks()["WEB-3"]
        self.assertEqual(
            entry,
            {"key": "WEB-3", "title": "Polish it", "status": "todo", "priority": "low", "assignee": "bob", "due_date": "2026-10-20", "overdue": False,
             "remaining_hours": 4.0, "state": "scheduled", "projected_start": "2026-10-21", "projected_finish": "2026-10-21", "margin_days": -1,
             "late_by_days": 1, "why": {"kind": "queue", "task": "WEB-2", "until": "2026-10-20"},
             "explanation": "Bob is busy with WEB-2 (Test it) until Tue 20 Oct, so this starts after it.", "notes": []},
        )  # fmt: skip

    def test_the_person_summary(self):
        (bob,) = self.view()["people"]
        self.assertEqual(
            bob,
            {"username": "bob", "display_name": "Bob", "hours_per_day": 8.0, "capacity_pct": 100.0, "open_hours": 28.0, "open_hours_here": 28.0, "late_tasks": 1, "busy_until": "2026-10-21"},
        )

    def test_a_project_that_fits_says_how_much_room_it_has(self):
        self.ok(self.client("alice").patch("/api/projects/WEB", {"due_date": "2026-10-30"}))
        v = self.view()
        self.assertEqual((v["verdict"], v["late_by_days"]), ("on_track", 0))
        self.assertEqual(v["headline"], "Projected to finish Wed 21 Oct, 9 days before the due date (Fri 30 Oct). 1 of 3 open tasks will miss its own due date.")  # the project fits; the polish does not
        self.ok(self.client("alice").patch("/api/projects/WEB", {"due_date": "2026-10-21"}))
        self.assertEqual(self.view()["headline"], "Projected to finish Wed 21 Oct, on the due date (Wed 21 Oct). 1 of 3 open tasks will miss its own due date.")

    def test_a_project_without_a_due_date_has_nothing_to_be_late_against(self):
        self.ok(self.client("alice").patch("/api/projects/WEB", {"due_date": None}))
        v = self.view()
        self.assertEqual((v["verdict"], v["late_by_days"], v["finish"]), ("no_due_date", None, "2026-10-21"))
        self.assertEqual(v["headline"], "Projected to finish Wed 21 Oct. The project has no due date, so there is nothing to compare it with. 1 of 3 open tasks will miss its own due date.")

    def test_tasks_without_a_due_date_are_never_late(self):
        key = self.task("Whenever", 8, "bob")
        entry = self.tasks()[key]
        self.assertEqual((entry["late_by_days"], entry["margin_days"], entry["due_date"]), (None, None, None))
        self.assertEqual(entry["projected_finish"], "2026-10-21")  # medium priority beats the low-priority polish: day 3 -> 4

    def test_priority_beats_the_due_date(self):
        self.ok(self.client("alice").patch("/api/tasks/WEB-3", {"priority": "critical", "due_date": "2026-11-30"}))
        tasks = self.tasks()
        self.assertEqual(tasks["WEB-3"]["projected_start"], "2026-10-16")  # the critical one goes first even though it is due last
        self.assertEqual(tasks["WEB-1"]["projected_start"], "2026-10-16")  # and is done in half a day, so the 16-hour task starts the same day
        self.assertEqual(tasks["WEB-1"]["projected_finish"], "2026-10-20")  # 0.5 -> 2.5
        self.assertEqual(tasks["WEB-2"]["projected_finish"], "2026-10-21")  # 2.5 -> 3.5


class ExplanationTests(ProjectionCase):
    def test_a_task_waits_for_its_blocker_while_somebody_else_does_something_useful(self):
        self.task("Prepare", 8, "bob", "2026-10-16")  # WEB-1
        self.task("Review", 8, "carol", "2026-10-19")  # WEB-2, waits for WEB-1
        self.task("Tidy up", 8, "carol", priority="low")  # WEB-3: carol does this while she waits
        self.block("WEB-2", "WEB-1")
        tasks = self.tasks()
        self.assertEqual((tasks["WEB-3"]["projected_start"], tasks["WEB-3"]["projected_finish"]), ("2026-10-16", "2026-10-16"))
        self.assertEqual((tasks["WEB-2"]["projected_start"], tasks["WEB-2"]["projected_finish"]), ("2026-10-19", "2026-10-19"))
        self.assertEqual(tasks["WEB-2"]["why"], {"kind": "blocker", "task": "WEB-1", "until": "2026-10-16"})
        self.assertEqual(tasks["WEB-2"]["explanation"], "Waits for WEB-1 (Prepare), which finishes Fri 16 Oct.")

    def test_the_root_cause_of_a_delay_is_named(self):
        self.task("Big job", 24, "bob", "2026-10-20", priority="high")  # WEB-1: 0 -> 3
        self.task("Small job", 8, "bob", "2026-10-20")  # WEB-2: queued behind WEB-1, 3 -> 4
        self.task("Follow-up", 8, "carol", "2026-10-20")  # WEB-3 waits for WEB-2: 4 -> 5
        self.block("WEB-3", "WEB-2")
        entry = self.tasks()["WEB-3"]
        self.assertEqual((entry["projected_start"], entry["projected_finish"], entry["late_by_days"]), ("2026-10-22", "2026-10-22", 2))
        self.assertEqual(
            entry["explanation"],
            "Waits for WEB-2 (Small job), which finishes Wed 21 Oct. The delay starts in Bob's queue: busy with WEB-1 until Tue 20 Oct.",
        )

    def test_a_start_date_is_named_as_the_reason_when_it_is_the_reason(self):
        self.task("Not before", 8, "bob", "2026-10-30", start_date="2026-10-22")
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["projected_start"], entry["projected_finish"]), ("2026-10-22", "2026-10-22"))
        self.assertEqual(entry["why"], {"kind": "start_date"})
        self.assertEqual(entry["explanation"], "Its start date (Thu 22 Oct) holds it back.")

    def test_a_delay_that_starts_with_a_start_date_says_so(self):
        self.task("Held back", 8, "bob", start_date="2026-10-22")  # WEB-1: day 4
        self.task("After it", 8, "carol", "2026-10-22")  # WEB-2 waits for WEB-1: day 5
        self.block("WEB-2", "WEB-1")
        self.assertEqual(
            self.tasks()["WEB-2"]["explanation"],
            "Waits for WEB-1 (Held back), which finishes Thu 22 Oct. The delay starts with WEB-1, which cannot begin before its start date (Thu 22 Oct).",
        )

    def test_a_start_date_in_the_past_changes_nothing(self):
        self.task("Old start", 8, "bob", start_date="2026-09-01")
        self.assertEqual(self.tasks()["WEB-1"]["why"], {"kind": "immediately"})

    def test_people_who_cannot_work_leave_their_tasks_unschedulable_and_say_why(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"weekly_capacity_hours": 0}))
        self.task("Needs carol", 8, "carol", "2026-10-20")  # WEB-1
        self.task("Needs the first", 8, "bob", "2026-10-20")  # WEB-2
        self.task("Independent", 8, "bob", "2026-10-20", priority="low")  # WEB-3
        self.block("WEB-2", "WEB-1")
        v = self.view()
        by_key = {t["key"]: t for t in v["tasks"]}
        self.assertEqual((by_key["WEB-1"]["state"], by_key["WEB-1"]["projected_finish"]), ("no_capacity", None))
        self.assertEqual(by_key["WEB-1"]["explanation"], "Carol has no capacity (0 hours a week), so this cannot be scheduled.")
        self.assertEqual((by_key["WEB-2"]["state"], by_key["WEB-2"]["explanation"]), ("waiting_for_unschedulable", "Waits for WEB-1, which cannot be scheduled."))
        self.assertEqual(by_key["WEB-3"]["projected_finish"], "2026-10-16")
        self.assertEqual((v["verdict"], v["late_by_days"], v["counts"]["unprojectable"]), ("unknown", None, 2))
        self.assertEqual(v["headline"], "Cannot be projected completely: 2 tasks cannot be scheduled (see the tasks below).")


class LatenessExplanationTests(ProjectionCase):
    def test_a_task_that_starts_at_once_but_cannot_make_its_date_blames_the_work_itself(self):
        self.task("Too big", 40, "bob", "2026-10-19")  # five days of work, due in one
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["projected_finish"], entry["late_by_days"]), ("2026-10-22", 3))
        self.assertEqual(
            entry["explanation"],
            "Starts right away: Bob is free and nothing blocks it. The work itself (40 hours at 8 a day) runs past its due date.",
        )

    def test_a_task_that_was_due_in_the_past_says_it_is_already_overdue(self):
        self.task("Forgotten", 8, "bob", "2026-10-14")
        self.assertEqual(self.tasks()["WEB-1"]["explanation"], "Starts right away: Bob is free and nothing blocks it. It is already overdue.")

    def test_the_pace_in_the_explanation_is_the_persons_own(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("bob"), {"weekly_capacity_hours": 20}))
        self.task("Part-time", 10, "bob", "2026-10-19")
        self.assertIn("The work itself (10 hours at 4 a day) runs past its due date.", self.tasks()["WEB-1"]["explanation"])

    def test_nothing_is_added_when_the_task_is_on_time(self):
        self.task("Fine", 8, "bob", "2026-10-30")
        self.assertEqual(self.tasks()["WEB-1"]["explanation"], "Starts right away: Bob is free and nothing blocks it.")


class DateFormattingTests(ProjectionCase):
    def test_the_year_is_shown_only_when_it_is_not_this_year(self):
        self.task("Long haul", 480, "bob", "2026-12-01")  # sixty working days: Thursday 7 January 2027
        v = self.view()
        self.assertEqual(v["finish"], "2027-01-07")
        self.assertEqual(v["headline"], "Projected to finish Thu 7 Jan 2027, 79 days after the due date (Tue 20 Oct). 1 of 1 open task will miss its own due date.")
        self.task("Follow-up", 8, "carol", "2026-12-01")
        self.block("WEB-2", "WEB-1")
        self.assertEqual(
            self.tasks()["WEB-2"]["explanation"],
            "Waits for WEB-1 (Long haul), which finishes Thu 7 Jan 2027.",
        )
        self.assertIn("Tue 20 Oct)", v["headline"])  # this year's dates stay short


class ParentAndDependencyTests(ProjectionCase):
    def test_something_that_waits_for_a_parent_waits_for_all_of_its_open_subtasks(self):
        self.make_task("WEB", "Epic", type="epic")  # WEB-1: a container, never scheduled itself
        self.task("Part one", 8, "bob", "2026-10-20", parent="WEB-1")  # WEB-2: 0 -> 1
        self.task("Part two", 8, "bob", "2026-10-20", parent="WEB-1")  # WEB-3: 1 -> 2
        self.task("After the epic", 8, "carol", "2026-10-20")  # WEB-4
        self.block("WEB-4", "WEB-1")
        tasks = self.tasks()
        self.assertNotIn("WEB-1", tasks)
        entry = tasks["WEB-4"]
        self.assertEqual((entry["projected_start"], entry["projected_finish"]), ("2026-10-20", "2026-10-20"))
        self.assertEqual(entry["why"], {"kind": "blocker", "task": "WEB-3", "until": "2026-10-19"})
        self.assertEqual(self.view()["counts"]["open_tasks"], 3)  # only the leaves carry work

    def test_a_parent_that_is_itself_blocked_passes_that_on_to_whoever_waits_for_it(self):
        self.make_task("WEB", "Epic", type="epic")  # WEB-1
        self.task("Part", 8, "bob", "2026-10-20", parent="WEB-1")  # WEB-2: 0 -> 1
        self.task("Prerequisite", 24, "carol", "2026-10-20")  # WEB-3: 0 -> 3, and the epic waits for it
        self.task("After the epic", 8, "bob", "2026-10-22")  # WEB-4
        self.block("WEB-1", "WEB-3")
        self.block("WEB-4", "WEB-1")
        entry = self.tasks()["WEB-4"]
        self.assertEqual(entry["why"], {"kind": "blocker", "task": "WEB-3", "until": "2026-10-20"})  # the later of the part (day 1) and the epic's own blocker (day 3)
        self.assertEqual((entry["projected_start"], entry["projected_finish"]), ("2026-10-21", "2026-10-21"))
        self.assertEqual(self.tasks()["WEB-2"]["projected_finish"], "2026-10-16")  # the epic's blocker does not hold up its own subtasks

    def test_closed_blockers_and_closed_tasks_are_ignored(self):
        self.task("Done already", 8, "bob", "2026-10-20")  # WEB-1
        self.task("Cancelled", 8, "bob", "2026-10-20")  # WEB-2
        self.task("Open", 8, "carol", "2026-10-20")  # WEB-3
        self.block("WEB-3", "WEB-1")
        self.ok(self.set_status("WEB-1", "done"))
        self.ok(self.set_status("WEB-2", "cancelled"))
        tasks = self.tasks()
        self.assertEqual(list(tasks), ["WEB-3"])
        self.assertEqual(tasks["WEB-3"]["why"], {"kind": "immediately"})  # nothing left to wait for

    def test_a_task_flagged_blocked_is_scheduled_but_the_date_is_called_optimistic(self):
        self.task("Waiting outside", 8, "bob", "2026-10-20")
        self.ok(self.set_status("WEB-1", "blocked"))
        entry = self.tasks()["WEB-1"]
        self.assertEqual(entry["projected_finish"], "2026-10-16")
        self.assertEqual(entry["notes"], ["Flagged as blocked: this date assumes the blocker clears now"])


class EffortTests(ProjectionCase):
    def test_time_already_logged_is_taken_off(self):
        self.task("Half done", 16, "bob", "2026-10-20")
        self.ok(self.client("bob").post("/api/tasks/WEB-1/time", {"hours": 8}), 201)
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["remaining_hours"], entry["projected_finish"]), (8.0, "2026-10-16"))

    def test_a_task_past_its_estimate_has_only_closing_left(self):
        self.task("Over budget", 4, "bob", "2026-10-20")
        for day in ("2026-10-15", "2026-10-16"):
            self.ok(self.client("bob").post("/api/tasks/WEB-1/time", {"hours": 3, "work_date": day}), 201)
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["remaining_hours"], entry["projected_finish"]), (0.0, "2026-10-16"))
        self.assertEqual(entry["notes"], ["Already at or over its estimate: only closing it is left"])

    def test_a_task_without_an_estimate_counts_the_default_hours(self):
        self.make_task("WEB", "Who knows", assignee="bob", due_date="2026-10-20")
        entry = self.tasks()["WEB-1"]
        self.assertEqual(entry["remaining_hours"], 8.0)
        self.assertEqual(entry["notes"], ["No estimate: assumed 8 hours"])

    def test_the_default_hours_come_from_the_configuration(self):
        import dataclasses

        from zylker_pm.app import App

        self.make_task("WEB", "Who knows", assignee="bob", due_date="2026-10-20")
        self.app = App(dataclasses.replace(self.config, default_task_hours=16.0), clock=lambda: self.now, notifier=self.notifier)
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["remaining_hours"], entry["projected_finish"]), (16.0, "2026-10-19"))

    def test_unassigned_tasks_proceed_side_by_side_at_eight_hours_a_day(self):
        self.task("Nobody one", 16, None, "2026-10-20")
        self.task("Nobody two", 16, None, "2026-10-20")
        tasks = self.tasks()
        self.assertEqual({k: t["projected_finish"] for k, t in tasks.items()}, {"WEB-1": "2026-10-19", "WEB-2": "2026-10-19"})
        self.assertEqual(tasks["WEB-1"]["notes"], ["Unassigned: assumed to proceed at 8 hours a day without queueing"])
        self.assertEqual(tasks["WEB-1"]["explanation"], "Starts right away; with nobody assigned this assumes someone is free.")

    def test_a_deactivated_assignee_is_treated_as_unassigned_and_the_note_says_so(self):
        self.task("Carol's", 8, "carol", "2026-10-20")
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"active": False}))
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["assignee"], entry["projected_finish"]), (None, "2026-10-16"))
        self.assertEqual(entry["notes"], ["The assignee is inactive: treated as unassigned"])

    def test_part_time_people_take_longer(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("bob"), {"weekly_capacity_hours": 20}))  # 4 hours a day
        self.task("Ten hours", 10, "bob", "2026-10-20")
        self.assertEqual(self.tasks()["WEB-1"]["projected_finish"], "2026-10-20")  # 2.5 days: Fri, Mon, half of Tue
        self.assertEqual(self.view()["people"][0]["hours_per_day"], 4.0)


class SharedPeopleTests(ProjectionCase):
    def test_one_person_cannot_be_promised_to_two_projects_at_once(self):
        self.make_project("OPS", members=("carol",), due_date="2026-10-19")
        self.task("Web work", 16, "carol", "2026-10-19", priority="high")  # WEB-1: 0 -> 2
        self.task("Ops work", 8, "carol", "2026-10-19", key="OPS")  # OPS-1: queued behind WEB-1, 2 -> 3
        ops = self.tasks("OPS")["OPS-1"]
        self.assertEqual((ops["projected_finish"], ops["late_by_days"]), ("2026-10-20", 1))
        self.assertEqual(ops["explanation"], "Carol is busy with WEB-1 (Web work) until Mon 19 Oct, so this starts after it.")
        self.assertEqual(self.tasks("WEB")["WEB-1"]["late_by_days"], 0)
        (carol,) = self.view("OPS")["people"]
        self.assertEqual((carol["open_hours"], carol["open_hours_here"], carol["busy_until"]), (24.0, 8.0, "2026-10-20"))

    def test_projects_on_hold_do_not_use_up_anybodys_time(self):
        self.make_project("OPS", members=("carol",))
        self.task("Ops work", 40, "carol", None, key="OPS", priority="critical")  # five days of carol's time, and it goes first
        self.task("Web work", 8, "carol", "2026-10-20")
        self.assertEqual(self.tasks()["WEB-1"]["projected_finish"], "2026-10-23")  # while OPS is active: Friday the 23rd, the sixth working day
        self.ok(self.client("alice").patch("/api/projects/OPS", {"status": "on_hold"}))
        self.assertEqual(self.tasks()["WEB-1"]["projected_finish"], "2026-10-16")  # nobody is working on it: carol is free today

    def test_projects_that_are_not_being_worked_on_say_so(self):
        self.make_project("OPS", members=("carol",))
        self.task("Ops work", 8, "carol", None, key="OPS")
        for status, text in (("on_hold", "Not projected: the project is on hold."), ("archived", "Not projected: the project is archived.")):
            self.ok(self.client("alice").patch("/api/projects/OPS", {"status": status}))
            v = self.view("OPS")
            self.assertEqual((v["state"], v["reason"], v["headline"], v["tasks"], v["finish"]), ("not_projected", text, text, [], None), status)

    def test_a_project_without_open_work_is_complete(self):
        self.make_project("OPS", members=("carol",))
        key = self.task("Only task", 8, "carol", None, key="OPS")
        self.ok(self.set_status(key, "done"))
        v = self.view("OPS")
        self.assertEqual((v["state"], v["verdict"], v["headline"], v["finish"]), ("complete", "on_track", "No open work: there is nothing to project.", None))


class MilestoneTests(ProjectionCase):
    def test_milestones_are_projected_from_their_open_tasks(self):
        late = self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "Beta", "due_date": "2026-10-19"}), 201)["id"]
        fine = self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "GA", "due_date": "2026-11-30"}), 201)["id"]
        empty = self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "Someday", "due_date": "2026-12-31"}), 201)["id"]
        self.task("Beta one", 16, "bob", "2026-10-19", milestone=late)  # day 0 -> 2: Monday the 19th, on time
        self.task("Beta two", 8, "bob", "2026-10-20", milestone=late)  # 2 -> 3: Tuesday the 20th, a day after the milestone
        self.task("GA work", 8, "carol", "2026-11-20", milestone=fine)
        got = {m["id"]: m for m in self.view()["milestones"]}
        self.assertEqual((got[late]["projected_finish"], got[late]["late_by_days"], got[late]["open_tasks"]), ("2026-10-20", 1, 2))
        self.assertEqual((got[fine]["projected_finish"], got[fine]["late_by_days"]), ("2026-10-16", 0))
        self.assertEqual((got[empty]["projected_finish"], got[empty]["late_by_days"], got[empty]["open_tasks"]), (None, None, 0))

    def test_a_milestone_with_unschedulable_work_has_no_projected_finish(self):
        ms = self.ok(self.client("alice").post("/api/projects/WEB/milestones", {"name": "Beta", "due_date": "2026-10-19"}), 201)["id"]
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"weekly_capacity_hours": 0}))
        self.task("Stuck", 8, "carol", milestone=ms)
        self.task("Fine", 8, "bob", milestone=ms)
        (got,) = self.view()["milestones"]
        self.assertEqual((got["projected_finish"], got["late_by_days"], got["open_tasks"]), (None, None, 2))


class WeekendAndOverdueTests(ProjectionCase):
    def test_on_a_weekend_the_first_working_day_is_monday(self):
        self.now = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)  # Saturday
        self.task("Weekend plan", 8, "bob", "2026-10-19")
        v = self.view()
        entry = v["tasks"][0]
        self.assertEqual((v["as_of"], entry["projected_start"], entry["projected_finish"], entry["late_by_days"]), ("2026-10-17", "2026-10-19", "2026-10-19", 0))

    def test_the_week_end_is_skipped_when_work_runs_across_it(self):
        self.task("Long job", 24, "bob", "2026-10-20")  # Fri, Mon, Tue
        self.assertEqual(self.tasks()["WEB-1"]["projected_finish"], "2026-10-20")
        self.task("Longer job", 40, "carol", "2026-10-20")  # five working days: Fri .. Thu
        self.assertEqual(self.tasks()["WEB-2"]["projected_finish"], "2026-10-22")

    def test_overdue_tasks_are_flagged_and_measured_against_their_due_date(self):
        self.task("Late already", 8, "bob", "2026-10-14")
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["overdue"], entry["projected_finish"], entry["late_by_days"]), (True, "2026-10-16", 2))
        self.assertEqual(self.view()["counts"]["overdue_tasks"], 1)

    def test_the_time_zone_decides_what_today_is(self):
        import dataclasses

        from zylker_pm.app import App

        self.app = App(dataclasses.replace(self.config, timezone="Asia/Kolkata"), clock=lambda: self.now, notifier=self.notifier)
        self.now = datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)  # already Saturday the 17th in India: the plan starts on Monday
        self.task("Evening", 8, "bob", "2026-10-19")
        entry = self.tasks()["WEB-1"]
        self.assertEqual((entry["projected_start"], self.view()["as_of"]), ("2026-10-19", "2026-10-17"))


class WhatIfTests(ProjectionCase):
    def setUp(self):
        super().setUp()
        self.task("Build it", 16, "bob", "2026-10-19")  # the same queue as in SingleQueueTests
        self.task("Test it", 8, "bob", "2026-10-20")
        self.task("Polish it", 4, "bob", "2026-10-20", priority="low")

    def test_less_availability_stretches_the_plan_and_the_answer_shows_the_difference(self):
        v = self.view(capacity="bob:50")  # four hours a day: 4 + 2 + 1 days
        self.assertEqual({k: t["projected_finish"] for k, t in {t["key"]: t for t in v["tasks"]}.items()}, {"WEB-1": "2026-10-21", "WEB-2": "2026-10-23", "WEB-3": "2026-10-26"})
        self.assertEqual((v["finish"], v["late_by_days"], v["counts"]["late_tasks"]), ("2026-10-26", 6, 3))
        self.assertEqual(
            v["scenario"],
            {"capacity": {"bob": 50.0}, "overrun": 0.0, "reassign": [],
             "baseline": {"finish": "2026-10-21", "late_by_days": 1, "late_tasks": 1, "verdict": "will_miss"},
             "finish_moves_by_days": 5, "extra_late_tasks": 2},
        )  # fmt: skip
        (bob,) = v["people"]
        self.assertEqual((bob["hours_per_day"], bob["capacity_pct"]), (4.0, 50.0))

    def test_estimates_that_overrun_have_the_same_effect_as_a_slower_pace(self):
        slower = self.finish_dates(capacity="bob:50")
        overrun = self.finish_dates(overrun="100")
        self.assertEqual(slower, overrun)
        v = self.view(overrun="100")
        self.assertEqual((v["scenario"]["overrun"], v["scenario"]["capacity"]), (100.0, {}))
        self.assertEqual(self.tasks(overrun="100")["WEB-1"]["remaining_hours"], 32.0)

    def test_more_availability_than_normal_is_allowed(self):
        self.assertEqual(self.view(capacity="bob:200")["finish"], "2026-10-19")  # 16 + 8 + 4 hours at 16 a day: 1.75 days

    def test_somebody_who_is_away_cannot_finish_anything(self):
        v = self.view(capacity="bob:0")
        self.assertEqual((v["verdict"], v["counts"]["unprojectable"], v["finish"]), ("unknown", 3, None))
        self.assertEqual(v["scenario"]["baseline"]["finish"], "2026-10-21")

    def test_the_options_apply_to_the_portfolio_too(self):
        report = self.ok(self.client("vera").get("/api/reports/projection", query={"capacity": "bob:50"}))
        (item,) = report["projects"]
        self.assertEqual((item["finish"], item["baseline_finish"], item["late_tasks"], item["baseline_late_tasks"]), ("2026-10-26", "2026-10-21", 3, 1))
        self.assertEqual(report["scenario"], {"capacity": {"bob": 50.0}, "overrun": 0.0, "reassign": []})

    def test_nothing_is_changed_by_asking(self):
        before = self.ok(self.client("vera").get("/api/tasks/WEB-1"))
        self.view(capacity="bob:10", overrun="300")
        self.assertEqual(self.ok(self.client("vera").get("/api/tasks/WEB-1")), before)

    def test_bad_options_are_reported_field_by_field(self):
        for query, fields in (
            ({"capacity": "nobody:50"}, ["capacity"]), ({"capacity": "bob"}, ["capacity"]), ({"capacity": "bob:"}, ["capacity"]),
            ({"capacity": "bob:250"}, ["capacity"]), ({"capacity": "bob:-5"}, ["capacity"]), ({"capacity": "bob:fifty"}, ["capacity"]),
            ({"capacity": "bob:nan"}, ["capacity"]), ({"capacity": "bob:50,bob:60"}, ["capacity"]), ({"capacity": ":50"}, ["capacity"]),
            ({"overrun": "-1"}, ["overrun"]), ({"overrun": "301"}, ["overrun"]), ({"overrun": "lots"}, ["overrun"]), ({"overrun": "inf"}, ["overrun"]),
            ({"capacity": "bob:999", "overrun": "x"}, ["capacity", "overrun"]),
        ):  # fmt: skip
            r = self.client("vera").get("/api/projects/WEB/projection", query=query)
            self.assertEqual((r.status, sorted(r.json["error"]["details"])), (422, fields), query)
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"active": False}))
        self.assertEqual(self.client("vera").get("/api/projects/WEB/projection", query={"capacity": "carol:50"}).status, 422)  # inactive

    def test_fractional_percentages_and_several_people(self):
        v = self.view(capacity="bob:62.5,carol:100", overrun="12.5")
        self.assertEqual(v["scenario"]["capacity"], {"bob": 62.5, "carol": 100.0})
        self.assertEqual(v["scenario"]["overrun"], 12.5)
        # 28 hours * 1.125 = 31.5 hours at 5 hours a day = 6.3 days
        self.assertEqual(v["finish"], "2026-10-26")


class PortfolioTests(ProjectionCase):
    def test_projects_are_listed_worst_first(self):
        self.make_project("OPS", members=("carol",), due_date="2026-12-31")
        self.make_project("LAB", members=("carol",))
        self.make_project("OLD", members=("carol",))
        self.task("Web", 24, "bob", "2026-10-20")  # fits: Fri, Mon, Tue (the 20th, the due date)
        self.task("Ops", 8, "carol", "2026-12-01", key="OPS")  # fits easily
        self.task("Lab", 8, "carol", None, key="LAB")  # no project due date
        self.task("Web too", 24, "bob", "2026-10-20")  # pushes WEB beyond its due date: day 3 -> 6, Friday the 23rd
        self.ok(self.client("alice").patch("/api/projects/OLD", {"status": "on_hold"}))
        self.make_project("GONE", members=("carol",))
        self.ok(self.client("alice").patch("/api/projects/GONE", {"status": "archived"}))
        report = self.ok(self.client("vera").get("/api/reports/projection"))
        self.assertEqual([(p["project"], p["state"], p["verdict"]) for p in report["projects"]], [
            ("WEB", "projected", "will_miss"), ("OPS", "projected", "on_track"), ("LAB", "projected", "no_due_date"), ("OLD", "not_projected", "unknown"),
        ])  # fmt: skip  # archived projects are not listed
        web = report["projects"][0]
        self.assertEqual((web["finish"], web["due_date"], web["late_by_days"], web["late_tasks"], web["open_tasks"]), ("2026-10-23", "2026-10-20", 3, 1, 2))
        self.assertEqual(report["totals"], {"projected": 3, "will_miss": 1, "late_tasks": 1})
        self.assertEqual(report["as_of"], "2026-10-16")
        self.assertIn("Projected to finish Fri 23 Oct, 3 days after the due date (Tue 20 Oct).", web["headline"])

    def test_the_worse_the_slip_the_higher_the_project(self):
        self.make_project("OPS", members=("carol",), due_date="2026-10-16")
        self.task("Web", 16, "bob", "2026-10-20")  # day 2: Monday the 19th, a day early
        self.task("Ops", 24, "carol", "2026-10-16", key="OPS")  # due today, finishes Tuesday: 4 days late (Fri 16 -> Tue 20)
        self.assertEqual([p["project"] for p in self.ok(self.client("vera").get("/api/reports/projection"))["projects"]], ["OPS", "WEB"])


class AccessAndLimitsTests(ProjectionCase):
    def test_everyone_signed_in_can_read_and_nobody_else(self):
        self.task("Anything", 8, "bob", "2026-10-20")
        for user in ("admin", "alice", "bob", "carol", "vera"):
            self.assertEqual(self.client(user).get("/api/projects/WEB/projection").status, 200, user)
            self.assertEqual(self.client(user).get("/api/reports/projection").status, 200, user)
        for path in ("/api/projects/WEB/projection", "/api/reports/projection"):
            self.assertEqual(self.client().get(path).status, 401, path)

    def test_unknown_projects_are_404s(self):
        r = self.client("vera").get("/api/projects/NOPE/projection")
        self.assertEqual((r.status, r.error_code), (404, "project_not_found"))

    def test_asking_changes_nothing_in_the_database(self):
        self.task("Anything", 8, "bob", "2026-10-20")
        before = self.admin.get("/api/activity").json
        self.view()
        self.ok(self.client("vera").get("/api/reports/projection"))
        self.assertEqual(self.admin.get("/api/activity").json, before)

    def test_a_very_large_organisation_gets_a_clear_refusal_instead_of_a_slow_answer(self):
        self.task("One", 8, "bob", "2026-10-20")
        self.task("Two", 8, "bob", "2026-10-20")
        with mock.patch.object(projection, "MAX_TASKS", 1):
            r = self.client("vera").get("/api/projects/WEB/projection")
        self.assertEqual((r.status, r.error_code), (409, "projection_too_large"))

    def test_the_task_list_is_capped_but_the_late_ones_survive_the_cut(self):
        for n in range(6):
            self.task("Task %d" % n, 8, "bob", "2026-10-20")  # six days of work: the later ones are late
        with mock.patch.object(projection, "MAX_LISTED", 3):
            v = self.view()
        self.assertEqual((len(v["tasks"]), v["tasks_truncated"], v["counts"]["open_tasks"]), (3, True, 6))
        self.assertTrue(all(t["late_by_days"] for t in v["tasks"]))

    def test_the_assumptions_are_part_of_every_answer(self):
        a = self.view()["assumptions"]
        self.assertEqual((a["default_task_hours"], a["unassigned_hours_per_day"], a["horizon_work_days"]), (8.0, 8, 1300))
        self.assertEqual(len(a["notes"]), 5)
        self.assertEqual(self.ok(self.client("vera").get("/api/reports/projection"))["assumptions"], a)

    def test_a_very_long_chain_of_parents_waiting_for_parents_does_not_exhaust_the_stack(self):
        chain = 1200  # deeper than the interpreter's recursion limit of 1,000
        with self.app.session() as ctx:
            conn = ctx.conn
            project = conn.execute("SELECT id FROM projects WHERE key = 'WEB'").fetchone()[0]
            bob, number, previous = self.user_id("bob"), 0, None
            stamp = "2026-10-01T00:00:00Z"

            def insert(title, parent=None, hours=None, assignee=None):
                nonlocal number
                number += 1
                conn.execute(
                    "INSERT INTO tasks (project_id, number, title, status, priority, assignee_id, reporter_id, parent_id, estimate_hours, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'todo', 'medium', ?, ?, ?, ?, ?, ?)", (project, number, title, assignee, bob, parent, hours, stamp, stamp))
                return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            for i in range(chain):  # parent i has one subtask of one hour, and waits for parent i - 1
                parent = insert("Parent %d" % i)
                insert("Part %d" % i, parent, 1, bob)
                if previous:
                    conn.execute("INSERT INTO task_dependencies (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)", (previous, parent, stamp))
                previous = parent
            tail = insert("Tail", None, 1, bob)
            conn.execute("INSERT INTO task_dependencies (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)", (previous, tail, stamp))
        with mock.patch.object(projection, "MAX_LISTED", 5000):  # the tail finishes last, so the usual cut of 1,000 would drop it
            tail_entry = self.tasks()["WEB-%d" % number]
        # bob does 1,200 one-hour parts and then the tail: 1,201 hours at 8 a day = 150.125 working days, so the tail finishes on working day 150 (zero-based)
        day, n = date(2026, 10, 16), 150
        while n:
            day += timedelta(days=1)
            if day.weekday() < 5:
                n -= 1
        self.assertEqual(tail_entry["projected_finish"], day.isoformat())
        self.assertEqual(self.view()["counts"]["open_tasks"], chain + 1)  # the parents are containers: only the work itself is counted

    def test_work_that_is_never_going_to_finish_is_reported_as_beyond_the_horizon(self):
        self.task("Huge", 10000, "bob", "2026-10-20")  # 1,250 working days: just inside the horizon of 1,300
        self.task("Another huge one", 10000, "bob", "2026-10-20")  # would end at day 2,500
        self.task("After it", 8, "carol", "2026-10-20")
        self.block("WEB-3", "WEB-2")
        by_key = {t["key"]: t for t in self.view()["tasks"]}
        self.assertEqual(by_key["WEB-1"]["state"], "scheduled")
        self.assertEqual((by_key["WEB-2"]["state"], by_key["WEB-2"]["explanation"]), ("beyond_horizon", "Would not finish within 1300 working days."))
        self.assertEqual((by_key["WEB-3"]["state"], by_key["WEB-3"]["explanation"]), ("waiting_for_unschedulable", "Waits for WEB-2, which cannot be scheduled."))
        self.assertEqual(self.view()["counts"]["unprojectable"], 2)


class PlanningRulesTests(ProjectionCase):
    """Rules about *when* the clock starts and how exactly numbers are kept."""

    def test_the_cut_off_for_very_large_organisations_is_exact(self):
        self.task("One", 8, "bob", "2026-10-20")
        self.task("Two", 8, "bob", "2026-10-20")
        with mock.patch.object(projection, "MAX_TASKS", 2):
            self.assertEqual(self.client("vera").get("/api/projects/WEB/projection").status, 200)  # exactly as many as allowed
        with mock.patch.object(projection, "MAX_TASKS", 1):
            self.assertEqual(self.client("vera").get("/api/projects/WEB/projection").status, 409)

    def test_on_a_weekend_the_work_starts_on_monday(self):
        self.advance_days(1)  # Saturday 17 October
        self.task("Weekend plan", 8, "bob", "2026-10-19")
        v = self.view()
        self.assertEqual(v["as_of"], "2026-10-17")
        entry = v["tasks"][0]
        self.assertEqual((entry["projected_start"], entry["projected_finish"], entry["late_by_days"]), ("2026-10-19", "2026-10-19", 0))
        self.advance_days(1)  # Sunday: the same
        self.assertEqual(self.view()["tasks"][0]["projected_finish"], "2026-10-19")

    def test_a_task_due_today_that_cannot_make_it_is_late_but_not_yet_overdue(self):
        self.task("Due today, two days of work", 16, "bob", "2026-10-16")  # today is Friday the 16th
        self.task("Due yesterday", 4, "carol", "2026-10-15")
        tasks = self.tasks()
        self.assertEqual(tasks["WEB-1"]["explanation"], "Starts right away: Bob is free and nothing blocks it. The work itself (16 hours at 8 a day) runs past its due date.")
        self.assertFalse(tasks["WEB-1"]["overdue"])
        self.assertEqual(tasks["WEB-2"]["explanation"], "Starts right away: Carol is free and nothing blocks it. It is already overdue.")
        self.assertTrue(tasks["WEB-2"]["overdue"])

    def test_estimates_with_fractions_of_an_hour_are_kept_exactly(self):
        self.task("Quarter hour", 0.25, "bob", "2026-10-20")
        self.task("Odd one", 1.35, "bob", "2026-10-20")
        tasks = self.tasks()
        self.assertEqual((tasks["WEB-1"]["remaining_hours"], tasks["WEB-2"]["remaining_hours"]), (0.25, 1.35))


class LinkTypeTests(ProjectionCase):
    """Dependencies that carry a type (a later schema change adds the column) only delay work when they are finish-to-start."""

    def setUp(self):
        super().setUp()
        self.task("First", 16, "bob", "2026-10-19")  # WEB-1: day 0 -> 2
        self.task("Second", 8, "carol", "2026-10-19")  # WEB-2
        self.block("WEB-2", "WEB-1")

    def test_without_link_types_every_link_is_finish_to_start(self):
        self.assertEqual(self.finish_dates(), {"WEB-1": "2026-10-19", "WEB-2": "2026-10-20"})  # carol waits two days for bob

    def test_only_finish_to_start_links_delay_work_when_links_have_types(self):
        with self.app.session() as ctx:  # the column is added here, unless the schema already has it
            if not any(column[1] == "type" for column in ctx.conn.execute("PRAGMA table_info(task_dependencies)")):
                ctx.conn.execute("ALTER TABLE task_dependencies ADD COLUMN type TEXT NOT NULL DEFAULT 'finish_to_start'")
        self.assertEqual(self.finish_dates(), {"WEB-1": "2026-10-19", "WEB-2": "2026-10-20"})  # the default type is finish-to-start
        with self.app.session() as ctx:
            ctx.conn.execute("UPDATE task_dependencies SET type = 'start_to_start'")
        self.assertEqual(self.finish_dates(), {"WEB-1": "2026-10-19", "WEB-2": "2026-10-16"})  # carol may start at once
        self.assertEqual(self.tasks()["WEB-2"]["explanation"], "Starts right away: Carol is free and nothing blocks it.")
        with self.app.session() as ctx:
            ctx.conn.execute("UPDATE task_dependencies SET type = 'finish_to_finish'")
        self.assertEqual(self.finish_dates()["WEB-2"], "2026-10-16")


class ReassignAndAdviceCase(ProjectionCase):
    """bob has three tasks in a row and carol nothing. The project is due Wednesday the 21st; today is Friday the 16th (day 0).

    bob: WEB-1 24 h (day 0 -> 3, done Tuesday the 20th), WEB-2 8 h (3 -> 4, Wednesday the 21st), WEB-3 8 h (4 -> 5, Thursday the 22nd: a day late).
    """

    def setUp(self):
        super().setUp()
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("alice"), {"weekly_capacity_hours": 0}))  # alice (the project's owner) only manages
        self.ok(self.client("alice").patch("/api/projects/WEB", {"due_date": "2026-10-21"}))
        self.task("Big job", 24, "bob", "2026-10-20")
        self.task("Review", 8, "bob", "2026-10-21")
        self.task("Docs", 8, "bob", "2026-10-21")

    def advice(self, key="WEB", user="vera", **query):
        return self.ok(self.client(user).get("/api/projects/%s/projection/advice" % key, query=query))

    def deactivate(self, username):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id(username), {"active": False}))


class ReassignTests(ReassignAndAdviceCase):
    def test_the_starting_point(self):
        self.assertEqual(self.finish_dates(), {"WEB-1": "2026-10-20", "WEB-2": "2026-10-21", "WEB-3": "2026-10-22"})
        v = self.view()
        self.assertEqual((v["verdict"], v["late_by_days"], v["counts"]["late_tasks"]), ("will_miss", 1, 1))

    def test_giving_a_task_to_somebody_else_replays_the_whole_schedule(self):
        v = self.view(reassign="WEB-1:carol")
        tasks = {t["key"]: t for t in v["tasks"]}
        # carol does the big job (0 -> 3, Tuesday the 20th); bob does the review (0 -> 1, today) and the docs (1 -> 2, Monday the 19th)
        self.assertEqual({k: t["projected_finish"] for k, t in tasks.items()}, {"WEB-1": "2026-10-20", "WEB-2": "2026-10-16", "WEB-3": "2026-10-19"})
        self.assertEqual((v["finish"], v["late_by_days"], v["verdict"]), ("2026-10-20", 0, "on_track"))
        self.assertEqual(v["scenario"]["reassign"], [{"task": "WEB-1", "to": "carol", "to_name": "Carol"}])
        self.assertEqual((v["scenario"]["finish_moves_by_days"], v["scenario"]["extra_late_tasks"]), (-2, -1))
        self.assertEqual((tasks["WEB-1"]["assignee"], tasks["WEB-1"]["explanation"]), ("carol", "Starts right away: Carol is free and nothing blocks it."))
        self.assertIn("Given to Carol in this what-if", tasks["WEB-1"]["notes"])
        self.assertEqual({p["username"]: p["open_hours"] for p in v["people"]}, {"bob": 16.0, "carol": 24.0})

    def test_the_answer_names_the_new_assignee_in_the_explanations_of_other_tasks(self):
        self.task("Follow-up", 8, "bob", "2026-10-22", priority="low")  # WEB-4
        v = self.view(reassign="WEB-3:carol,WEB-4:carol")
        tasks = {t["key"]: t for t in v["tasks"]}
        self.assertEqual(tasks["WEB-4"]["assignee"], "carol")
        self.assertEqual(self.tasks(reassign="WEB-3:carol,WEB-4:carol")["WEB-4"]["explanation"], "Carol is busy with WEB-3 (Docs) until Fri 16 Oct, so this starts after it.")

    def test_the_tasks_in_a_what_if_are_listed_in_task_order_not_alphabetical_order(self):
        for n in range(4, 11):
            self.task("Filler %d" % n, 1, "bob", "2026-10-30")
        v = self.view(reassign="WEB-10:carol,WEB-2:carol,WEB-9:carol")
        self.assertEqual([m["task"] for m in v["scenario"]["reassign"]], ["WEB-2", "WEB-9", "WEB-10"])

    def test_several_tasks_can_be_given_away_at_once_and_the_portfolio_agrees(self):
        v = self.view(reassign="WEB-2:carol, WEB-3:carol")  # spaces are fine
        self.assertEqual([m["task"] for m in v["scenario"]["reassign"]], ["WEB-2", "WEB-3"])
        portfolio = self.ok(self.client("vera").get("/api/reports/projection", query={"reassign": "WEB-2:carol,WEB-3:carol"}))
        item = next(p for p in portfolio["projects"] if p["project"] == "WEB")
        self.assertEqual((item["finish"], item["baseline_finish"]), (v["finish"], "2026-10-22"))
        self.assertEqual(portfolio["scenario"]["reassign"], v["scenario"]["reassign"])

    def test_nothing_is_changed_by_asking(self):
        before = self.admin.get("/api/tasks/WEB-1").json
        self.view(reassign="WEB-1:carol")
        self.assertEqual(self.admin.get("/api/tasks/WEB-1").json, before)
        self.assertEqual(before["assignee"]["username"], "bob")

    def test_questions_that_cannot_be_answered_are_explained(self):
        self.make_project("OPS", members=("carol",))
        self.task("Parked", 8, "carol", None, key="OPS")
        self.ok(self.client("alice").patch("/api/projects/OPS", {"status": "on_hold"}))
        self.ok(self.client("alice").post("/api/projects/WEB/tasks", {"title": "Part", "parent": "WEB-2"}), 201)  # WEB-2 becomes a parent
        self.ok(self.set_status("WEB-3", "done"))
        for value, text in (
            ("WEB-1", "unknown or inactive user ''"), ("WEB-1:", "unknown or inactive user ''"), ("WEB-1:nobody", "unknown or inactive user 'nobody'"),
            ("NOPE-1:carol", "unknown task 'NOPE-1'"), ("WEB-99:carol", "unknown task 'WEB-99'"), ("nonsense:carol", "unknown task 'nonsense'"),
            ("WEB-1:vera", "vera is not a member of project WEB"), ("WEB-1:carol,WEB-1:bob", "WEB-1 is listed twice"),
            ("WEB-2:carol", "WEB-2 is a parent task: reassign one of its subtasks instead"),
            ("WEB-3:carol", "WEB-3 is not open work in a project that is being worked on"), ("OPS-1:carol", "OPS-1 is not open work in a project that is being worked on"),
        ):
            r = self.client("vera").get("/api/projects/WEB/projection", query={"reassign": value})
            self.assertEqual((r.status, list(r.json["error"]["details"])), (422, ["reassign"]), value)
            self.assertIn(text, r.json["error"]["details"]["reassign"], value)
        self.deactivate("carol")
        r = self.client("vera").get("/api/projects/WEB/projection", query={"reassign": "WEB-1:carol"})
        self.assertEqual(r.status, 422)

    def test_a_person_without_capacity_can_be_given_work_but_never_finishes_it(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"weekly_capacity_hours": 0}))
        v = self.view(reassign="WEB-1:carol")
        entry = next(t for t in v["tasks"] if t["key"] == "WEB-1")
        self.assertEqual(entry["state"], "no_capacity")
        self.assertEqual(v["verdict"], "unknown")


class AdviceTests(ReassignAndAdviceCase):
    def test_every_single_move_to_carol_would_fix_it_and_the_best_comes_first(self):
        a = self.advice()
        self.assertEqual((a["project"], a["as_of"], a["reason"], a["limited"]), ("WEB", "2026-10-16", None, False))
        self.assertEqual(a["tried"], 3)  # three tasks x one other member (carol)
        self.assertEqual([s["task"] for s in a["suggestions"]], ["WEB-1", "WEB-2", "WEB-3"])  # WEB-1 gets everything done soonest; the others tie, so by key
        first = a["suggestions"][0]
        self.assertEqual((first["from"]["username"], first["to"]["username"], first["reassign"]), ("bob", "carol", "WEB-1:carol"))
        self.assertEqual(first["before"], {"finish": "2026-10-22", "late_by_days": 1, "late_tasks": 1, "unprojectable": 0})
        self.assertEqual(first["after"], {"finish": "2026-10-20", "late_by_days": 0, "late_tasks": 0, "unprojectable": 0})
        self.assertEqual(first["summary"], "Give WEB-1 (Big job) to Carol instead of Bob: the project would finish Tue 20 Oct, 1 day before the due date, "
                                           "and no task would be late; now it finishes Thu 22 Oct, 1 day after the due date, and 1 task is late.")  # fmt: skip
        self.assertEqual(self.advice()["suggestions"][1]["after"]["finish"], "2026-10-21")
        self.assertEqual((first["to"]["open_hours"], first["to"]["busy_until"]), (0.0, None))  # carol has nothing else on

    def test_the_limit_picks_how_many_are_returned(self):
        self.assertEqual([s["task"] for s in self.advice(limit=1)["suggestions"]], ["WEB-1"])
        self.assertEqual(len(self.advice(limit=10)["suggestions"]), 3)
        for bad in ("0", "11", "-1", "many", "2.5"):
            r = self.client("vera").get("/api/projects/WEB/projection/advice", query={"limit": bad})
            self.assertEqual((r.status, list(r.json["error"]["details"])), (422, ["limit"]), bad)

    def test_a_suggestion_that_would_make_another_project_late_is_not_made(self):
        self.make_project("OPS", members=("carol", "bob"))
        self.task("Carol's job", 24, "carol", "2026-10-22", key="OPS")  # carol: day 0 -> 3 (Tuesday), due Thursday: fine for now
        a = self.advice()
        # WEB-1 (due the 20th) would go in front of OPS-1 in carol's queue: OPS-1 would end on Friday the 23rd, a day late. The other two are fine.
        self.assertEqual([s["task"] for s in a["suggestions"]], ["WEB-2", "WEB-3"])
        self.assertEqual(a["tried"], 3)
        after = self.finish_dates(reassign="WEB-2:carol", key="OPS")
        self.assertEqual(after["OPS-1"], "2026-10-21")  # delayed by the review, but still before its due date
        self.assertEqual((a["suggestions"][0]["to"]["open_hours"], a["suggestions"][0]["to"]["busy_until"]), (24.0, "2026-10-20"))  # carol's other work, and when it ends

    def test_a_move_that_would_push_somebody_elses_work_out_of_sight_is_not_made(self):
        for key in ("WEB-1", "WEB-2", "WEB-3"):
            self.ok(self.client("alice").patch("/api/tasks/" + key, {"status": "cancelled"}))
        self.task("Blocker", 400, "bob", "2026-10-20", priority="critical")  # WEB-4: 50 working days, and it goes first
        self.task("Small", 8, "bob", "2026-10-19")  # WEB-5: waits behind it
        self.make_project("OPS", members=("carol", "bob"))
        self.task("Own huge job", 10000, "carol", None, key="OPS", priority="high")  # 1,250 working days of carol's time
        self.task("Tail", 8, "carol", None, key="OPS", priority="low")  # OPS-2: ends on day 1,251 - just inside the horizon of 1,300
        self.assertEqual(self.view("OPS")["counts"]["unprojectable"], 0)
        self.assertEqual(self.view()["counts"]["late_tasks"], 2)
        a = self.advice(limit=10)
        # Giving the blocker to carol would leave only one task of WEB late, but it would go in front of carol's own work: her tail would end on
        # day 1,301 and could not be projected any more. So it is not suggested - and neither is anything else, as nothing else helps.
        self.assertEqual((a["suggestions"], a["reason"]), ([], "No single reassignment to another member of the project would help."))
        self.assertEqual((self.view("OPS", reassign="WEB-4:carol")["counts"]["unprojectable"], self.view(reassign="WEB-4:carol")["counts"]["late_tasks"]), (1, 1))

    def test_only_members_of_the_project_with_some_capacity_are_asked(self):
        self.make_user("dave", "member", weekly_capacity_hours=0)
        self.make_user("erin", "member")  # free, but not on the project
        self.ok(self.client("alice").post("/api/projects/WEB/members", {"username": "dave"}))
        a = self.advice(limit=10)
        # not dave (no capacity), not erin (not a member), not vera (a viewer), not alice (no capacity); only carol
        self.assertEqual(({s["to"]["username"] for s in a["suggestions"]}, a["tried"]), ({"carol"}, 3))

    def test_the_owner_of_the_project_is_a_member_like_any_other_when_they_have_capacity(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("alice"), {"weekly_capacity_hours": 40}))
        a = self.advice(limit=10)
        self.assertEqual(a["tried"], 6)  # three tasks x (alice, carol)
        self.assertEqual([(s["task"], s["to"]["username"]) for s in a["suggestions"]], [("WEB-1", "alice"), ("WEB-2", "alice"), ("WEB-3", "alice")])  # equally good: by name

    def test_with_time_for_one_try_the_work_that_cannot_be_scheduled_is_tried_first(self):
        self.make_user("dave", "member")
        self.ok(self.client("alice").post("/api/projects/WEB/members", {"username": "dave"}))
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"weekly_capacity_hours": 0}))
        self.ok(self.client("alice").patch("/api/tasks/WEB-3", {"assignee": "carol"}))  # carol has no capacity: her task cannot be scheduled
        self.ok(self.client("alice").patch("/api/tasks/WEB-1", {"due_date": "2026-10-19"}))  # and the big job is now late too: dave could take it as well
        self.assertEqual((self.view()["counts"]["unprojectable"], self.view()["counts"]["late_tasks"]), (1, 1))
        with mock.patch.object(projection, "MAX_TRIALS", 1):
            a = self.advice()
        self.assertEqual((a["tried"], a["limited"]), (1, True))
        self.assertEqual([(s["task"], s["to"]["username"]) for s in a["suggestions"]], [("WEB-3", "dave")])  # the one that cannot be scheduled goes first; dave is the freest

    def test_work_nobody_is_assigned_to_is_never_moved(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"weekly_capacity_hours": 50}))  # ten hours a day: faster than an unassigned task
        for key in ("WEB-1", "WEB-2", "WEB-3"):
            self.ok(self.client("alice").patch("/api/tasks/" + key, {"status": "cancelled"}))
        self.task("Nobody's", 20, None, "2026-10-19")  # two and a half days at the unassigned pace of 8: due Monday, done Tuesday
        self.assertEqual(self.view()["counts"]["late_tasks"], 1)
        a = self.advice()
        self.assertEqual((a["suggestions"], a["tried"]), ([], 0))
        self.assertEqual(a["reason"], "No single reassignment to another member of the project would help.")

    def test_work_nobody_is_assigned_to_is_not_moved_and_nothing_late_means_no_advice(self):
        self.ok(self.client("alice").patch("/api/projects/WEB", {"due_date": "2026-10-30"}))
        self.ok(self.client("alice").patch("/api/tasks/WEB-3", {"due_date": "2026-10-30"}))
        a = self.advice()
        self.assertEqual((a["suggestions"], a["tried"], a["reason"]), ([], 0, "Nothing is projected to be late."))

    def test_late_tasks_are_worth_fixing_even_when_the_project_as_a_whole_is_on_time(self):
        self.ok(self.client("alice").patch("/api/projects/WEB", {"due_date": "2026-12-01"}))
        v = self.view()
        self.assertEqual((v["verdict"], v["counts"]["late_tasks"]), ("on_track", 1))  # only the docs are late
        a = self.advice()
        self.assertEqual([s["task"] for s in a["suggestions"]], ["WEB-1", "WEB-2", "WEB-3"])
        self.assertEqual((a["suggestions"][0]["before"]["late_tasks"], a["suggestions"][0]["after"]["late_tasks"]), (1, 0))

    def test_a_project_that_is_not_being_worked_on_has_no_advice(self):
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "on_hold"}))
        a = self.advice()
        self.assertEqual((a["suggestions"], a["reason"]), ([], "Not projected: the project is on hold."))

    def test_when_moving_one_task_does_not_help_it_says_so(self):
        # one 24-hour task that is due after one working day cannot be rescued by giving it to carol, who is just as fast
        self.ok(self.client("alice").patch("/api/tasks/WEB-2", {"status": "cancelled"}))
        self.ok(self.client("alice").patch("/api/tasks/WEB-3", {"status": "cancelled"}))
        self.ok(self.client("alice").patch("/api/tasks/WEB-1", {"due_date": "2026-10-16"}))
        a = self.advice()
        self.assertEqual(a["suggestions"], [])
        self.assertEqual(a["reason"], "No single reassignment to another member of the project would help.")
        self.assertEqual(a["tried"], 1)

    def test_work_that_cannot_be_scheduled_is_fixed_by_giving_it_to_somebody_who_can(self):
        self.ok(self.admin.patch("/api/users/%d" % self.user_id("carol"), {"weekly_capacity_hours": 0}))
        self.ok(self.client("alice").patch("/api/tasks/WEB-3", {"assignee": "carol"}))
        a = self.advice(limit=1)
        [first] = a["suggestions"]
        self.assertEqual((first["task"], first["to"]["username"]), ("WEB-3", "bob"))  # the unschedulable task comes first, whatever else could be moved
        self.assertEqual((first["before"]["unprojectable"], first["after"]["unprojectable"]), (1, 0))
        self.assertEqual(first["summary"], "Give WEB-3 (Docs) to Bob instead of Carol: the project would finish Thu 22 Oct, 1 day after the due date, and 1 task would be late; "
                                           "now it finishes Wed 21 Oct, on the due date, no task is late and 1 task cannot be scheduled.")  # fmt: skip

    def test_the_search_is_capped_and_says_so(self):
        with mock.patch.object(projection, "MAX_TRIALS", 2):
            a = self.advice()
        self.assertEqual((a["tried"], a["limited"]), (2, True))
        # with time for two tries, the late task (WEB-3) goes first, then the biggest one (WEB-1); the review would have helped too
        self.assertEqual([s["task"] for s in a["suggestions"]], ["WEB-1", "WEB-3"])
        with mock.patch.object(projection, "WORK_BUDGET", 7):  # each try schedules bob's three tasks again: two fit, the third does not
            a = self.advice()
        self.assertEqual((a["tried"], a["limited"]), (2, True))
        with mock.patch.object(projection, "WORK_BUDGET", 2):  # not even one try fits
            a = self.advice()
        self.assertEqual((a["suggestions"], a["tried"], a["limited"]), ([], 0, True))
        self.assertEqual(a["reason"], "There are too many open tasks to try reassignments.")

    def test_it_is_read_only_and_for_everyone_who_can_read(self):
        before = self.admin.get("/api/activity").json
        for user in ("admin", "alice", "bob", "carol", "vera"):
            self.assertEqual(self.client(user).get("/api/projects/WEB/projection/advice").status, 200, user)
        self.assertEqual(self.client().get("/api/projects/WEB/projection/advice").status, 401)
        self.assertEqual(self.client("vera").get("/api/projects/NOPE/projection/advice").status, 404)
        self.assertEqual(self.admin.get("/api/activity").json, before)

    def test_the_advice_and_the_what_if_agree_on_every_suggestion(self):
        for s in self.advice(limit=10)["suggestions"]:
            v = self.view(reassign=s["reassign"])
            self.assertEqual((v["finish"], v["late_by_days"], v["counts"]["late_tasks"], v["counts"]["unprojectable"]),
                             (s["after"]["finish"], s["after"]["late_by_days"], s["after"]["late_tasks"], s["after"]["unprojectable"]), s["reassign"])  # fmt: skip


class AdviceOracleTests(ProjectionCase):
    """The advice is checked against a slower, independent route to the same answer.

    For each random organisation every reassignment of every task the late work waited for is asked about *through the public what-if
    option* (which loads everything from the database and schedules everything again), the results are ranked by the documented rules,
    and the advice - which re-schedules only the people and tasks that a move can affect - has to name exactly the same suggestions
    in the same order.
    """

    PEOPLE = ("alice", "bob", "carol", "dave")

    def setUp(self):
        super().setUp()
        self.make_user("dave", "member")

    def build(self, rng):
        for username in self.PEOPLE:
            self.ok(self.admin.patch("/api/users/%d" % self.user_id(username), {"weekly_capacity_hours": rng.choice((0, 20, 40, 40, 40))}))
        days = ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-28"]
        self.ok(self.client("alice").patch("/api/projects/WEB", {"due_date": rng.choice(days)}))
        self.make_project("OPS", members=tuple(rng.sample(("bob", "carol", "dave"), rng.randint(1, 3))), due_date=rng.choice(days))
        for key in ("WEB", "OPS"):
            for who in ("dave",):
                if rng.random() < 0.5:
                    self.client("alice").post("/api/projects/%s/members" % key, {"username": who})  # may already be a member: ignored
            members = [m["username"] for m in self.ok(self.client("alice").get("/api/projects/" + key))["members"]]
            made = []
            for n in range(rng.randint(2, 7)):
                fields = {"estimate_hours": rng.choice((2, 4, 8, 12, 16, 24)), "priority": rng.choice(("low", "medium", "medium", "high", "critical"))}
                if rng.random() < 0.85:
                    fields["assignee"] = rng.choice(members)
                if rng.random() < 0.8:
                    fields["due_date"] = rng.choice(days)
                if fields.get("due_date") and rng.random() < 0.2:
                    fields["start_date"] = rng.choice([d for d in days if d <= fields["due_date"]])
                made.append(self.make_task(key, "%s task %d" % (key, n), **fields)["key"])
            for later in range(1, len(made)):
                if rng.random() < 0.3:
                    r = self.client("alice").post("/api/tasks/%s/dependencies" % made[later], {"blocked_by": made[rng.randrange(later)]})
                    self.assertEqual(r.status, 200, r.text)
            if rng.random() < 0.15:
                self.ok(self.set_status(made[0], "done"))

    def standing(self, **query):
        v = self.view("WEB", **query)
        report = self.ok(self.client("vera").get("/api/reports/projection", query=query))
        others = [p for p in report["projects"] if p["project"] != "WEB"]
        finish, due = v["finish"], v["due_date"]
        days_late = max((date.fromisoformat(finish) - date.fromisoformat(due)).days, 0) if finish and due else 0  # also while a task cannot be scheduled
        return {
            "score": (v["counts"]["unprojectable"], days_late, v["counts"]["late_tasks"], date.fromisoformat(finish).toordinal() if finish else 0),
            "others": (sum(p["late_tasks"] for p in others), sum(p["unprojectable"] for p in others)),
            "view": v,
        }

    def critical(self, v):
        """The tasks of WEB that the late work waited for, read off the public answers: from each late task (and from the tasks that end a
        late project, and from those that cannot be scheduled) follow "it waited for ..." through any project. Moving any other task cannot
        help; it may still change the plan by a quirk (giving somebody something to do makes them skip a long task they would have started
        just before a more important one became ready), but such a move cannot be explained, so it is not suggested."""
        entries = {}
        for key in ("WEB", "OPS"):
            entries.update({t["key"]: t for t in self.view(key)["tasks"]})
        project_late = bool(v["finish"] and v["due_date"] and v["finish"] > v["due_date"])
        todo = [t["key"] for t in v["tasks"] if t["state"] != "scheduled" or t["late_by_days"] or (project_late and t["projected_finish"] == v["finish"])]
        seen = set()
        while todo:
            key = todo.pop()
            if key in seen or key not in entries:
                continue
            seen.add(key)
            why = entries[key]["why"]
            if why and why["kind"] in ("queue", "blocker"):
                todo.append(why["task"])
        return {t["key"] for t in v["tasks"] if t["key"] in seen and t["assignee"] is not None}

    def expected(self, limit):
        v = self.view("WEB")
        if v["state"] != "projected":
            return []
        before = self.standing()
        if before["score"][:3] == (0, 0, 0):
            return []
        members = {m["username"]: m for m in self.ok(self.client("alice").get("/api/projects/WEB"))["members"]}
        usable = [u for u in members if self.capacity_of(u) > 0]
        best, movable = {}, self.critical(v)
        for t in v["tasks"]:
            if t["assignee"] is None or t["key"] not in movable:
                continue
            for username in usable:
                if username == t["assignee"]:
                    continue
                after = self.standing(reassign="%s:%s" % (t["key"], username))
                if after["score"][:3] < before["score"][:3] and after["others"][0] <= before["others"][0] and after["others"][1] <= before["others"][1]:
                    candidate = (after["score"], username)
                    if t["key"] not in best or candidate < best[t["key"]]:
                        best[t["key"]] = candidate
        ranked = sorted(best.items(), key=lambda kv: (kv[1][0], kv[0]))
        return [(key, username) for key, (_, username) in ranked[:limit]]

    def capacity_of(self, username):
        return next(u for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == username)["weekly_capacity_hours"]

    def test_the_advice_matches_the_slow_route_on_random_organisations(self):
        import random

        helped = 0
        for seed in range(30):
            with self.subTest(seed=seed):
                self.tearDown_everything()
                rng = random.Random(seed)
                self.build(rng)
                advice = self.advice_for_oracle()
                want = self.expected(10)
                self.assertEqual([(s["task"], s["to"]["username"]) for s in advice["suggestions"]], want)
                for s in advice["suggestions"]:  # what the advice promises is what asking again with that assumption shows
                    v = self.standing(reassign=s["reassign"])["view"]
                    self.assertEqual(s["after"], {"finish": v["finish"], "late_by_days": v["late_by_days"], "late_tasks": v["counts"]["late_tasks"], "unprojectable": v["counts"]["unprojectable"]})
                    self.assertEqual(s["before"]["late_tasks"], self.view("WEB")["counts"]["late_tasks"])
                helped += bool(want)
        self.assertGreaterEqual(helped, 5)  # the generator really produces organisations where moving a task helps

    def advice_for_oracle(self):
        return self.ok(self.client("vera").get("/api/projects/WEB/projection/advice", query={"limit": 10}))

    def tearDown_everything(self):
        """Start every random organisation from the same empty state (users stay, projects and tasks go)."""
        with self.app.session() as ctx:
            for table in ("task_dependencies", "time_entries", "comments", "tasks", "milestones", "project_members", "projects"):
                try:
                    ctx.conn.execute("DELETE FROM %s" % table)
                except Exception:  # a table that does not exist in this schema
                    pass
        self.make_project("WEB", members=("bob", "carol"), due_date="2026-10-20")


class ConsistencyTests(ProjectionCase):
    def test_the_portfolio_agrees_with_each_project_view(self):
        self.make_project("OPS", members=("carol", "bob"), due_date="2026-11-06")
        self.task("A", 20, "bob", "2026-10-20")
        self.task("B", 12, "carol", "2026-10-22", priority="high")
        self.task("C", 30, "bob", "2026-11-03", key="OPS")
        self.task("D", 6, "carol", None, key="OPS")
        self.block("OPS-2", "OPS-1")
        report = {p["project"]: p for p in self.ok(self.client("vera").get("/api/reports/projection"))["projects"]}
        for key in ("WEB", "OPS"):
            v = self.view(key)
            p = report[key]
            self.assertEqual((p["finish"], p["late_by_days"], p["verdict"], p["headline"]), (v["finish"], v["late_by_days"], v["verdict"], v["headline"]))
            self.assertEqual((p["open_tasks"], p["late_tasks"]), (v["counts"]["open_tasks"], v["counts"]["late_tasks"]))

    def test_a_project_view_is_the_same_whichever_project_asks_first(self):
        self.make_project("OPS", members=("carol", "bob"))
        self.task("A", 20, "bob", "2026-10-20")
        self.task("C", 30, "bob", "2026-11-03", key="OPS")
        first = (self.view("WEB")["tasks"], self.view("OPS")["tasks"])
        second = (self.view("WEB")["tasks"], self.view("OPS")["tasks"])
        self.assertEqual(first, second)  # deterministic: no randomness, no dependence on call order


if __name__ == "__main__":
    unittest.main()
