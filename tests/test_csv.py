"""CSV export (formula-injection safe) and tolerant, idempotent CSV import."""

import csv
import io
from unittest import mock

from helpers import ApiTestCase

from zylker_pm.services import csvio

# Fictional rows in the style of a Jira export: repeated "Labels" columns, month-name dates.
JIRA_STYLE = (
    "Issue key,Summary,Issue Type,Status,Priority,Assignee,Reporter,Due Date,Labels,Labels,Description\n"
    "DEMO-1,Fix login,Bug,In Progress,High,bob,alice,30-Jul-26,auth,backend,Steps to reproduce\n"
    "DEMO-2,Write docs,Story,To Do,Lowest,Carol,,2026-12-01,,,\n"
    "DEMO-3,Ship it,Task,Done,Highest,nobody@example.com,alice,30/Jul/2026 12:00 AM,,,\n"
)


class ImportBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("IMP", members=("bob", "carol"))
        self.alice = self.client("alice")

    def do_import(self, text, user="alice", **query):
        return self.client(user).request("POST", "/api/projects/IMP/import", raw=text.encode("utf-8"), content_type="text/csv", query=query)

    def tasks(self):
        return self.ok(self.alice.get("/api/tasks", query={"project": "IMP", "limit": 200}))["items"]


class ImportTests(ImportBase):
    def test_jira_style_file_is_mapped_field_by_field(self):
        report = self.ok(self.do_import(JIRA_STYLE))
        self.assertEqual((report["created"], report["skipped_duplicates"], report["errors"]), (3, 0, []))
        self.assertEqual(report["keys"], ["IMP-1", "IMP-2", "IMP-3"])
        t1, t2, t3 = self.tasks()
        self.assertEqual((t1["type"], t1["status"], t1["priority"]), ("bug", "in_progress", "high"))
        self.assertEqual((t1["assignee"]["username"], t1["reporter"]["username"], t1["due_date"]), ("bob", "alice", "2026-07-30"))
        self.assertEqual((t1["labels"], t1["external_ref"], t1["description"]), (["auth", "backend"], "DEMO-1", "Steps to reproduce"))
        self.assertTrue(t1["is_overdue"])
        self.assertEqual((t2["type"], t2["priority"], t2["assignee"]["username"], t2["due_date"]), ("story", "low", "carol", "2026-12-01"))
        self.assertEqual(t2["reporter"]["username"], "alice")  # empty reporter -> the importing user
        self.assertEqual((t3["status"], t3["priority"], t3["due_date"]), ("done", "critical", "2026-07-30"))
        self.assertIsNotNone(t3["completed_at"])
        self.assertTrue(any("nobody@example.com" in w["message"] for w in report["warnings"]))
        self.assertIsNone(t3["assignee"])

    def test_importing_the_same_file_twice_creates_nothing_new(self):
        self.ok(self.do_import(JIRA_STYLE))
        again = self.ok(self.do_import(JIRA_STYLE))
        self.assertEqual((again["created"], again["skipped_duplicates"]), (0, 3))
        self.assertEqual(len(self.tasks()), 3)

    def test_duplicate_refs_within_one_file_are_skipped(self):
        report = self.ok(self.do_import("key,summary\nX-1,one\nX-1,again\nX-2,two\n"))
        self.assertEqual((report["created"], report["skipped_duplicates"]), (2, 1))

    def test_dry_run_reports_everything_but_writes_nothing(self):
        dry = self.ok(self.do_import(JIRA_STYLE, dry_run="true"))
        self.assertEqual((dry["dry_run"], dry["created"]), (True, 3))
        self.assertEqual(self.tasks(), [])
        real = self.ok(self.do_import(JIRA_STYLE))
        self.assertEqual(real["keys"], ["IMP-1", "IMP-2", "IMP-3"])  # the dry run did not burn task numbers
        acts = [a["action"] for a in self.ok(self.alice.get("/api/projects/IMP/activity"))["items"]]
        self.assertEqual(acts.count("import.completed"), 1)

    def test_bad_rows_are_reported_with_row_numbers_and_good_rows_still_import(self):
        text = "summary,start date,due date,estimate\nGood,2026-10-01,2026-10-09,3\n,2026-10-01,,\nBackwards,2026-10-09,2026-10-01,\nOdd date,whenever,,x\n"
        report = self.ok(self.do_import(text))
        self.assertEqual(report["created"], 2)  # "Good" and "Odd date"
        self.assertEqual([e["row"] for e in report["errors"]], [3, 4])
        self.assertIn("title", report["errors"][0]["message"])
        self.assertIn("due_date", report["errors"][1]["message"])
        warnings = " | ".join(w["message"] for w in report["warnings"])
        self.assertIn("unrecognised start_date 'whenever' ignored", warnings)
        self.assertIn("invalid estimate 'x' ignored", warnings)
        self.assertEqual([t["title"] for t in self.tasks()], ["Good", "Odd date"])

    def test_unknown_enum_values_fall_back_with_a_warning(self):
        report = self.ok(self.do_import("summary,status,priority,type\nA,Waiting for QA,Meh,Chore\nB,,,\n"))
        a, b = self.tasks()
        self.assertEqual((a["status"], a["priority"], a["type"]), ("todo", "medium", "task"))
        self.assertEqual(len(report["warnings"]), 3)
        self.assertEqual((b["status"], b["priority"]), ("todo", "medium"))  # blanks are silently defaulted

    def test_non_members_are_left_unassigned_unless_requested(self):
        self.make_user("zed", "member", display_name="Zed Zimmer")
        csv_text = "summary,assignee\nT,zed\n"
        report = self.ok(self.do_import(csv_text))
        self.assertIn("not a project member", report["warnings"][0]["message"])
        self.assertIsNone(self.tasks()[0]["assignee"])
        self.ok(self.do_import("summary,assignee\nT2,zed\n", add_missing_members="true"))
        self.assertEqual(self.tasks()[1]["assignee"]["username"], "zed")
        self.assertIn("zed", [m["username"] for m in self.ok(self.alice.get("/api/projects/IMP"))["members"]])

    def test_structural_problems_are_rejected_up_front(self):
        self.assertEqual(self.do_import("").status, 422)
        self.assertEqual(self.do_import("foo,bar\n1,2\n").status, 422)
        r = self.alice.request("POST", "/api/projects/IMP/import", raw=b"{}", content_type="application/json")
        self.assertEqual(r.status, 415)

    def test_only_managers_can_import(self):
        for user in ("bob", "vera"):
            self.assertEqual(self.do_import("summary\nx\n", user=user).status, 403)

    def test_byte_order_mark_and_header_variants(self):
        text = "\ufeffTask Name,Due_Date,Estimate (hours),Tags\nT,01-Dec-2026,2.5,a b;c\n"
        self.ok(self.do_import(text))
        t = self.tasks()[0]
        self.assertEqual((t["title"], t["due_date"], t["estimate_hours"], t["labels"]), ("T", "2026-12-01", 2.5, ["a", "b", "c"]))

    def test_row_limit_is_enforced(self):
        with mock.patch.object(csvio, "MAX_IMPORT_ROWS", 2):
            report = self.ok(self.do_import("summary\na\nb\nc\nd\n"))
        self.assertEqual(report["created"], 2)
        self.assertIn("row limit", report["errors"][0]["message"])

    def test_importing_does_not_ping_cliq_per_task(self):
        self.do_import("summary,assignee\nA,bob\nB,carol\n")
        self.assertEqual(self.notifier.messages, [])


class DateParsingTests(ApiTestCase):
    def test_accepted_and_rejected_formats(self):
        parse = lambda s: (csvio.parse_loose_date(s).isoformat() if csvio.parse_loose_date(s) else None)  # noqa: E731
        self.assertEqual(parse("2026-07-30"), "2026-07-30")
        self.assertEqual(parse("2026-07-30T10:15:00.000+0000"), "2026-07-30")
        self.assertEqual(parse("30-Jul-26"), "2026-07-30")
        self.assertEqual(parse("30-jul-2026"), "2026-07-30")
        self.assertEqual(parse("30/Jul/26 12:00 AM"), "2026-07-30")
        self.assertEqual(parse("5 September 2026"), "2026-09-05")
        for bad in ("07/30/2026", "30/07/2026", "31-Feb-26", "30-Foo-26", "", "tomorrow", "2026-13-01"):
            self.assertIsNone(parse(bad), bad)  # numeric d/m vs m/d is ambiguous, so it is refused


class ExportTests(ImportBase):
    def rows(self):
        r = self.alice.get("/api/projects/IMP/export.csv")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/csv"))
        self.assertIn('filename="IMP-tasks.csv"', r.headers["Content-Disposition"])
        return list(csv.DictReader(io.StringIO(r.text.lstrip("\ufeff"))))

    def test_columns_and_values(self):
        self.make_task("IMP", "Plain", assignee="bob", priority="high", due_date="2026-11-01", estimate_hours=2, labels=["x", "y"], description="line1\nline2, with comma")
        [row] = self.rows()
        self.assertEqual(list(row), csvio.EXPORT_COLUMNS)
        self.assertEqual((row["key"], row["assignee"], row["priority"], row["labels"], row["estimate_hours"]), ("IMP-1", "bob", "high", "x;y", "2.0"))
        self.assertEqual(row["description"], "line1\nline2, with comma")

    def test_spreadsheet_formulas_are_neutralised(self):
        payloads = ['=HYPERLINK("http://evil.example","click")', "+1+1", "-2+3", "@SUM(A1:A9)"]
        for p in payloads:
            self.make_task("IMP", p, description=p)
        rows = self.rows()
        for row, payload in zip(rows, payloads, strict=True):
            self.assertEqual(row["title"], "'" + payload)
            self.assertEqual(row["description"], "'" + payload)
        self.make_task("IMP", "safe: =not at start")
        self.assertEqual(self.rows()[-1]["title"], "safe: =not at start")

    def test_escaping_helper_covers_tab_and_carriage_return_too(self):
        # Input validation trims leading whitespace, so these cannot arrive via the API;
        # the exporter still guards them in case data is ever loaded another way.
        self.assertEqual(csvio._safe("\t=cmd"), "'\t=cmd")
        self.assertEqual(csvio._safe("\r=cmd"), "'\r=cmd")
        self.assertEqual((csvio._safe(None), csvio._safe(""), csvio._safe("plain"), csvio._safe(12)), ("", "", "plain", "12"))

    def test_export_then_import_round_trips_even_risky_titles(self):
        self.make_task("IMP", "-- cleanup", priority="critical", assignee="bob", labels=["tidy"], due_date="2026-11-05")
        self.make_task("IMP", "=1+1", status="done")
        exported = self.alice.get("/api/projects/IMP/export.csv").text
        self.make_project("COPY", members=("bob",))
        r = self.alice.request("POST", "/api/projects/COPY/import", raw=exported.encode("utf-8"), content_type="text/csv")
        report = self.ok(r)
        self.assertEqual(report["created"], 2, report)
        copy = self.ok(self.alice.get("/api/tasks", query={"project": "COPY"}))["items"]
        self.assertEqual([t["title"] for t in copy], ["-- cleanup", "=1+1"])
        self.assertEqual((copy[0]["priority"], copy[0]["assignee"]["username"], copy[0]["labels"], copy[0]["due_date"]), ("critical", "bob", ["tidy"], "2026-11-05"))
        self.assertEqual(copy[1]["status"], "done")
        self.assertEqual(copy[0]["external_ref"], "IMP-1")  # original key kept for traceability

    def test_round_trip_is_lossless_even_for_text_that_already_starts_with_an_apostrophe(self):
        titles = ["'=quoted formula", "''+two", "'plain apostrophe", "=x", "normal", "-dash", "it's fine", "@mention-looking"]
        for t in titles:
            self.make_task("IMP", t)
        exported = self.alice.get("/api/projects/IMP/export.csv").text
        self.make_project("COPY")
        report = self.ok(self.alice.request("POST", "/api/projects/COPY/import", raw=exported.encode(), content_type="text/csv"))
        self.assertEqual(report["created"], len(titles), report)
        copy = self.ok(self.alice.get("/api/tasks", query={"project": "COPY", "sort": "key"}))["items"]
        self.assertEqual([t["title"] for t in copy], titles)

    def test_viewers_can_export(self):
        self.assertEqual(self.client("vera").get("/api/projects/IMP/export.csv").status, 200)
        self.assertEqual(self.alice.get("/api/projects/NOPE/export.csv").status, 404)
