"""``GET /api/projects/{key}/dependencies``: every dependency of one project in one answer (what the timeline draws)."""

import random
from unittest import mock

from helpers import ApiTestCase
from msproject_helpers import doc, task

from zylker_pm.services import deps


class Base(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.alice = self.client("alice")

    def listing(self, key="WEB", user="alice", **kw):
        return self.ok(self.client(user).get("/api/projects/%s/dependencies" % key, **kw))

    def link(self, blocked, blocker, **extra):
        return self.ok(self.alice.post("/api/tasks/%s/dependencies" % blocked, {"blocked_by": blocker, **extra}))

    def edges(self, key="WEB"):
        return [(i["blocker"]["key"], i["blocked"]["key"]) for i in self.listing(key)["items"]]

    def task(self, key):
        return self.ok(self.alice.get("/api/tasks/" + key))


class ShapeTests(Base):
    def test_a_project_without_dependencies_lists_nothing(self):
        self.make_task("WEB", "Alone")
        self.assertEqual(self.listing(), {"project": "WEB", "items": [], "truncated": False})

    def test_every_link_comes_with_both_ends_its_type_and_what_the_dates_imply(self):
        self.make_task("WEB", "Design", start_date="2026-10-05", due_date="2026-10-09")
        self.make_task("WEB", "Build", start_date="2026-10-19", due_date="2026-10-23", assignee="bob")
        self.make_task("WEB", "Undated")
        self.link("WEB-2", "WEB-1", lag_days=2)
        self.link("WEB-3", "WEB-2", type="start_to_start")
        self.assertEqual(
            self.listing(),
            {
                "project": "WEB",
                "truncated": False,
                "items": [
                    {
                        "blocker": {"key": "WEB-1", "title": "Design", "status": "todo", "start_date": "2026-10-05", "due_date": "2026-10-09"},
                        "blocked": {"key": "WEB-2", "title": "Build", "status": "todo", "start_date": "2026-10-19", "due_date": "2026-10-23"},
                        "type": "finish_to_start",
                        "lag_days": 2,
                        "holding": True,
                        "schedule": {"applies_to": "start", "not_before": "2026-10-11", "conflict": False},
                    },
                    {
                        "blocker": {"key": "WEB-2", "title": "Build", "status": "todo", "start_date": "2026-10-19", "due_date": "2026-10-23"},
                        "blocked": {"key": "WEB-3", "title": "Undated", "status": "todo", "start_date": None, "due_date": None},
                        "type": "start_to_start",
                        "lag_days": 0,
                        "holding": True,
                        "schedule": {"applies_to": "start", "not_before": "2026-10-19", "conflict": False},
                    },
                ],
            },
        )

    def test_a_link_that_the_planned_dates_break_says_so(self):
        self.make_task("WEB", "Design", start_date="2026-10-05", due_date="2026-10-20")
        self.make_task("WEB", "Build", start_date="2026-10-12", due_date="2026-10-30")
        self.link("WEB-2", "WEB-1")
        (item,) = self.listing()["items"]
        self.assertEqual(item["schedule"], {"applies_to": "start", "not_before": "2026-10-20", "conflict": True})

    def test_no_schedule_when_the_blocker_lacks_the_date_the_link_hangs_on(self):
        self.make_task("WEB", "Design", start_date="2026-10-05")  # no due date, finish-to-start hangs on it
        self.make_task("WEB", "Build", start_date="2026-10-12")
        self.link("WEB-2", "WEB-1")
        self.assertIsNone(self.listing()["items"][0]["schedule"])

    def test_links_come_in_blocker_then_blocked_order(self):
        for i in range(5):
            self.make_task("WEB", "T%d" % (i + 1))
        for blocked, blocker in (("WEB-5", "WEB-1"), ("WEB-3", "WEB-2"), ("WEB-4", "WEB-2"), ("WEB-3", "WEB-1"), ("WEB-5", "WEB-3")):
            self.link(blocked, blocker)
        self.assertEqual(self.edges(), [("WEB-1", "WEB-3"), ("WEB-1", "WEB-5"), ("WEB-2", "WEB-3"), ("WEB-2", "WEB-4"), ("WEB-3", "WEB-5")])

    def test_links_to_and_from_parent_tasks_are_listed_like_any_other(self):
        self.make_task("WEB", "Epic", type="epic")
        self.make_task("WEB", "Child", parent="WEB-1")
        self.make_task("WEB", "Other")
        self.link("WEB-3", "WEB-1")
        self.link("WEB-3", "WEB-2", type="finish_to_finish")
        self.assertEqual(self.edges(), [("WEB-1", "WEB-3"), ("WEB-2", "WEB-3")])

    def test_only_the_links_of_that_project_are_listed(self):
        self.make_project("OPS", members=("bob",))
        for key in ("WEB", "OPS"):
            self.make_task(key, "First")
            self.make_task(key, "Second")
        self.link("WEB-2", "WEB-1")
        self.assertEqual(self.edges("WEB"), [("WEB-1", "WEB-2")])
        self.assertEqual(self.edges("OPS"), [])
        self.ok(self.alice.post("/api/tasks/OPS-2/dependencies", {"blocked_by": "OPS-1", "type": "finish_to_finish", "lag_days": -1}))
        self.assertEqual(self.edges("OPS"), [("OPS-1", "OPS-2")])
        self.assertEqual(self.edges("WEB"), [("WEB-1", "WEB-2")])

    def test_removing_and_changing_a_link_shows_in_the_next_answer(self):
        for _ in range(3):
            self.make_task("WEB", "T")
        self.link("WEB-2", "WEB-1")
        self.link("WEB-3", "WEB-2")
        self.ok(self.alice.patch("/api/tasks/WEB-2/dependencies/WEB-1", {"type": "start_to_start", "lag_days": 3}))
        first = self.listing()["items"][0]
        self.assertEqual((first["type"], first["lag_days"]), ("start_to_start", 3))
        self.ok(self.alice.delete("/api/tasks/WEB-3/dependencies/WEB-2"))
        self.assertEqual(self.edges(), [("WEB-1", "WEB-2")])


class HoldingTests(Base):
    """``holding`` is the rule behind ``blocked_by_open``, written out here independently of the code."""

    PATHS = {"todo": [], "in_progress": ["in_progress"], "in_review": ["in_progress", "in_review"], "blocked": ["blocked"], "done": ["done"], "cancelled": ["cancelled"]}
    # Does a blocker in this status still hold the blocked task back, per link type?
    HOLDS = {
        "finish_to_start": {"todo": True, "in_progress": True, "in_review": True, "blocked": True, "done": False, "cancelled": False},
        "finish_to_finish": {"todo": True, "in_progress": True, "in_review": True, "blocked": True, "done": False, "cancelled": False},
        "start_to_start": {"todo": True, "in_progress": False, "in_review": False, "blocked": True, "done": False, "cancelled": False},
    }

    def test_the_three_types_against_every_status_of_the_blocker(self):
        expected = {}
        for kind, by_status in self.HOLDS.items():
            for status, holds in by_status.items():
                blocker, blocked = self.make_task("WEB", "Blocker %s %s" % (kind, status))["key"], self.make_task("WEB", "Blocked")["key"]
                self.link(blocked, blocker, type=kind)
                for step in self.PATHS[status]:
                    self.ok(self.set_status(blocker, step))
                expected[(blocker, blocked)] = holds
        listing = {(i["blocker"]["key"], i["blocked"]["key"]): i for i in self.listing()["items"]}
        self.assertEqual(len(listing), 18)
        for edge, holds in expected.items():
            item = listing[edge]
            self.assertEqual(item["holding"], holds, (item["type"], item["blocker"]["status"]))
            self.assertEqual(self.task(edge[1])["blocked_by_open"], 1 if holds else 0)

    def test_a_task_that_is_already_closed_is_not_being_held_back(self):
        self.make_task("WEB", "Blocker")
        self.make_task("WEB", "Done already")
        self.make_task("WEB", "Cancelled already")
        self.ok(self.set_status("WEB-2", "done"))
        self.ok(self.set_status("WEB-3", "cancelled"))
        self.link("WEB-2", "WEB-1")  # linking a finished task is allowed; the blocker is still open
        self.link("WEB-3", "WEB-1")
        self.assertEqual([i["holding"] for i in self.listing()["items"]], [False, False])
        self.ok(self.set_status("WEB-2", "todo"))  # reopening it makes the link bite again
        self.assertEqual([i["holding"] for i in self.listing()["items"]], [True, False])


class AgreementTests(Base):
    """The answer has to say exactly what the task dialog says about the same links."""

    DATES = [None, "2026-10-01", "2026-10-06", "2026-10-12", "2026-10-15", "2026-10-20", "2026-10-31", "2026-11-03"]
    TASKS, LINKS = 14, 22

    def build(self, key, seed):
        rng = random.Random(seed)
        self.make_project(key, members=("bob", "carol"))
        for n in range(self.TASKS):
            start, due = rng.choice(self.DATES), rng.choice(self.DATES)
            if start and due and start > due:
                start, due = due, start
            self.make_task(key, "Task %d" % (n + 1), **{k: v for k, v in (("start_date", start), ("due_date", due)) if v})
        added = 0
        while added < self.LINKS:
            a, b = rng.sample(range(1, self.TASKS + 1), 2)
            response = self.alice.post(
                "/api/tasks/%s-%d/dependencies" % (key, a),
                {"blocked_by": "%s-%d" % (key, b), "type": rng.choice(["finish_to_start", "start_to_start", "finish_to_finish"]), "lag_days": rng.randint(-4, 6)},
            )
            added += response.status == 200  # refused: it would be a loop, or the pair is already linked

    def test_listing_and_task_dialog_agree_on_random_projects(self):
        for seed in (1, 2, 3):
            key = "AG%d" % seed
            with self.subTest(seed=seed):
                self.build(key, seed)
                items = self.listing(key)["items"]
                self.assertEqual(len(items), self.LINKS)
                by_blocker, by_blocked = {}, {}
                for i in items:
                    entry = {"type": i["type"], "lag_days": i["lag_days"], "schedule": i["schedule"]}
                    by_blocker.setdefault(i["blocker"]["key"], {})[i["blocked"]["key"]] = entry
                    by_blocked.setdefault(i["blocked"]["key"], {})[i["blocker"]["key"]] = entry
                    for end in ("blocker", "blocked"):  # the ends say what the task itself says
                        t = self.task(i[end]["key"])
                        fields = ("title", "status", "start_date", "due_date")
                        self.assertEqual({k: t[k] for k in fields}, {k: i[end][k] for k in fields})
                for n in range(1, self.TASKS + 1):
                    name = "%s-%d" % (key, n)
                    detail = self.task(name)
                    simple = {e["key"]: {k: e[k] for k in ("type", "lag_days", "schedule")} for e in detail["blocks"]}
                    self.assertEqual(simple, by_blocker.get(name, {}))
                    simple = {e["key"]: {k: e[k] for k in ("type", "lag_days", "schedule")} for e in detail["blocked_by"]}
                    self.assertEqual(simple, by_blocked.get(name, {}))
                    held = sum(1 for i in items if i["blocked"]["key"] == name and i["holding"])
                    self.assertEqual(detail["blocked_by_open"], held, name)  # every task here is still open


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        self.make_task("WEB", "A")
        self.make_task("WEB", "B")
        self.link("WEB-2", "WEB-1")

    def test_everybody_who_is_signed_in_can_read_it(self):
        for user in ("admin", "alice", "bob", "carol", "vera"):
            items = self.listing(user=user)["items"]
            self.assertEqual([(i["blocker"]["key"], i["blocked"]["key"]) for i in items], [("WEB-1", "WEB-2")], user)

    def test_not_without_signing_in(self):
        r = self.client().get("/api/projects/WEB/dependencies")
        self.assertEqual((r.status, r.error_code), (401, "unauthorized"))
        r = self.client(token="nonsense").get("/api/projects/WEB/dependencies")
        self.assertEqual(r.status, 401)

    def test_an_unknown_project_is_a_404_and_the_key_may_be_written_in_lower_case(self):
        r = self.alice.get("/api/projects/NOPE/dependencies")
        self.assertEqual((r.status, r.error_code), (404, "project_not_found"))
        self.assertEqual(self.alice.get("/api/projects/web/dependencies").json["project"], "WEB")

    def test_only_get_exists(self):
        for method in ("POST", "PATCH", "DELETE"):
            r = self.alice.request(method, "/api/projects/WEB/dependencies", {})
            self.assertEqual(r.status, 405, method)

    def test_reading_writes_no_history_and_changes_nothing(self):
        before = self.ok(self.alice.get("/api/projects/WEB/activity", query={"limit": 200}))
        updated = self.task("WEB-2")["updated_at"]
        for _ in range(3):
            self.listing()
            self.listing(user="vera")
        self.assertEqual(self.ok(self.alice.get("/api/projects/WEB/activity", query={"limit": 200})), before)
        self.assertEqual(self.task("WEB-2")["updated_at"], updated)

    def test_archived_projects_can_still_be_read(self):
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "archived"}))
        self.assertEqual(self.edges(), [("WEB-1", "WEB-2")])


class LimitTests(Base):
    def test_a_project_with_more_links_than_one_answer_carries_says_so(self):
        for _ in range(4):
            self.make_task("WEB", "T")
        for blocked, blocker in (("WEB-2", "WEB-1"), ("WEB-3", "WEB-2"), ("WEB-4", "WEB-3")):
            self.link(blocked, blocker)
        with mock.patch.object(deps, "MAX_LISTED_DEPENDENCIES", 3):
            answer = self.listing()
            self.assertEqual((len(answer["items"]), answer["truncated"]), (3, False))
        with mock.patch.object(deps, "MAX_LISTED_DEPENDENCIES", 2):
            answer = self.listing()
            self.assertEqual((len(answer["items"]), answer["truncated"]), (2, True))
            self.assertEqual([(i["blocker"]["key"], i["blocked"]["key"]) for i in answer["items"]], [("WEB-1", "WEB-2"), ("WEB-2", "WEB-3")])

    def test_the_real_limit_is_generous(self):
        self.assertGreaterEqual(deps.MAX_LISTED_DEPENDENCIES, 5000)


class ImportedLinkTests(Base):
    def test_links_that_came_in_from_a_microsoft_project_file_are_listed_with_their_type_and_lag(self):
        data = doc(
            task(1, "Foundation", 1, Start="2026-10-05T08:00:00", Finish="2026-10-09T17:00:00"),
            task(2, "Walls", 1, Start="2026-10-12T08:00:00", Finish="2026-10-16T17:00:00", links=[(1, 1, 4800)]),
            task(3, "Roof", 1, Start="2026-10-12T08:00:00", Finish="2026-10-20T17:00:00", links=[(2, 3), (1, 0, 9600)]),
        )
        report = self.ok(self.alice.request("POST", "/api/projects/WEB/import/msproject", raw=data, content_type="application/xml"))
        self.assertEqual(report["links"], 3)
        items = {(i["blocker"]["title"], i["blocked"]["title"]): (i["type"], i["lag_days"]) for i in self.listing()["items"]}
        self.assertEqual(
            items,
            {
                ("Foundation", "Walls"): ("finish_to_start", 1),
                ("Walls", "Roof"): ("start_to_start", 0),
                ("Foundation", "Roof"): ("finish_to_finish", 2),
            },
        )
