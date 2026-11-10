"""Exporting a project as Microsoft Project (MSPDI) XML: the structure, the schema order, the dates and the hostile text."""

import random
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta

from msproject_helpers import MspCase, child, local, text
from msproject_schema import order_problems

from zylker_pm.services import msproject as msp
from zylker_pm.services.msproject import _add_workdays, _finish_after, _work_minutes, _workdays_between, format_duration, parse_duration_hours


def tasks_of(root):
    return {text(t, "Name"): t for t in child(root, "Tasks") if text(t, "UID") != "0"}


def links_of(task):
    return [(text(link, "PredecessorUID"), text(link, "Type"), text(link, "LinkLag")) for link in task if local(link.tag) == "PredecessorLink"]


class DurationTests(unittest.TestCase):
    def test_durations_are_written_as_hours_minutes_and_seconds(self):
        self.assertEqual([format_duration(h) for h in (0, 8, 0.5, 80, 0.01, 1.37, 10000)],
                         ["PT0H0M0S", "PT8H0M0S", "PT0H30M0S", "PT80H0M0S", "PT0H0M36S", "PT1H22M12S", "PT10000H0M0S"])  # fmt: skip

    def test_durations_are_read_back_exactly(self):
        for hours in (0, 0.01, 0.25, 1.37, 8, 41.5, 10000):
            self.assertAlmostEqual(parse_duration_hours(format_duration(hours)), hours, places=9)

    def test_other_writers_forms_are_understood_and_rubbish_is_not(self):
        self.assertEqual([parse_duration_hours(t) for t in ("PT8H0M0S", "PT80H30M", "PT0S", "PT1H30M15S", "P1DT2H", "PT36S", " PT2H ")], [8, 80.5, 0, 1 + 0.5 + 15 / 3600, 26, 0.01, 2])
        for rubbish in ("", None, "P", "PT", "8h", "-PT1H", "PT1.5H", "P1Y", "soon", "PT8H junk"):
            self.assertIsNone(parse_duration_hours(rubbish), rubbish)


class WorkingTimeTests(unittest.TestCase):
    MON, FRI = date(2026, 11, 2), date(2026, 11, 6)

    def at(self, day, hours, minutes=0):
        return datetime.combine(day, time(hours, minutes))

    def test_work_ends_where_a_day_on_the_standard_calendar_says(self):
        cases = {0: self.at(self.MON, 8), 60: self.at(self.MON, 9), 240: self.at(self.MON, 12), 241: self.at(self.MON, 13, 1), 300: self.at(self.MON, 14),
                 480: self.at(self.MON, 17), 481: self.at(date(2026, 11, 3), 8, 1), 960: self.at(date(2026, 11, 3), 17), 2400: self.at(self.FRI, 17), 2401: self.at(date(2026, 11, 9), 8, 1)}  # fmt: skip
        for minutes, finish in cases.items():
            self.assertEqual(_finish_after(self.MON, minutes), finish, minutes)

    def test_weekends_are_skipped(self):
        self.assertEqual(_finish_after(self.FRI, 480), self.at(self.FRI, 17))
        self.assertEqual(_finish_after(self.FRI, 960), self.at(date(2026, 11, 9), 17))
        self.assertEqual(_add_workdays(self.FRI, 1), date(2026, 11, 9))
        self.assertEqual(_add_workdays(date(2026, 11, 9), -1), self.FRI)
        self.assertEqual(_add_workdays(self.MON, 10), date(2026, 11, 16))
        self.assertEqual(_add_workdays(self.MON, -5), date(2026, 10, 26))
        self.assertEqual(_workdays_between(self.MON, date(2026, 11, 9)), 5)
        self.assertEqual(_workdays_between(self.FRI, date(2026, 11, 9)), 1)
        self.assertEqual(_workdays_between(date(2026, 11, 7), date(2026, 11, 9)), 0)

    def test_the_minutes_between_two_moments_are_what_they_were_built_from(self):
        rng = random.Random(7)
        for _ in range(300):
            start = self.at(date(2026, 1, 1) + timedelta(days=rng.randrange(400)), 8)
            if start.weekday() >= 5:
                continue
            minutes = rng.randrange(0, 20000)
            self.assertEqual(_work_minutes(start, _finish_after(start.date(), minutes)), minutes, (start, minutes))


class StructureTests(MspCase):
    def setUp(self):
        super().setUp()
        a = self.alice
        self.epic = self.make_task("IMP", "Epic <one> & \"two\"", type="epic", description="Top level\nnotes", priority="critical")
        self.design = self.make_task("IMP", "Design", parent="IMP-1", assignee="bob", estimate_hours=16, start_date="2026-11-02", due_date="2026-11-05", priority="high", status="done")
        self.build = self.make_task("IMP", "Build", parent="IMP-1", assignee="carol", estimate_hours=40, due_date="2026-11-20", status="in_progress", priority="low")
        self.review = self.make_task("IMP", "Review", estimate_hours=2.5, status="in_review")
        self.blocked = self.make_task("IMP", "Blocked one", status="blocked")
        self.dropped = self.make_task("IMP", "Dropped", status="cancelled")
        self.plain = self.make_task("IMP", "No estimate, no dates")
        self.gate = self.ok(a.post("/api/projects/IMP/milestones", {"name": "Beta", "due_date": "2026-11-23", "description": "Usable"}), 201)
        self.ok(a.patch("/api/tasks/IMP-3", {"milestone": self.gate["id"]}))
        self.ok(a.post("/api/tasks/IMP-3/dependencies", {"blocked_by": "IMP-2", "type": "finish_to_start", "lag_days": 2}))
        self.ok(a.post("/api/tasks/IMP-4/dependencies", {"blocked_by": "IMP-3", "type": "start_to_start", "lag_days": -1}))
        self.ok(a.post("/api/tasks/IMP-5/dependencies", {"blocked_by": "IMP-3", "type": "finish_to_finish"}))
        self.root = self.exported()
        self.t = tasks_of(self.root)

    def test_the_response_is_a_downloadable_xml_file(self):
        r = self.export()
        self.assertEqual((r.status, r.headers["Content-Type"], r.headers["Content-Disposition"]), (200, "application/xml; charset=utf-8", 'attachment; filename="IMP-msproject.xml"'))
        self.assertTrue(r.body.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Project xmlns="http://schemas.microsoft.com/project">'))
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.export().body, r.body)  # the same project twice gives the same bytes

    def test_the_elements_are_in_the_order_the_schema_demands(self):
        self.assertEqual(order_problems(self.root), [])

    def test_the_project_is_described_for_project(self):
        root = self.root
        self.assertEqual([text(root, n) for n in ("SaveVersion", "Name", "Title", "Manager", "Author", "MinutesPerDay", "MinutesPerWeek", "CalendarUID")],
                         ["14", "Project IMP", "Project IMP", "Alice", "Zylker Projects", "480", "2400", "1"])  # fmt: skip
        self.assertEqual(text(root, "UID"), msp.project_guid("IMP"))
        self.assertEqual(msp.project_guid("imp"), msp.project_guid("IMP"))
        self.assertNotEqual(msp.project_guid("OTHER"), msp.project_guid("IMP"))
        calendar = child(child(root, "Calendars"), "Calendar")
        days = {text(d, "DayType"): text(d, "DayWorking") for d in child(calendar, "WeekDays")}
        self.assertEqual(days, {"1": "0", "2": "1", "3": "1", "4": "1", "5": "1", "6": "1", "7": "0"})
        monday = next(d for d in child(calendar, "WeekDays") if text(d, "DayType") == "2")
        self.assertEqual([text(w, "FromTime") + "-" + text(w, "ToTime") for w in child(monday, "WorkingTimes")], ["08:00:00-12:00:00", "13:00:00-17:00:00"])

    def test_row_zero_is_the_project_and_the_outline_follows_the_hierarchy(self):
        rows = list(child(self.root, "Tasks"))
        self.assertEqual((text(rows[0], "UID"), text(rows[0], "OutlineLevel"), text(rows[0], "Summary")), ("0", "0", "1"))
        names = [text(r, "Name") for r in rows[1:]]
        self.assertEqual(names, ["Epic <one> & \"two\"", "Design", "Build", "Review", "Blocked one", "Dropped", "No estimate, no dates", "Beta"])
        levels = {text(r, "Name"): (text(r, "OutlineLevel"), text(r, "WBS"), text(r, "Summary"), text(r, "UID"), text(r, "ID")) for r in rows[1:]}
        self.assertEqual(levels["Epic <one> & \"two\""], ("1", "1", "1", "1", "1"))
        self.assertEqual(levels["Design"], ("2", "1.1", "0", "2", "2"))
        self.assertEqual(levels["Build"], ("2", "1.2", "0", "3", "3"))
        self.assertEqual(levels["Review"], ("1", "2", "0", "4", "4"))
        self.assertEqual(levels["Beta"], ("1", "6", "0", "8", "8"))  # milestones come last; UIDs continue after the task numbers

    def test_a_task_carries_its_own_fields(self):
        design = self.t["Design"]
        self.assertEqual([text(design, n) for n in ("Priority", "Duration", "Work", "Estimated", "PercentComplete", "Active", "Milestone")], ["750", "PT16H0M0S", "PT16H0M0S", "0", "100", "1", "0"])
        self.assertEqual([text(design, n) for n in ("Start", "Finish", "ConstraintType", "ConstraintDate", "Deadline")],
                         ["2026-11-02T08:00:00", "2026-11-03T17:00:00", "4", "2026-11-02T08:00:00", "2026-11-05T17:00:00"])  # fmt: skip
        self.assertEqual(text(self.t["Epic <one> & \"two\""], "Notes"), "Top level\nnotes")
        self.assertEqual(text(self.t["Epic <one> & \"two\""], "Priority"), "950")
        self.assertEqual(text(self.t["Build"], "Priority"), "250")
        self.assertIsNone(child(self.t["Review"], "Notes"))

    def test_status_becomes_completion_and_cancelled_tasks_are_inactive(self):
        percent = {name: (text(t, "PercentComplete"), text(t, "Active")) for name, t in self.t.items()}
        self.assertEqual(percent["Design"], ("100", "1"))
        self.assertEqual(percent["Build"], ("50", "1"))
        self.assertEqual(percent["Review"], ("80", "1"))
        self.assertEqual(percent["Blocked one"], ("0", "1"))
        self.assertEqual(percent["Dropped"], ("0", "0"))
        self.assertEqual(percent["No estimate, no dates"], ("0", "1"))

    def test_a_task_without_an_estimate_is_one_estimated_day_with_no_work(self):
        plain = self.t["No estimate, no dates"]
        self.assertEqual([text(plain, n) for n in ("Duration", "Work", "Estimated")], ["PT8H0M0S", "PT0H0M0S", "1"])
        self.assertIsNone(child(plain, "ConstraintType"))
        self.assertIsNone(child(plain, "Deadline"))

    def test_dates_start_where_set_or_end_at_the_due_date_and_summaries_span_their_subtasks(self):
        build = self.t["Build"]  # 40 h, due Friday the 20th, no start date: five working days back
        self.assertEqual((text(build, "Start"), text(build, "Finish"), text(build, "Deadline")), ("2026-11-16T08:00:00", "2026-11-20T17:00:00", "2026-11-20T17:00:00"))
        self.assertIsNone(child(build, "ConstraintType"))
        epic = self.t["Epic <one> & \"two\""]
        self.assertEqual((text(epic, "Start"), text(epic, "Finish")), ("2026-11-02T08:00:00", "2026-11-20T17:00:00"))
        self.assertEqual(text(epic, "Duration"), format_duration(_work_minutes(datetime(2026, 11, 2, 8), datetime(2026, 11, 20, 17)) / 60))
        self.assertIsNone(child(epic, "Work"))
        self.assertEqual(text(epic, "PercentComplete"), "%d" % round((100 * 16 + 50 * 40) / 56))  # weighted by duration

    def test_dependencies_are_written_on_the_successor_with_their_type_and_lag(self):
        day = 480 * 10
        self.assertEqual(links_of(self.t["Build"]), [("2", "1", str(2 * day))])
        self.assertEqual(links_of(self.t["Review"]), [("3", "3", str(-day))])
        self.assertEqual(links_of(self.t["Blocked one"]), [("3", "0", "0")])
        link = child(self.t["Build"], "PredecessorLink")
        self.assertEqual([text(link, n) for n in ("CrossProject", "LagFormat")], ["0", "7"])
        self.assertEqual(links_of(self.t["Design"]), [])

    def test_a_milestone_is_a_zero_length_task_that_its_tasks_lead_into(self):
        beta = self.t["Beta"]
        self.assertEqual([text(beta, n) for n in ("Milestone", "Summary", "Duration", "Start", "Finish", "ConstraintType", "ConstraintDate", "Deadline", "Notes")],
                         ["1", "0", "PT0H0M0S", "2026-11-23T17:00:00", "2026-11-23T17:00:00", "6", "2026-11-23T17:00:00", "2026-11-23T17:00:00", "Usable"])  # fmt: skip
        self.assertEqual(links_of(beta), [("3", "1", "0")])
        self.assertIsNone(child(beta, "Work"))

    def test_people_become_resources_and_their_tasks_become_assignments(self):
        resources = {text(r, "Name"): r for r in child(self.root, "Resources")}
        self.assertEqual(sorted(resources), ["Alice", "Bob", "Carol"])  # the manager who created the project is a member too
        self.assertEqual([text(resources["Bob"], n) for n in ("Type", "MaxUnits", "IsNull")], ["1", "1.00", "0"])
        by_uid = {text(r, "UID"): text(r, "Name") for r in child(self.root, "Resources")}
        assignments = [(text(a, "TaskUID"), by_uid[text(a, "ResourceUID")], text(a, "Units"), text(a, "Work")) for a in child(self.root, "Assignments")]
        self.assertEqual(sorted(assignments), [("2", "Bob", "1", "PT16H0M0S"), ("3", "Carol", "1", "PT40H0M0S")])

    def test_capacity_and_e_mail_travel_with_the_resource(self):
        self.make_user("dana", "member", email="dana@example.com", weekly_capacity_hours=20)
        self.ok(self.alice.post("/api/projects/IMP/members", {"username": "dana"}))
        dana = next(r for r in child(self.exported(), "Resources") if text(r, "Name") == "Dana")
        self.assertEqual([text(dana, "EmailAddress"), text(dana, "MaxUnits")], ["dana@example.com", "0.50"])

    def test_a_project_with_nothing_in_it_is_still_a_valid_file(self):
        self.make_project("EMPTY", members=())
        root = self.exported("EMPTY")
        self.assertEqual(order_problems(root), [])
        self.assertEqual([text(t, "UID") for t in child(root, "Tasks")], ["0"])
        self.assertEqual(self.run_import(self.export("EMPTY").body, dry_run="true")["warnings"], [{"uid": None, "message": "the file contains no tasks"}])

    def test_everyone_who_can_read_the_project_can_export_it_and_nobody_else(self):
        self.assertEqual(self.export(user="vera").status, 200)
        self.assertEqual(self.export(user="bob").status, 200)
        self.assertEqual(self.client(token="nope").get("/api/projects/IMP/export/msproject").status, 401)
        self.assertEqual(self.client().get("/api/projects/IMP/export/msproject", token=None).status, 401)
        self.assertEqual(self.export("NOPE").status, 404)

    def test_the_filename_is_safe_whatever_the_key(self):
        self.make_project("AB1", members=())
        self.assertEqual(self.export("AB1").headers["Content-Disposition"], 'attachment; filename="AB1-msproject.xml"')


class HostileTextTests(MspCase):
    NASTY = ["</Name><Evil>1</Evil>", "]]>", "<!-- x -->", "&lt;&amp;", "\x00\x01\x1b control \x7f", "\ud83d\ude00 emoji and \u202e bidi", "line one\nline two\r\nline three", "quote \" and ' apostrophe", "<?xml version='1.0'?>", "a" * 400]

    def test_the_text_of_tasks_and_people_cannot_break_out_of_its_element(self):
        for title in self.NASTY:
            self.make_task("IMP", title[:200] or "x", description=title)
        self.make_user("zed", "member", display_name="Zed </Name><X/>")
        self.ok(self.alice.post("/api/projects/IMP/members", {"username": "zed"}))
        r = self.export()
        root = ET.fromstring(r.body)  # still well formed
        self.assertEqual(order_problems(root), [])
        self.assertEqual(len(list(child(root, "Tasks"))), 1 + len(self.NASTY))
        self.assertNotIn(b"<Evil>", r.body)
        self.assertNotIn(b"\x00", r.body)
        names = [text(t, "Name") for t in child(root, "Tasks")][1:]
        self.assertEqual(names[0], "</Name><Evil>1</Evil>")
        self.assertTrue(all(len(n) <= 255 for n in names))
        self.assertTrue(all(ch in "\t\n\r" or ord(ch) >= 0x20 for n in names for ch in n))  # nothing XML 1.0 forbids

    def test_what_comes_out_goes_back_in_as_the_same_text(self):
        for title in self.NASTY[:9]:
            self.make_task("IMP", title, description=title)
        self.make_project("BACK", members=())
        self.run_import(self.export().body, key="BACK")
        original = {t["title"]: t["description"] for t in self.tasks()}
        again = {t["title"]: t["description"] for t in self.tasks("BACK")}
        clean = lambda s: " ".join("".join(c for c in s if c in "\t\n\r" or 32 <= ord(c) != 127).split())  # noqa: E731
        self.assertEqual(sorted(again), sorted(clean(t) for t in original))


class BigProjectTests(MspCase):
    def test_a_few_thousand_tasks_export_and_import_quickly(self):
        import time as clock

        from zylker_pm.db import connect

        conn = connect(self.config.db_path)
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute("SELECT id FROM projects WHERE key = 'IMP'").fetchone()[0]
        for n in range(1, 3001):
            conn.execute("INSERT INTO tasks (project_id, number, title, reporter_id, parent_id, created_at, updated_at, estimate_hours) VALUES (?,?,?,2,?,?,?,?)",
                         (project, n, "Task %d" % n, None if n % 10 == 1 else conn.execute("SELECT id FROM tasks WHERE project_id = ? AND number = ?", (project, n - 1 - (n - 1) % 10 + 1)).fetchone()[0], "2026-10-16T09:00:00Z", "2026-10-16T09:00:00Z", n % 40))  # fmt: skip
        conn.execute("UPDATE projects SET next_task_number = 3001 WHERE id = ?", (project,))
        conn.execute("COMMIT")
        conn.close()
        self.make_project("COPY", members=())
        started = clock.time()
        body = self.export().body
        report = self.run_import(body, key="COPY")
        self.assertEqual((report["created"], report["errors"]), (3000, []))
        self.assertLess(clock.time() - started, 20)  # well under a second on a laptop; the bound is only there to catch a quadratic loop
