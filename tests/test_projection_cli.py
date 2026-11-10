"""The early-warning message: ``zylker-pm projection`` in the terminal, in Cliq, and its safety against hostile text."""

import contextlib
import io
import os
from unittest import mock

from helpers import ApiTestCase, CaptureServer

from zylker_pm import cli, notify
from zylker_pm.services import projection


class ProjectionCliCase(ApiTestCase):
    """LATE can never make its date (the date is long gone); FINE has until 2099."""

    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZYLKER_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.make_project("LATE", members=("bob",), due_date="2020-02-01")
        self.make_task("LATE", "Long overdue job", assignee="bob", estimate_hours=16, due_date="2020-01-06")
        self.make_project("FINE", members=("carol",), due_date="2099-01-01")
        self.make_task("FINE", "Plenty of time", assignee="carol", estimate_hours=8, due_date="2099-01-01")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", self.config.db_path, *args])
        return code, out.getvalue(), err.getvalue()


class CliTests(ProjectionCliCase):
    def test_it_prints_what_will_slip_and_why_and_what_is_fine(self):
        code, out, _ = self.run_cli("projection")
        self.assertEqual(code, 0)
        self.assertIn("*Delivery projection for", out)
        self.assertIn("*Project LATE* (LATE)", out)
        self.assertIn("*LATE-1* Long overdue job", out)
        self.assertIn("It is already overdue.", out)
        self.assertIn("On track: Project FINE", out)
        self.assertNotIn("Plenty of time", out)  # nothing to say about work that is fine

    def test_the_exit_code_can_say_that_something_is_late(self):
        self.assertEqual(self.run_cli("projection", "--fail-if-late")[0], 3)
        self.assertEqual(self.run_cli("projection", "--project", "fine", "--fail-if-late")[0], 0)  # case does not matter; this one is fine
        self.assertEqual(self.run_cli("projection")[0], 0)  # without the flag it is information, not a failure

    def test_a_single_project_is_always_shown_even_when_it_is_on_track(self):
        code, out, _ = self.run_cli("projection", "--project", "FINE")
        self.assertEqual(code, 0)
        self.assertIn("*Project FINE* (FINE)", out)
        self.assertNotIn("LATE", out)
        self.assertNotIn("On track:", out)

    def test_an_unknown_project_is_a_clear_error(self):
        code, _, err = self.run_cli("projection", "--project", "NOPE")
        self.assertEqual(code, 1)
        self.assertIn("Project NOPE not found", err)

    def test_when_everything_is_fine_it_says_so(self):
        self.assertEqual(self.client("alice").delete("/api/tasks/LATE-1").status, 204)
        code, out, _ = self.run_cli("projection", "--fail-if-late")
        self.assertEqual(code, 0)
        self.assertIn("Everything is projected to finish on time.", out)

    def test_posting_needs_a_webhook(self):
        code, out, err = self.run_cli("projection", "--post")
        self.assertEqual(code, 2)
        self.assertIn("ZYLKER_CLIQ_ENDPOINT", err)
        self.assertIn("Long overdue job", out)  # it still printed the message

    def test_it_posts_the_same_message_to_cliq(self):
        server = CaptureServer()
        self.addCleanup(server.close)
        with mock.patch.dict(os.environ, {"ZYLKER_CLIQ_ENDPOINT": server.url, "ZYLKER_PUBLIC_URL": "https://pm.example.com"}):
            code, out, _ = self.run_cli("projection", "--post")
        self.assertEqual(code, 0)
        self.assertIn("Posted to Cliq", out)
        [request] = server.requests
        self.assertIn("[LATE-1](https://pm.example.com/#/t/LATE-1) Long overdue job", request["body"]["text"])
        self.assertTrue(out.startswith(request["body"]["text"].replace("(https://pm.example.com/#/t/LATE-1)", "").replace("[LATE-1]", "*LATE-1*")[:40]))


class RenderingTests(ProjectionCliCase):
    def report(self, **kw):
        with self.app.session(write=False) as ctx:
            return projection.early_warning(ctx, **kw)

    def test_the_report_has_one_entry_per_project_that_needs_attention(self):
        report = self.report()
        self.assertEqual([p["project"] for p in report["projects"]], ["LATE"])
        self.assertEqual([p["project"] for p in report["on_track"]], ["FINE"])
        late = report["projects"][0]
        self.assertEqual((late["verdict"], late["late_tasks"], late["more"]), ("will_miss", 1, 0))
        self.assertEqual(sorted(late["tasks"][0]), ["assignee", "due_date", "explanation", "key", "late_by_days", "projected_finish", "state", "title"])
        self.assertEqual(report["totals"], {"flagged": 1, "late_tasks": 1})

    def test_only_the_worst_few_tasks_are_listed_and_the_rest_are_counted(self):
        for n in range(8):
            self.make_task("LATE", "Extra %d" % n, assignee="bob", estimate_hours=8, due_date="2020-01-06")
        late = self.report(limit=3)["projects"][0]
        self.assertEqual((len(late["tasks"]), late["more"], late["late_tasks"]), (3, 6, 9))
        text = notify.render_projection(self.report(limit=3))
        self.assertIn("\u2026and 6 more", text)

    def test_unprojectable_work_is_reported_instead_of_hidden(self):
        self.ok(self.admin.patch("/api/users/%d" % next(u["id"] for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == "carol"), {"weekly_capacity_hours": 0}))
        report = self.report()
        fine = next(p for p in report["projects"] if p["project"] == "FINE")
        self.assertEqual((fine["verdict"], fine["unprojectable"]), ("unknown", 1))
        text = notify.render_projection(report)
        self.assertIn("\U0001f7e1 *Project FINE* (FINE)", text)
        self.assertIn("\u2014 cannot be scheduled. Carol has no capacity (0 hours a week)", text)  # what happened to the task, then why
        self.assertNotIn("projected None", text)

    def test_a_project_that_is_not_being_worked_on_can_still_be_asked_about(self):
        self.ok(self.client("alice").patch("/api/projects/FINE", {"status": "on_hold"}))
        report = self.report(project_key="FINE")
        self.assertEqual((report["projects"][0]["state"], report["projects"][0]["headline"], report["projects"][0]["tasks"]), ("not_projected", "Not projected: the project is on hold.", []))
        self.assertIn("Not projected: the project is on hold.", notify.render_projection(report))

    def test_user_text_cannot_inject_markup_links_or_mentions(self):
        hostile = "*bold* [click](http://evil.example) {@all} <b>x</b> `c` ~s~ _i_"
        # critical, so it goes first in bob's queue: the other task's explanation ("busy with ...") then quotes this title too
        self.make_task("LATE", hostile, assignee="bob", estimate_hours=8, due_date="2020-01-01", priority="critical")
        text = notify.render_projection(self.report())
        for key in ("LATE-1", "LATE-2"):
            line = next(x for x in text.splitlines() if "*%s*" % key in x)
            self.assertIn("busy with" if key == "LATE-1" else "Starts right away", line)
            for ch in "[]{}<>`~_":
                self.assertNotIn(ch, line.replace("*%s*" % key, "", 1).replace("\u2022", ""), "%s in the line of %s" % (ch, key))
        self.assertNotIn("{@all}", text)
        self.assertNotIn("](http://evil", text)
        self.assertIn("busy with LATE-2 (bold click(http://evil.example) @all bx/b c s i)", text)  # the title as a person would read it, markup removed

    def fixable_project(self, title="Big job"):
        """FIX is due Wednesday the 21st (today is Friday the 16th): dave has 24 h + 8 h + 8 h in a row and finishes a day late; erin is free."""
        self.make_user("dave", "member")
        self.make_user("erin", "member")
        self.ok(self.admin.patch("/api/users/%d" % next(u["id"] for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == "alice"), {"weekly_capacity_hours": 0}))
        self.make_project("FIX", members=("dave", "erin"), due_date="2026-10-21")
        self.make_task("FIX", title, assignee="dave", estimate_hours=24, due_date="2026-10-20")
        self.make_task("FIX", "Review", assignee="dave", estimate_hours=8, due_date="2026-10-21")
        self.make_task("FIX", "Docs", assignee="dave", estimate_hours=8, due_date="2026-10-21")

    def test_the_worst_projects_get_a_suggested_fix_in_the_message(self):
        self.fixable_project()
        report = self.report()
        fix = next(p for p in report["projects"] if p["project"] == "FIX")
        self.assertEqual(sorted(fix["advice"]), ["reassign", "summary", "task"])
        self.assertEqual((fix["advice"]["task"], fix["advice"]["reassign"]), ("FIX-1", "FIX-1:erin"))
        late = next(p for p in report["projects"] if p["project"] == "LATE")
        self.assertIsNone(late["advice"])  # nothing to move: it is overdue wherever it is done
        lines = [x for x in notify.render_projection(report).splitlines() if "\U0001f4a1" in x]
        self.assertEqual(lines, ["  \U0001f4a1 Give FIX-1 (Big job) to Erin instead of Dave: the project would finish Tue 20 Oct, 1 day before the due date, "
                                 "and no task would be late; now it finishes Thu 22 Oct, 1 day after the due date, and 1 task is late."])  # fmt: skip

    def test_a_project_that_is_on_time_overall_still_gets_advice_for_its_late_tasks(self):
        self.fixable_project()
        self.ok(self.client("alice").patch("/api/projects/FIX", {"due_date": "2026-12-01"}))
        fix = next(p for p in self.report()["projects"] if p["project"] == "FIX")
        self.assertEqual((fix["verdict"], fix["late_tasks"]), ("on_track", 1))
        self.assertEqual(fix["advice"]["reassign"], "FIX-1:erin")

    def test_the_suggestion_is_cleaned_like_every_other_piece_of_user_text(self):
        self.fixable_project(title="*Big* [job](http://evil.example) {@all} <b>x</b> `c` ~s~ _i_")
        [line] = [x for x in notify.render_projection(self.report()).splitlines() if "\U0001f4a1" in x]
        for ch in "[]{}<>`~_*":
            self.assertNotIn(ch, line, ch)
        self.assertIn("Give FIX-1 (Big job(http://evil.example) @all bx/b c s i) to Erin instead of Dave", line)

    def test_only_the_worst_few_projects_are_advised_and_a_single_project_always_is(self):
        self.fixable_project()
        with mock.patch.object(projection, "MAX_ADVISED", 0):
            report = self.report()
            self.assertIsNone(next(p for p in report["projects"] if p["project"] == "FIX")["advice"])
            self.assertNotIn("\U0001f4a1", notify.render_projection(report))
        self.assertEqual(self.report(project_key="FIX")["projects"][0]["advice"]["reassign"], "FIX-1:erin")

    def test_a_huge_report_is_capped_below_the_cliq_limit(self):
        for n in range(60):
            self.make_project("P%02d" % n, members=("bob",), due_date="2020-01-01")
            self.make_task("P%02d" % n, "Task " + "t" * 150, assignee="bob", estimate_hours=8, due_date="2020-01-06")
        text = notify.render_projection(self.report(limit=5))
        self.assertEqual(len(text), notify.MAX_MESSAGE)
        self.assertLess(notify.MAX_MESSAGE, 5000)

    def test_links_use_the_public_url(self):
        text = notify.render_projection(self.report(), "https://pm.example.com")
        self.assertIn("[LATE-1](https://pm.example.com/#/t/LATE-1)", text)
