"""The client portal's project report: a hand-worked week, the corners of the period, and an independent check on random projects."""

import json
import random
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from test_clients import ClientCase

from zylker_pm.services import projection


def at(day, hour=9, month=10):
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


class WeekCase(ClientCase):
    """Project WEB (Fri 16 Oct 2026 is "today"; the report covers 10 to 16 Oct) and everything that happened to it."""

    def setUp(self):
        super().setUp()
        self.now = at(5)
        self.make_project("WEB", members=("bob", "carol"), start_date="2026-09-28", due_date="2026-11-13")
        ms = {}
        for name, due in (("Design sign-off", "2026-10-14"), ("Beta", "2026-10-30"), ("Go live", "2026-11-13")):
            ms[name] = self.ok(self.a.post("/api/projects/WEB/milestones", {"name": name, "due_date": due}), 201)["id"]
        self.make_task("WEB", "Discovery", due_date="2026-10-09", estimate_hours=4)
        self.make_task("WEB", "Design", due_date="2026-10-14", estimate_hours=8, milestone=ms["Design sign-off"])
        self.make_task("WEB", "Build", due_date="2026-10-30", estimate_hours=20, milestone=ms["Beta"])
        self.make_task("WEB", "Test", due_date="2026-11-06", estimate_hours=10, milestone=ms["Beta"])
        self.make_task("WEB", "Launch", due_date="2026-11-13", estimate_hours=4, milestone=ms["Go live"])
        self.now = at(6)
        self.ok(self.set_status("WEB-1", "done"))  # before the week
        self.now = at(12)
        self.ok(self.set_status("WEB-2", "done"))
        self.make_task("WEB", "Docs", due_date="2026-10-23", estimate_hours=3)  # WEB-6
        self.now = at(13)
        self.ok(self.a.patch("/api/tasks/WEB-3", {"due_date": "2026-11-04"}))  # five days later
        self.make_task("WEB", "Extra")  # WEB-7
        self.now = at(14)
        self.ok(self.set_status("WEB-7", "cancelled"))
        self.ok(self.a.patch("/api/tasks/WEB-4", {"due_date": "2026-11-03"}))  # three days earlier ...
        self.now = at(15)
        self.ok(self.a.patch("/api/tasks/WEB-4", {"due_date": "2026-11-09"}))  # ... and then three days later than it started
        self.make_task("WEB", "Compliance", due_date="2026-10-15", estimate_hours=2)  # WEB-8, a day overdue
        self.ok(self.set_status("WEB-6", "blocked"))
        self.acme = self.new_client("Acme")
        self.share(self.acme, "WEB", "full", update="Beta moves a few days.")
        self.now = at(16)
        self.contact(self.acme)

    def report(self, level="full", **query):
        if level != "full":
            self.share(self.acme, "WEB", level)
        return self.ok(self.client("rita").get("/api/portal/projects/WEB", query=query or None))


class WeekTests(WeekCase):
    def test_the_summary_level(self):
        self.assertEqual(
            self.report("summary"),
            {
                "client": {"id": 1, "name": "Acme"},
                "project": {"key": "WEB", "name": "Project WEB", "status": "active", "owner": "Alice", "start_date": "2026-09-28", "due_date": "2026-11-13"},
                "level": "summary",
                "as_of": "2026-10-16",
                "period": {"from": "2026-10-10", "to": "2026-10-16", "days": 7},
                "rating": {"status": "at_risk", "reasons": ["1 task(s) overdue"]},
                "headline": "Project WEB is at risk. 2 of 7 tasks are done (29%); 1 finished in the last 7 days.",
                "update": {"text": "Beta moves a few days.", "by": "Alice", "at": "2026-10-15T09:00:00Z"},
                "progress": {"tasks": {"total": 7, "done": 2, "open": 5, "percent": 28.6}, "period": {"completed": 1, "added": 2}},
                "milestones": {
                    "items": [
                        {"name": "Beta", "due_date": "2026-10-30", "status": "open", "tasks_done": 0, "tasks_total": 2, "progress": 0.0},
                        {"name": "Go live", "due_date": "2026-11-13", "status": "open", "tasks_done": 0, "tasks_total": 1, "progress": 0.0},
                    ],
                    "more": 0,
                    "achieved": 1,
                },
            },
        )

    def test_the_full_level_adds_the_forecast_and_what_happened_task_by_task(self):
        full = self.report()
        self.assertEqual(full["headline"], "Project WEB is at risk. 2 of 7 tasks are done (29%); 1 finished in the last 7 days. "
                                           "Projected to finish Tue 20 Oct, 24 days before the due date (Fri 13 Nov). 1 of 5 open tasks will miss its own due date.")
        self.assertEqual(
            full["forecast"],
            {
                "state": "projected",
                "headline": "Projected to finish Tue 20 Oct, 24 days before the due date (Fri 13 Nov). 1 of 5 open tasks will miss its own due date.",
                "finish": "2026-10-20",
                "due_date": "2026-11-13",
                "late_by_days": 0,
                "verdict": "on_track",
                "late": {"count": 1, "items": [{"key": "WEB-8", "title": "Compliance", "due_date": "2026-10-15", "projected_finish": "2026-10-16", "late_by_days": 1}], "more": 0},
            },
        )
        self.assertEqual([(m["name"], m["projected_finish"], m["late_by_days"]) for m in full["milestones"]["items"]], [("Beta", "2026-10-20", 0), ("Go live", "2026-10-16", 0)])
        self.assertEqual(full["completed"], {"count": 1, "items": [{"key": "WEB-2", "title": "Design", "completed_on": "2026-10-12"}], "more": 0})
        self.assertEqual(full["upcoming"], {"days": 14, "count": 1, "items": [{"key": "WEB-6", "title": "Docs", "due_date": "2026-10-23", "status": "blocked"}], "more": 0})
        self.assertEqual(
            full["attention"],
            {
                "overdue": {"count": 1, "items": [{"key": "WEB-8", "title": "Compliance", "due_date": "2026-10-15", "days_overdue": 1}], "more": 0},
                "blocked": {"count": 1, "items": [{"key": "WEB-6", "title": "Docs"}], "more": 0},
            },
        )
        self.assertEqual(
            full["changes"],
            {
                "added": {"count": 2, "items": [{"key": "WEB-6", "title": "Docs", "created_on": "2026-10-12"}, {"key": "WEB-8", "title": "Compliance", "created_on": "2026-10-15"}], "more": 0},
                "cancelled": {"count": 1, "items": [{"key": "WEB-7", "title": "Extra", "cancelled_on": "2026-10-14"}], "more": 0},
                "rescheduled": {
                    "later": 2,
                    "earlier": 0,
                    "count": 2,
                    "items": [
                        {"key": "WEB-3", "title": "Build", "from": "2026-10-30", "to": "2026-11-04", "days": 5},
                        {"key": "WEB-4", "title": "Test", "from": "2026-11-06", "to": "2026-11-09", "days": 3},
                    ],
                    "more": 0,
                },
            },
        )

    def test_a_shorter_or_longer_period_changes_what_counts_as_this_week(self):
        one = self.report(days="1")
        self.assertEqual(one["period"], {"from": "2026-10-16", "to": "2026-10-16", "days": 1})
        self.assertEqual((one["progress"]["period"], one["completed"]["count"], one["changes"]["added"]["count"], one["changes"]["rescheduled"]["count"]), ({"completed": 0, "added": 0}, 0, 0, 0))
        self.assertTrue(one["headline"].startswith("Project WEB is at risk. 2 of 7 tasks are done (29%); none finished today. "))
        three = self.report(days="3")  # 14 to 16 Oct
        self.assertEqual((three["changes"]["added"]["count"], three["changes"]["cancelled"]["count"], three["completed"]["count"]), (1, 1, 0))
        self.assertEqual([x["key"] for x in three["changes"]["rescheduled"]["items"]], ["WEB-4"], "the move of WEB-3 was on the 13th")
        self.assertEqual(three["changes"]["rescheduled"]["items"][0], {"key": "WEB-4", "title": "Test", "from": "2026-11-06", "to": "2026-11-09", "days": 3}, "from where it was when the period began")
        two = self.report(days="2")  # 15 and 16 Oct: only the second move of WEB-4 is in it
        self.assertEqual(two["changes"]["rescheduled"]["items"], [{"key": "WEB-4", "title": "Test", "from": "2026-11-03", "to": "2026-11-09", "days": 6}])
        long = self.report(days="90")
        self.assertEqual((long["completed"]["count"], long["progress"]["period"]["completed"]), (2, 2))
        self.assertEqual(long["changes"]["added"]["count"], 8 - 1, "every task but the cancelled one was created in these 90 days")

    def test_the_first_day_and_the_last_day_of_the_period_are_both_inside_it(self):
        self.now = at(10, 0)  # a task finished at the very start of the first day ...
        self.make_task("WEB", "First", status="done")  # WEB-9
        self.now = at(16, 0)  # ... and one at the very start of the last
        self.make_task("WEB", "Last", status="done")  # WEB-10
        self.now = at(9, 23)
        self.make_task("WEB", "Before", status="done")  # WEB-11: the evening before the period
        self.now = at(16)
        keys = [i["key"] for i in self.report()["completed"]["items"]]
        self.assertEqual(keys, ["WEB-10", "WEB-2", "WEB-9"], "newest first, the evening before is out")

    def test_the_lists_are_cut_but_the_counts_are_not(self):
        self.now = at(14)
        for n in range(14):
            self.make_task("WEB", "Overdue %d" % n, due_date="2026-10-%02d" % (1 + n % 9))
        self.now = at(16)
        full = self.report()
        self.assertEqual((full["attention"]["overdue"]["count"], len(full["attention"]["overdue"]["items"]), full["attention"]["overdue"]["more"]), (15, 10, 5))
        self.assertEqual(full["attention"]["overdue"]["items"][0]["days_overdue"], 15, "the longest overdue first")
        self.assertEqual(full["changes"]["added"]["count"], 16)
        self.assertEqual(len(full["changes"]["added"]["items"]), 10)

    def test_every_milestone_that_is_not_achieved_is_listed_up_to_a_limit(self):
        for n in range(10):
            self.ok(self.a.post("/api/projects/WEB/milestones", {"name": "M%d" % n, "due_date": "2026-12-%02d" % (1 + n)}), 201)
        summary = self.report("summary")
        self.assertEqual((len(summary["milestones"]["items"]), summary["milestones"]["more"], summary["milestones"]["achieved"]), (8, 4, 1))
        self.assertEqual(summary["milestones"]["items"][0]["name"], "Beta", "in the order they fall due")

    def test_an_overdue_milestone_says_so(self):
        self.ok(self.a.patch("/api/milestones/2", {"due_date": "2026-10-15"}))
        items = self.report("summary")["milestones"]["items"]
        self.assertEqual((items[0]["name"], items[0]["status"]), ("Beta", "overdue"))

    def test_the_manager_can_take_the_message_back(self):
        self.ok(self.a.request("PUT", "/api/clients/1/projects/WEB", {"update": None}))
        self.assertIsNone(self.report()["update"])
        self.assertIsNone(self.ok(self.client("rita").get("/api/portal/projects"))["items"][0]["update"])

    def test_the_project_list_has_one_line_per_shared_project(self):
        listing = self.ok(self.client("rita").get("/api/portal/projects"))
        self.assertEqual(
            listing,
            {
                "client": {"id": 1, "name": "Acme"},
                "items": [
                    {
                        "key": "WEB", "name": "Project WEB", "status": "active", "owner": "Alice", "start_date": "2026-09-28", "due_date": "2026-11-13", "share": "full",
                        "rating": {"status": "at_risk", "reasons": ["1 task(s) overdue"]},
                        "progress": {"percent": 28.6, "done": 2, "total": 7},
                        "update": {"text": "Beta moves a few days.", "by": "Alice", "at": "2026-10-15T09:00:00Z"},
                        "next_milestone": {"name": "Beta", "due_date": "2026-10-30", "status": "open"},
                    }
                ],
            },
        )

    def test_a_manager_who_leaves_a_message_is_named_and_a_removed_manager_is_not_a_problem(self):
        self.ok(self.root.patch("/api/users/%d" % self.sql("SELECT id FROM users WHERE username = 'alice'")[0][0], {"active": False}))
        self.assertEqual(self.report()["update"]["by"], "Alice", "she may have left, her message has not")


class HeadlineTests(WeekCase):
    def test_a_project_without_tasks(self):
        self.make_project("EMP", name="Empty one")
        beta = self.new_client("Beta")
        self.share(beta, "EMP", "full")
        self.contact(beta, "ben", "Ben")
        report = self.ok(self.client("ben").get("/api/portal/projects/EMP"))
        self.assertEqual(report["headline"], "Empty one has no tasks yet.")
        self.assertEqual((report["rating"]["status"], report["progress"]["tasks"]), ("not_started", {"total": 0, "done": 0, "open": 0, "percent": 0.0}))

    def test_a_finished_project_and_a_project_on_hold(self):
        self.ok(self.set_status("WEB-6", "todo"))  # a task flagged blocked has to be unblocked before it can be finished
        for key in ("WEB-%d" % n for n in (3, 4, 5, 6, 8)):
            self.ok(self.set_status(key, "done"))
        full = self.report()
        self.assertEqual(full["rating"]["status"], "complete")
        self.assertTrue(full["headline"].startswith("Project WEB is complete. 7 of 7 tasks are done (100%); 6 finished in the last 7 days."), full["headline"])
        self.assertEqual(full["forecast"]["state"], "complete")
        self.assertNotIn("Projected", full["headline"], "nothing is left to project")

    def test_a_project_on_hold_is_not_projected(self):
        self.ok(self.a.patch("/api/projects/WEB", {"status": "on_hold"}))
        full = self.report()
        self.assertEqual(full["rating"], {"status": "on_hold", "reasons": ["Project is on hold"]})
        self.assertEqual(full["headline"], "Project WEB is on hold. 2 of 7 tasks are done (29%); 1 finished in the last 7 days.")
        self.assertEqual(full["forecast"]["state"], "not_projected")
        self.assertIsNone(full["forecast"]["finish"])

    def test_singular_and_plural(self):
        self.make_project("ONE", name="One task")
        beta = self.new_client("Beta")
        self.share(beta, "ONE", "summary")
        self.contact(beta, "ben", "Ben")
        self.make_task("ONE", "Only")
        self.assertEqual(self.ok(self.client("ben").get("/api/portal/projects/ONE"))["headline"], "One task is on track. 0 of 1 task is done (0%); none finished in the last 7 days.")
        self.make_task("ONE", "Second")
        self.ok(self.set_status("ONE-1", "done"))
        self.assertEqual(self.ok(self.client("ben").get("/api/portal/projects/ONE"))["headline"], "One task is on track. 1 of 2 tasks is done (50%); 1 finished in the last 7 days.")


class ForecastTests(WeekCase):
    def test_it_is_the_projection_the_staff_see_and_says_nothing_about_people(self):
        self.ok(self.a.patch("/api/tasks/WEB-3", {"assignee": "bob"}))
        self.ok(self.a.patch("/api/tasks/WEB-8", {"assignee": "bob"}))  # Bob queues: the staff see "busy with ..."
        staff = self.ok(self.a.get("/api/projects/WEB/projection"))
        full = self.report()
        for key in ("finish", "due_date", "late_by_days", "verdict", "headline"):
            self.assertEqual(full["forecast"][key], staff[key], key)
        self.assertEqual([x["key"] for x in full["forecast"]["late"]["items"]], [t["key"] for t in staff["tasks"] if t["late_by_days"]][:5])
        text = json.dumps(full)
        for word in ("busy with", "queue", "Bob", "bob", "explanation", "why", "assignee", "people", "capacity"):
            self.assertNotIn(word, text, word)

    def test_late_tasks_are_listed_worst_first_and_at_most_five(self):
        self.now = at(16)
        for n in range(7):
            self.make_task("WEB", "Late %d" % n, due_date="2026-10-%02d" % (1 + n), estimate_hours=2)
        late = self.report()["forecast"]["late"]
        self.assertEqual((late["count"], len(late["items"]), late["more"]), (8, 5, 3))
        self.assertEqual([x["late_by_days"] for x in late["items"]], sorted((x["late_by_days"] for x in late["items"]), reverse=True))

    def test_too_much_open_work_to_project_is_said_plainly_and_the_rest_still_works(self):
        with mock.patch.object(projection, "MAX_TASKS", 2):
            full = self.report()
        self.assertEqual(full["forecast"], {"state": "unavailable", "headline": "A forecast is not available right now."})
        self.assertEqual(full["headline"], "Project WEB is at risk. 2 of 7 tasks are done (29%); 1 finished in the last 7 days.")
        self.assertEqual((full["milestones"]["items"][0]["projected_finish"], full["milestones"]["items"][0]["late_by_days"]), (None, None))
        self.assertEqual(full["completed"]["count"], 1)

    def test_the_summary_level_never_runs_the_projection(self):
        self.share(self.acme, "WEB", "summary")
        with mock.patch.object(projection, "compute", side_effect=AssertionError("not for a summary")):
            self.assertEqual(self.ok(self.client("rita").get("/api/portal/projects/WEB"))["level"], "summary")
            self.assertEqual(self.ok(self.client("rita").get("/api/portal/projects"))["items"][0]["key"], "WEB")


class TimeZoneTests(WeekCase):
    def test_the_week_is_the_week_where_the_organisation_is(self):
        import dataclasses

        from zylker_pm.app import App

        self.config = dataclasses.replace(self.config, timezone="Asia/Kolkata")  # UTC+5:30
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.a = self.client("alice")
        self.now = datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)  # 00:30 on the 11th in Kolkata
        self.make_task("WEB", "Just inside", status="done")  # WEB-9
        self.now = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)  # 23:30 on the 10th
        self.make_task("WEB", "Just outside", status="done")  # WEB-10
        self.now = datetime(2026, 10, 16, 20, 30, tzinfo=timezone.utc)  # 02:00 on the 17th: the 17th is today now
        report = self.ok(self.client("rita").get("/api/portal/projects/WEB"))
        self.assertEqual((report["as_of"], report["period"]["from"], report["period"]["to"]), ("2026-10-17", "2026-10-11", "2026-10-17"))
        self.assertEqual([(i["key"], i["completed_on"]) for i in report["completed"]["items"]], [("WEB-2", "2026-10-12"), ("WEB-9", "2026-10-11")], "WEB-10, at 23:30 on the 10th, is out")


# ------------------------------------------------------------------------------------------------ the oracle


class OracleTests(ClientCase):
    """Random projects and a second implementation of every list, written from the documentation and fed only by the other endpoints."""

    def build(self, rng, key="RND"):
        self.now = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
        self.make_project(key, members=("bob", "carol"), start_date="2026-09-01", due_date="2026-12-01")
        days = ["2026-10-%02d" % d for d in range(1, 32)] + ["2026-11-%02d" % d for d in range(1, 15)]
        numbers = 0
        while self.now < datetime(2026, 10, 16, 9, tzinfo=timezone.utc):
            self.now += timedelta(hours=rng.randint(0, 40))
            if self.now > datetime(2026, 10, 16, 9, tzinfo=timezone.utc):
                break
            op = rng.choice(["new", "new", "new", "status", "status", "due", "due", "sub"])
            if op == "new" or numbers == 0:
                body = {"title": "Task %d" % (numbers + 1), "estimate_hours": rng.choice([1, 2, 5, 8])}
                if rng.random() < 0.8:
                    body["due_date"] = rng.choice(days)
                if rng.random() < 0.15:
                    body["status"] = rng.choice(["done", "cancelled"])
                if self.client("alice").post("/api/projects/%s/tasks" % key, body).status == 201:
                    numbers += 1
                continue
            k = "%s-%d" % (key, rng.randint(1, numbers))
            if op == "status":
                self.set_status(k, rng.choice(["todo", "in_progress", "blocked", "done", "cancelled"]))
            elif op == "due":
                self.client("alice").patch("/api/tasks/" + k, {"due_date": rng.choice(days)})
            else:
                if self.client("alice").post("/api/projects/%s/tasks" % key, {"title": "Sub of %s" % k, "parent": k, "due_date": rng.choice(days)}).status == 201:
                    numbers += 1
        self.now = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)

    def listing(self, key):
        items = self.ok(self.a.get("/api/tasks", query={"project": key, "limit": 200}))["items"]
        return [t for t in items if t["subtasks"]["total"] == 0]

    def history(self, key):
        out, before = [], None
        while True:
            page = self.ok(self.a.get("/api/projects/%s/activity" % key, query={"limit": 200, **({"before": before} if before else {})}))
            out += page["items"]
            before = page["next_before"]
            if not before:
                return out[::-1]  # oldest first

    @staticmethod
    def day(stamp):
        return date.fromisoformat(stamp[:10])  # the organisation is on UTC here

    def expected(self, key, days):
        today = date(2026, 10, 16)
        first = today - timedelta(days=days - 1)
        inside = lambda stamp: bool(stamp) and first <= self.day(stamp) <= today  # noqa: E731
        number = lambda k: int(k.split("-")[1])  # noqa: E731
        leaves = self.listing(key)
        counted = [t for t in leaves if t["status"] != "cancelled"]
        open_ = [t for t in counted if t["status"] in ("todo", "in_progress", "in_review", "blocked")]
        created = {t["key"]: t["created_at"] for t in leaves}
        due_moves = {}
        for entry in self.history(key):
            if entry["action"] in ("task.updated", "task.status_changed") and "due_date" in entry["details"].get("changes", {}) and inside(entry["created_at"]):
                old, new = entry["details"]["changes"]["due_date"]
                due_moves[entry["task"]] = (due_moves.get(entry["task"], (old, None))[0], new)
        open_keys = {t["key"] for t in open_}
        moved = {}
        for k, (old, new) in due_moves.items():
            if k in open_keys and old and new and old != new and not inside(created[k]):
                moved[k] = (date.fromisoformat(new) - date.fromisoformat(old)).days
        return {
            "total": len(counted), "done": sum(1 for t in counted if t["status"] == "done"), "open": len(open_),
            "completed": [t["key"] for t in sorted((t for t in counted if t["status"] == "done" and inside(t["completed_at"])), key=lambda t: (self.day(t["completed_at"]), number(t["key"])), reverse=True)],
            "added": sorted((t["key"] for t in counted if inside(t["created_at"])), key=number),
            "cancelled": sorted((t["key"] for t in leaves if t["status"] == "cancelled" and inside(t["completed_at"])), key=number),
            "overdue": [t["key"] for t in sorted((t for t in open_ if t["due_date"] and t["due_date"] < today.isoformat()), key=lambda t: (t["due_date"], number(t["key"])))],
            "upcoming": [t["key"] for t in sorted((t for t in open_ if t["due_date"] and today.isoformat() <= t["due_date"] <= (today + timedelta(days=13)).isoformat()), key=lambda t: (t["due_date"], number(t["key"])))],
            "blocked": sorted((t["key"] for t in open_ if t["status"] == "blocked"), key=number),
            "moved": sorted(moved.items(), key=lambda kv: (-kv[1], number(kv[0]))),
        }

    def test_the_portal_agrees_with_the_oracle_on_random_projects(self):
        seen = {}
        for seed in range(1, 9):
            with self.subTest(seed=seed):
                key, user = "R%d" % seed, "rita%d" % seed
                self.build(random.Random(seed), key)
                acme = self.new_client("Acme %d" % seed)
                self.share(acme, key, "full")
                self.contact(acme, user, "Rita")
                for days in (7, 30):
                    want, context = self.expected(key, days), (seed, days)
                    for name, rows in want.items():
                        seen[name] = seen.get(name, 0) + (len(rows) if isinstance(rows, (list, dict)) else 0)
                    got = self.ok(self.client(user).get("/api/portal/projects/" + key, query={"days": days}))
                    tasks = got["progress"]["tasks"]
                    self.assertEqual((tasks["total"], tasks["done"], tasks["open"]), (want["total"], want["done"], want["open"]), context)
                    for name, section in (("completed", got["completed"]), ("added", got["changes"]["added"]), ("cancelled", got["changes"]["cancelled"]),
                                          ("overdue", got["attention"]["overdue"]), ("upcoming", got["upcoming"]), ("blocked", got["attention"]["blocked"])):
                        self.assertEqual([i["key"] for i in section["items"]], want[name][:10], (name, *context))
                        self.assertEqual((section["count"], section["more"]), (len(want[name]), max(len(want[name]) - 10, 0)), (name, *context))
                    self.assertEqual((got["progress"]["period"]["completed"], got["progress"]["period"]["added"]), (len(want["completed"]), len(want["added"])), context)
                    moved = got["changes"]["rescheduled"]
                    self.assertEqual([(i["key"], i["days"]) for i in moved["items"]], want["moved"][:10], context)
                    self.assertEqual((moved["count"], moved["later"], moved["earlier"]), (len(want["moved"]), sum(1 for _, v in want["moved"] if v > 0), sum(1 for _, v in want["moved"] if v < 0)), context)
                self.assertGreater(want["total"], 3, "the random project has some substance")
        for name in ("completed", "added", "cancelled", "overdue", "upcoming", "blocked", "moved"):
            self.assertGreater(seen[name], 5, "the random projects hardly ever have anything to say about %r, so agreeing on it proves little" % name)
