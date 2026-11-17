"""Recurring tasks: schedule maths, validation, the API, the generator's safety rules, the CLI and the upgrade."""

import json
import os
import sqlite3
import threading
import unittest
from datetime import date
from unittest import mock

from helpers import ApiTestCase
from test_cli import CliBase

from zylker_pm import cli, db
from zylker_pm.app import App
from zylker_pm.config import Config
from zylker_pm.db import init_db
from zylker_pm.errors import ValidationError
from zylker_pm.services import recurrence as rec


def rule(frequency="daily", interval=1, weekdays=(), month_day=None, start=date(2026, 10, 16), end=None, max_=None):
    return {"frequency": frequency, "interval": interval, "weekdays": list(weekdays), "month_day": month_day, "start": start, "end": end, "max": max_}


def first(r, n):
    return [d.isoformat() for d in rec.upcoming(r, r["start"], n)]


class ScheduleMathTests(unittest.TestCase):
    def test_daily_with_an_interval(self):
        self.assertEqual(first(rule(interval=3), 3), ["2026-10-16", "2026-10-19", "2026-10-22"])

    def test_weekly_on_several_days_and_every_other_week(self):
        # 2026-10-14 is a Wednesday; weeks are counted from the Monday of the start week
        r = rule("weekly", 2, weekdays=[0, 2], start=date(2026, 10, 14))
        self.assertEqual(first(r, 5), ["2026-10-14", "2026-10-26", "2026-10-28", "2026-11-09", "2026-11-11"])

    def test_weekly_never_yields_a_day_before_the_start(self):
        r = rule("weekly", 1, weekdays=[0, 1], start=date(2026, 10, 15))  # Thursday
        self.assertEqual(first(r, 2), ["2026-10-19", "2026-10-20"])

    def test_monthly_clamps_to_short_months_and_handles_leap_years(self):
        r = rule("monthly", 1, month_day=31, start=date(2027, 12, 31))
        self.assertEqual(first(r, 4), ["2027-12-31", "2028-01-31", "2028-02-29", "2028-03-31"])  # 2028 is a leap year
        r = rule("monthly", 1, month_day=30, start=date(2027, 1, 30))
        self.assertEqual(first(r, 2), ["2027-01-30", "2027-02-28"])

    def test_monthly_last_day_and_every_third_month(self):
        self.assertEqual(first(rule("monthly", 1, month_day=-1, start=date(2026, 1, 1)), 3), ["2026-01-31", "2026-02-28", "2026-03-31"])
        self.assertEqual(first(rule("monthly", 3, month_day=15, start=date(2026, 11, 20)), 3), ["2027-02-15", "2027-05-15", "2027-08-15"])

    def test_the_year_rolls_over(self):
        self.assertEqual(first(rule("monthly", 1, month_day=5, start=date(2026, 11, 1)), 3), ["2026-11-05", "2026-12-05", "2027-01-05"])

    def test_end_date_and_max_occurrences_stop_the_rule(self):
        self.assertEqual(first(rule(end=date(2026, 10, 18)), 10), ["2026-10-16", "2026-10-17", "2026-10-18"])
        self.assertEqual(len(list(rec.dates(rule(max_=4)))), 4)
        self.assertEqual(len(list(rec.dates(rule(end=date(2026, 10, 30), max_=2)))), 2)  # whichever comes first

    def test_a_rule_without_limits_still_ends(self):
        self.assertLess(len(list(rec.dates(rule("weekly", 1, weekdays=[0])))), 2000)

    def test_dates_between_excludes_the_lower_bound_and_includes_the_upper(self):
        r = rule()
        got = [d.isoformat() for d in rec.dates_between(r, date(2026, 10, 17), date(2026, 10, 19))]
        self.assertEqual(got, ["2026-10-18", "2026-10-19"])
        self.assertEqual(len(rec.dates_between(r, None, date(2026, 10, 18))), 3)

    def test_plain_english_description(self):
        self.assertEqual(rec.describe(rule()), "Every day")
        self.assertEqual(rec.describe(rule("weekly", 2, weekdays=[0, 4])), "Every 2 weeks on Mon, Fri")
        self.assertEqual(rec.describe(rule("monthly", 1, month_day=-1)), "Every month on the last day")
        self.assertEqual(rec.describe(rule("monthly", 3, month_day=15)), "Every 3 months on day 15")


class RecurrenceBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.alice = self.client("alice")

    def body(self, **extra):
        return {"title": "Weekly report {date}", "frequency": "weekly", "weekdays": ["fri"], "start_date": "2026-10-16", **extra}

    def create(self, **extra):
        return self.ok(self.alice.post("/api/projects/WEB/recurrences", self.body(**extra)), 201)

    def run_gen(self, **query):
        return self.ok(self.admin.post("/api/recurrences/run", query=query))

    def task_titles(self):
        return [t["title"] for t in self.ok(self.admin.get("/api/tasks", query={"project": "WEB", "sort": "number"}))["items"]]


class ApiTests(RecurrenceBase):
    def test_create_returns_the_rule_with_its_next_dates(self):
        r = self.create(assignee="bob", priority="high", estimate_hours=2, labels=["Ops", "weekly"], due_offset_days=2)
        self.assertEqual((r["project"], r["schedule"], r["status"], r["active"]), ("WEB", "Every week on Fri", "active", True))
        self.assertEqual(r["next_dates"], ["2026-10-16", "2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13"])
        self.assertEqual((r["assignee"], r["priority"], r["labels"], r["created_by"], r["tasks_created"]), ("bob", "high", ["ops", "weekly"], "alice", 0))
        self.assertEqual(self.ok(self.client("vera").get("/api/recurrences/%d" % r["id"]))["title"], "Weekly report {date}")  # viewers can read
        self.assertEqual([i["id"] for i in self.ok(self.client("vera").get("/api/projects/WEB/recurrences"))["items"]], [r["id"]])

    def test_defaults_come_from_the_start_date(self):
        w = self.ok(self.alice.post("/api/projects/WEB/recurrences", {"title": "A", "frequency": "weekly", "start_date": "2026-10-14"}), 201)
        self.assertEqual(w["weekdays"], ["wed"])
        m = self.ok(self.alice.post("/api/projects/WEB/recurrences", {"title": "B", "frequency": "monthly", "start_date": "2026-10-14"}), 201)
        self.assertEqual((m["month_day"], m["schedule"]), (14, "Every month on day 14"))

    def test_roles(self):
        for user in ("bob", "vera"):
            r = self.client(user).post("/api/projects/WEB/recurrences", self.body())
            self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), user)
        r = self.create()
        self.assertEqual(self.client("bob").patch("/api/recurrences/%d" % r["id"], {"active": False}).status, 403)
        self.assertEqual(self.client("bob").delete("/api/recurrences/%d" % r["id"]).status, 403)
        self.assertEqual(self.alice.post("/api/recurrences/run").status, 403)  # running the generator is an admin action
        self.assertEqual(self.client().get("/api/recurrences/1").status, 401)

    def test_validation_reports_every_problem(self):
        r = self.alice.post("/api/projects/WEB/recurrences", {"frequency": "yearly", "interval": 0, "start_date": "2026-13-01", "bogus": 1, "weekdays": ["funday"]})
        self.assertEqual(r.status, 422)
        d = r.json["error"]["details"]
        self.assertEqual(set(d), {"title", "frequency", "interval", "start_date", "bogus", "weekdays"})

    def test_cross_field_rules(self):
        cases = [
            ({"end_date": "2026-10-01"}, "end_date"),  # before the start
            ({"weekdays": []}, "weekdays"),  # weekly needs a day
            ({"month_day": 0, "frequency": "monthly"}, "month_day"),
            ({"title": "x" * 195 + "{date}"}, "title"),  # too long once the date is filled in
            ({"end_date": "2026-10-17"}, "end_date"),  # first Friday (16th) is fine ...
            ({"start_date": "1999-01-01"}, "start_date"),
            ({"assignee": "ghost"}, "assignee"),
            ({"assignee": "vera"}, "assignee"),  # not a member of WEB
            ({"max_occurrences": 0}, "max_occurrences"),
            ({"due_offset_days": 400}, "due_offset_days"),
        ]
        for extra, field in cases:
            r = self.alice.post("/api/projects/WEB/recurrences", self.body(**extra))
            if field == "end_date" and extra["end_date"] == "2026-10-17":
                self.assertEqual(r.status, 201)  # ... so this one is valid
                continue
            self.assertEqual((r.status, field in r.json["error"]["details"]), (422, True), (extra, r.text))

    def test_a_rule_that_can_never_fire_is_refused(self):
        r = self.alice.post("/api/projects/WEB/recurrences", self.body(start_date="2026-10-17", end_date="2026-10-18"))  # Sat-Sun, Fridays only
        self.assertEqual((r.status, "end_date" in r.json["error"]["details"]), (422, True))

    def test_at_most_fifty_rules_per_project(self):
        for i in range(rec.MAX_RULES_PER_PROJECT):
            self.create(title="R%d" % i)
        self.assertEqual(self.alice.post("/api/projects/WEB/recurrences", self.body()).status, 422)

    def test_archived_projects_are_read_only(self):
        r = self.create()
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "archived"}))
        self.assertEqual(self.alice.post("/api/projects/WEB/recurrences", self.body()).error_code, "project_archived")
        self.assertEqual(self.alice.patch("/api/recurrences/%d" % r["id"], {"title": "x"}).error_code, "project_archived")

    def test_update_changes_only_what_is_sent_and_logs_the_change(self):
        r = self.create(assignee="bob")
        u = self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"title": "Renamed", "priority": "low"}))
        self.assertEqual((u["title"], u["priority"], u["assignee"], u["schedule"]), ("Renamed", "low", "bob", "Every week on Fri"))
        u = self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"assignee": None}))
        self.assertIsNone(u["assignee"])
        [e] = [i for i in self.ok(self.admin.get("/api/audit", query={"action": "recurrence.updated"}))["items"]][:1]
        self.assertEqual(e["details"]["changes"], {"assignee": ["bob", None]})

    def test_changing_the_frequency_starts_from_that_frequencys_defaults(self):
        r = self.create(weekdays=["mon", "fri"])
        u = self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"frequency": "monthly", "month_day": -1}))
        self.assertEqual((u["weekdays"], u["month_day"], u["schedule"]), ([], -1, "Every month on the last day"))
        u = self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"frequency": "daily"}))
        self.assertEqual((u["weekdays"], u["month_day"], u["schedule"]), ([], None, "Every day"))

    def test_pause_and_status(self):
        r = self.create(end_date="2026-10-20")
        self.assertEqual(self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"active": False}))["status"], "paused")
        self.advance_days(10)
        self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"active": True}))
        self.assertEqual(self.ok(self.alice.get("/api/recurrences/%d" % r["id"]))["status"], "finished")  # its end date has passed

    def test_delete_keeps_the_tasks_it_made(self):
        r = self.create()
        self.run_gen()
        self.assertEqual(self.alice.delete("/api/recurrences/%d" % r["id"]).status, 204)
        self.assertEqual(self.task_titles(), ["Weekly report 2026-10-16"])
        self.assertEqual(self.alice.get("/api/recurrences/%d" % r["id"]).error_code, "recurrence_not_found")
        self.assertEqual(self.alice.get("/api/recurrences/abc").error_code, "recurrence_not_found")

    def test_deleting_a_project_removes_its_rules(self):
        r = self.create()
        self.assertEqual(self.admin.delete("/api/projects/WEB", query={"confirm": "WEB"}).status, 204)
        self.assertEqual(self.admin.get("/api/recurrences/%d" % r["id"]).status, 404)
        conn = sqlite3.connect(self.config.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM task_recurrences").fetchone()[0], 0)
        conn.close()

    def test_preview_saves_nothing_and_validates(self):
        p = self.ok(self.alice.post("/api/recurrences/preview", {"frequency": "monthly", "month_day": -1, "start_date": "2026-10-01"}, query={"count": 3}))
        self.assertEqual((p["schedule"], p["dates"]), ("Every month on the last day", ["2026-10-31", "2026-11-30", "2026-12-31"]))
        self.assertEqual(self.ok(self.alice.get("/api/projects/WEB/recurrences"))["items"], [])
        self.assertEqual(self.alice.post("/api/recurrences/preview", {"frequency": "nope", "start_date": "2026-10-01"}).status, 422)
        self.assertEqual(self.alice.post("/api/recurrences/preview", {"frequency": "daily", "start_date": "2026-10-01"}, query={"count": 99}).status, 422)
        self.assertEqual(self.client("bob").post("/api/recurrences/preview", {}).status, 403)

    def test_meta_describes_the_options(self):
        m = self.ok(self.client("vera").get("/api/meta"))["recurrence"]
        self.assertEqual((m["frequencies"], m["weekdays"][0], m["max_catch_up"]), (["daily", "weekly", "monthly"], "mon", 5))


class GeneratorTests(RecurrenceBase):
    def test_creates_the_task_from_the_template(self):
        self.create(assignee="bob", priority="high", estimate_hours=2.5, labels=["ops"], due_offset_days=2, description="Collect the numbers", type="story")
        rep = self.run_gen()
        self.assertEqual([(c["date"], c["task"]) for c in rep["created"]], [("2026-10-16", "WEB-1")])
        t = self.ok(self.alice.get("/api/tasks/WEB-1"))
        self.assertEqual((t["title"], t["priority"], t["type"], t["estimate_hours"], t["labels"]), ("Weekly report 2026-10-16", "high", "story", 2.5, ["ops"]))
        self.assertEqual((t["start_date"], t["due_date"], t["assignee"]["username"], t["reporter"]["username"], t["status"]), ("2026-10-16", "2026-10-18", "bob", "alice", "todo"))
        self.assertEqual(t["description"], "Collect the numbers")

    def test_running_twice_never_duplicates(self):
        self.create()
        self.assertEqual(len(self.run_gen()["created"]), 1)
        self.assertEqual(len(self.run_gen()["created"]), 0)
        self.assertEqual(self.task_titles(), ["Weekly report 2026-10-16"])

    def test_nothing_happens_before_the_start_or_between_dates(self):
        self.create(start_date="2026-10-23")
        self.assertEqual(self.run_gen()["created"], [])
        self.advance_days(7)
        self.assertEqual(len(self.run_gen()["created"]), 1)
        self.advance_days(3)
        self.assertEqual(self.run_gen()["created"], [])  # next one is a week away
        self.advance_days(4)
        self.assertEqual([c["date"] for c in self.run_gen()["created"]], ["2026-10-30"])

    def test_a_rule_that_starts_in_the_past_does_not_backfill(self):
        self.ok(self.alice.post("/api/projects/WEB/recurrences", {"title": "Daily", "frequency": "daily", "start_date": "2026-09-01"}), 201)
        self.assertEqual([c["date"] for c in self.run_gen()["created"]], ["2026-10-16"])  # today only

    def test_catching_up_after_downtime_is_capped_and_the_rest_recorded_as_skipped(self):
        r = self.ok(self.alice.post("/api/projects/WEB/recurrences", {"title": "Daily {date}", "frequency": "daily", "start_date": "2026-10-16"}), 201)
        self.run_gen()
        self.advance_days(20)  # nobody ran the generator for 20 days
        rep = self.run_gen()
        self.assertEqual(len(rep["created"]), rec.MAX_CATCH_UP)
        self.assertEqual([c["date"] for c in rep["created"]], ["2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"])
        self.assertEqual((rep["skipped"][0]["reason"], len(rep["skipped"][0]["dates"])), ("catch_up_limit", 15))
        occ = self.ok(self.alice.get("/api/recurrences/%d/occurrences" % r["id"], query={"limit": 200}))["items"]
        self.assertEqual({o["outcome"] for o in occ}, {"created", "skipped"})
        self.assertEqual(len(occ), 21)
        self.assertEqual(self.run_gen()["created"], [])
        skipped = self.ok(self.admin.get("/api/audit", query={"action": "recurrence.skipped"}))["items"]
        self.assertEqual((skipped[0]["severity"], skipped[0]["details"]["count"]), ("notice", 15))

    def test_resuming_or_editing_a_rule_never_backfills(self):
        r = self.ok(self.alice.post("/api/projects/WEB/recurrences", {"title": "Daily", "frequency": "daily", "start_date": "2026-10-16"}), 201)
        self.run_gen()
        self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"active": False}))
        self.advance_days(10)
        self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"active": True}))
        self.assertEqual([c["date"] for c in self.run_gen()["created"]], ["2026-10-26"])  # only today, not the paused ten days
        self.advance_days(5)
        self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"interval": 3}))  # a schedule edit is not a licence to backfill either
        self.assertEqual([c["date"] for c in self.run_gen()["created"]], ["2026-10-31"])

    def test_paused_rules_are_ignored(self):
        r = self.create()
        self.ok(self.alice.patch("/api/recurrences/%d" % r["id"], {"active": False}))
        rep = self.run_gen()
        self.assertEqual((rep["checked"], rep["created"]), (0, []))

    def test_archived_and_completed_projects_get_nothing_but_keep_the_dates_for_later(self):
        self.ok(self.alice.post("/api/projects/WEB/recurrences", {"title": "Daily {date}", "frequency": "daily", "start_date": "2026-10-16"}), 201)
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "archived"}))
        rep = self.run_gen()
        self.assertEqual((rep["created"], rep["skipped"][0]["reason"], rep["skipped"][0]["kept_for_later"]), ([], "project_archived", True))
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "active"}))
        self.advance_days(2)
        self.assertEqual([c["date"] for c in self.run_gen()["created"]], ["2026-10-16", "2026-10-17", "2026-10-18"])  # nothing was lost

    def test_a_member_who_left_the_project_leaves_the_task_unassigned_with_a_note(self):
        self.create(assignee="bob")
        self.alice.delete("/api/projects/WEB/members/bob")
        rep = self.run_gen()
        self.assertEqual(rep["created"][0]["note"], "assignee_unavailable")
        self.assertIsNone(self.ok(self.alice.get("/api/tasks/WEB-1"))["assignee"])
        occ = self.ok(self.alice.get("/api/recurrences/1/occurrences"))["items"]
        self.assertEqual(occ[0]["reason"], "assignee_unavailable")

    def test_the_reporter_falls_back_to_the_project_owner_when_the_creator_is_deactivated(self):
        self.ok(self.alice.patch("/api/projects/WEB", {"owner": "bob"}))
        self.create()
        alice_id = next(u["id"] for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == "alice")
        self.ok(self.admin.patch("/api/users/%d" % alice_id, {"active": False}))
        self.run_gen()
        self.assertEqual(self.ok(self.admin.get("/api/tasks/WEB-1"))["reporter"]["username"], "bob")

    def test_a_date_that_fails_is_recorded_and_the_others_still_get_created(self):
        self.ok(self.alice.post("/api/projects/WEB/recurrences", {"title": "Daily {date}", "frequency": "daily", "start_date": "2026-10-16"}), 201)
        self.run_gen()
        self.advance_days(2)
        real = rec.tasks.create_in_project

        def flaky(ctx, project, data, **kw):
            if data["start_date"] == "2026-10-17":
                raise ValidationError({"title": "refused for the test"})
            return real(ctx, project, data, **kw)

        with mock.patch.object(rec.tasks, "create_in_project", flaky):
            rep = self.run_gen()
        self.assertEqual([c["date"] for c in rep["created"]], ["2026-10-18"])
        self.assertIn("refused for the test", rep["skipped"][0]["reason"])
        occ = {o["date"]: o for o in self.ok(self.alice.get("/api/recurrences/1/occurrences"))["items"]}
        self.assertEqual((occ["2026-10-17"]["outcome"], occ["2026-10-18"]["outcome"]), ("skipped", "created"))
        self.assertEqual(self.task_titles(), ["Daily 2026-10-16", "Daily 2026-10-18"])  # no half-created task for the failed date

    def test_dry_run_changes_nothing(self):
        r = self.create()
        rep = self.run_gen(dry_run="true")
        self.assertEqual(([c["date"] for c in rep["created"]], rep["dry_run"]), (["2026-10-16"], True))
        self.assertEqual(self.task_titles(), [])
        self.assertEqual(self.ok(self.alice.get("/api/recurrences/%d" % r["id"]))["generated_through"], "2026-10-15")
        self.assertEqual(len(self.run_gen()["created"]), 1)  # and the real run still has the work to do

    def test_can_be_limited_to_one_project(self):
        self.make_project("API", members=("bob",))
        self.create()
        self.ok(self.alice.post("/api/projects/API/recurrences", self.body()), 201)
        rep = self.run_gen(project="API")
        self.assertEqual([c["project"] for c in rep["created"]], ["API"])
        self.assertEqual(self.admin.post("/api/recurrences/run", query={"project": "NOPE"}).status, 404)

    def test_a_deleted_task_is_not_created_again(self):
        self.create()
        self.run_gen()
        self.assertEqual(self.alice.delete("/api/tasks/WEB-1").status, 204)
        self.assertEqual(self.run_gen()["created"], [])
        occ = self.ok(self.alice.get("/api/recurrences/1/occurrences"))["items"]
        self.assertEqual((occ[0]["outcome"], occ[0]["task"]), ("created", "WEB-1"))  # the record keeps the key

    def test_it_is_audited_and_notifies_the_assignee(self):
        self.create(assignee="bob")
        self.run_gen()
        actions = [i["action"] for i in self.ok(self.admin.get("/api/audit", query={"category": "recurrence"}))["items"]]
        self.assertEqual(sorted(actions), ["recurrence.created", "recurrence.generated"])
        gen = self.ok(self.admin.get("/api/audit", query={"action": "recurrence.generated"}))["items"][0]
        self.assertEqual((gen["task"], gen["project"], gen["details"]["occurrence"]), ("WEB-1", "WEB", "2026-10-16"))
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("WEB-1", self.notifier.messages[0])
        self.assertIn("Bob", self.notifier.messages[0])

    def test_two_generators_at_once_create_each_task_exactly_once(self):
        self.create()
        results, errors = [], []

        def worker():
            try:
                app = App(self.config, clock=lambda: self.now)
                with app.session() as ctx:
                    results.append(len(rec.run(ctx)["created"]))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [0, 0, 0, 0, 1])
        self.assertEqual(self.task_titles(), ["Weekly report 2026-10-16"])


class CliAndBackgroundTests(CliBase):
    def test_recur_run_creates_due_tasks_and_dry_run_does_not(self):
        code, out, _ = self.run_cli("init", "--admin", "prem")
        token = self.token_in(out)
        api = self.api(token)
        self.assertEqual(api.post("/api/projects", {"key": "OPS", "name": "Ops"}).status, 201)
        self.assertEqual(api.post("/api/projects/OPS/recurrences", {"title": "Backup check {date}", "frequency": "daily", "start_date": "2026-10-16"}).status, 201)
        with mock.patch("zylker_pm.cli.App", lambda cfg: App(cfg, clock=lambda: self.now)):
            code, out, _ = self.run_cli("recur-run", "--dry-run")
            self.assertEqual((code, len(json.loads(out)["created"])), (0, 1))
            self.assertEqual(api.get("/api/tasks").json["total"], 0)
            code, out, _ = self.run_cli("recur-run")
            self.assertEqual((code, [c["task"] for c in json.loads(out)["created"]]), (0, ["OPS-1"]))
            code, out, _ = self.run_cli("recur-run", "--project", "OPS")
            self.assertEqual(json.loads(out)["created"], [])
        self.assertEqual(api.get("/api/tasks/OPS-1").json["title"], "Backup check 2026-10-16")

    def test_the_background_pass_returns_how_many_tasks_it_made(self):
        self.run_cli("init", "--admin", "prem")
        app = App(Config(db_path=self.db), clock=lambda: self.now)
        self.assertEqual(cli.run_recurrences_once(app), 0)

    def test_the_loop_survives_a_failing_pass(self):
        stop = threading.Event()
        calls = []

        def boom(app):
            calls.append(1)
            if len(calls) >= 2:
                stop.set()
            raise RuntimeError("database is locked")

        class FastStop:
            def wait(self, seconds):
                return stop.is_set()

        with mock.patch.object(cli, "run_recurrences_once", boom), self.assertLogs("zylker.recurrence", "ERROR"):
            cli._recurrence_loop(object(), 1, FastStop())
        self.assertEqual(len(calls), 2)  # the first failure did not end the loop


class ConfigTests(ApiTestCase):
    def test_the_check_interval_is_validated(self):
        self.assertEqual(Config.from_env({}).recurrence_check_minutes, 15)
        self.assertEqual(Config.from_env({"ZYLKER_RECURRENCE_CHECK_MINUTES": "0"}).recurrence_check_minutes, 0)
        for bad in ("-1", "1441", "soon", ""):
            with self.assertRaises(ValueError, msg=bad):
                Config.from_env({"ZYLKER_RECURRENCE_CHECK_MINUTES": bad})


class UpgradeTests(ApiTestCase):
    def v3_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        db._migrate(conn, 1)
        db._migrate(conn, 2)
        conn.close()

    def tables(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_a_v3_database_upgrades_and_matches_a_fresh_one(self):
        old, fresh = os.path.join(self.tmp, "v3.db"), os.path.join(self.tmp, "fresh4.db")
        self.v3_database(old)
        self.assertNotIn("task_recurrences", self.tables(old)[0])
        init_db(old)
        init_db(fresh)
        self.assertEqual(self.tables(old), self.tables(fresh))
        self.assertIn("recurrence_occurrences", self.tables(old)[0])
        self.assertEqual(self.tables(old)[1], db.SCHEMA_VERSION)

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v3b.db")
        self.v3_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE recurrence_occurrences (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        tables, version = self.tables(path)
        self.assertEqual(version, 3)
        self.assertNotIn("task_recurrences", tables)  # the first CREATE was rolled back with the failing one
