"""What leaves the application as Microsoft Project XML comes back the same (as far as the format can say it), and the
cycle check the importer uses agrees with the one the API uses."""

import random
from datetime import date, timedelta

from msproject_helpers import MspCase

from zylker_pm.db import connect
from zylker_pm.services import graph

STATUS_AFTER = {"todo": "todo", "blocked": "todo", "in_progress": "in_progress", "in_review": "in_progress", "done": "done", "cancelled": "cancelled"}


def rolled_up(states):
    if all(s in ("done", "cancelled") for s in states):
        return "done" if "done" in states else "cancelled"
    return "todo" if all(s == "todo" for s in states) else "in_progress"


class RoundTrip(MspCase):
    def snapshot(self, key):
        """Everything the format can carry, by task title; a parent's status is what its subtasks add up to."""
        rows = {t["key"]: t for t in self.tasks(key)}
        kids = {k: [c for c, t in rows.items() if t["parent"] == k] for k in rows}
        mapped = {}

        def status(k):
            if k not in mapped:
                mapped[k] = rolled_up([status(c) for c in kids[k]]) if kids[k] else STATUS_AFTER[rows[k]["status"]]
            return mapped[k]

        def depth(k):
            return 1 + (depth(rows[k]["parent"]) if rows[k]["parent"] else 0)

        out = {}
        for k, t in rows.items():
            detail = self.ok(self.alice.get("/api/tasks/" + k))
            leaf = not kids[k]
            out[t["title"]] = {
                "parent": rows[t["parent"]]["title"] if t["parent"] else None,
                "type": "epic" if kids[k] and depth(k) == 1 else "story" if kids[k] and depth(k) == 2 else "task",
                "status": status(k), "priority": t["priority"], "estimate": t["estimate_hours"] if leaf else None,
                "assignee": t["assignee"] and t["assignee"]["username"], "start": t["start_date"], "due": t["due_date"],
                "milestone": t["milestone"] and t["milestone"]["name"], "description": t["description"],
                "blocked_by": sorted((rows[b["key"]]["title"], b["type"], b["lag_days"]) for b in detail["blocked_by"]),
            }  # fmt: skip
        return out

    def copy_to(self, key, source="IMP"):
        self.make_project(key, members=("bob", "carol"))
        self.run_import(self.export(source).body, key=key, add_missing_members="true")
        return self.snapshot(key)

    def test_a_rich_project_comes_back_whole(self):
        self.make_task("IMP", "Epic", type="epic", description="Everything\nin one place", priority="critical")
        self.make_task("IMP", "Design", parent="IMP-1", assignee="bob", estimate_hours=16, start_date="2026-11-02", due_date="2026-11-05", priority="high", status="done")
        self.make_task("IMP", "Build", parent="IMP-1", assignee="carol", estimate_hours=40.25, due_date="2026-11-20", status="in_progress", priority="low")
        self.make_task("IMP", "Weekend start", start_date="2026-11-07", due_date="2026-11-09", status="in_review", estimate_hours=0.01)
        self.make_task("IMP", "Parked", status="blocked")
        self.make_task("IMP", "Dropped", status="cancelled", assignee="bob")
        self.make_task("IMP", "Sub of sub", parent="IMP-3")
        gate = self.ok(self.alice.post("/api/projects/IMP/milestones", {"name": "Beta", "due_date": "2026-11-23", "description": "Usable"}), 201)
        self.ok(self.alice.patch("/api/tasks/IMP-3", {"milestone": gate["id"]}))
        self.ok(self.alice.patch("/api/tasks/IMP-4", {"milestone": gate["id"]}))
        for blocked, blocker, kind, lag in (("IMP-3", "IMP-2", "finish_to_start", 2), ("IMP-4", "IMP-3", "start_to_start", -1), ("IMP-5", "IMP-3", "finish_to_finish", 0), ("IMP-6", "IMP-2", "finish_to_start", 0)):
            self.ok(self.alice.post("/api/tasks/%s/dependencies" % blocked, {"blocked_by": blocker, "type": kind, "lag_days": lag}))
        before = self.snapshot("IMP")
        after = self.copy_to("COPY")
        self.assertEqual(after, before)
        self.assertEqual(after["Weekend start"]["start"], "2026-11-07")  # a Saturday: kept, although the file's own start is the Monday
        self.assertEqual([(m["name"], m["due_date"], m["description"], m["tasks_total"]) for m in self.milestones("COPY")], [("Beta", "2026-11-23", "Usable", 2)])

    def test_the_copy_of_a_copy_is_the_same_again(self):
        self.make_task("IMP", "A", estimate_hours=3, due_date="2026-12-01", assignee="bob")
        self.make_task("IMP", "B", parent="IMP-1", status="done")
        first = self.copy_to("ONE")
        second = self.copy_to("TWO", source="ONE")
        self.assertEqual(second, first)

    def test_an_estimate_of_exactly_nothing_comes_back_as_no_estimate(self):
        self.make_task("IMP", "Zero", estimate_hours=0)
        self.assertEqual(self.copy_to("COPY")["Zero"]["estimate"], None)

    def test_importing_your_own_export_into_the_same_project_changes_nothing(self):
        self.make_task("IMP", "A", estimate_hours=3, due_date="2026-12-01")
        self.make_task("IMP", "B", parent="IMP-1")
        self.make_task("IMP", "C")
        self.ok(self.alice.post("/api/tasks/IMP-3/dependencies", {"blocked_by": "IMP-2"}))
        gate = self.ok(self.alice.post("/api/projects/IMP/milestones", {"name": "Gate", "due_date": "2026-12-01"}), 201)
        self.ok(self.alice.patch("/api/tasks/IMP-2", {"milestone": gate["id"]}))
        body = self.export().body
        before = self.snapshot("IMP")
        report = self.run_import(body)
        self.assertEqual((report["created"], report["skipped_duplicates"], report["milestones"], report["links"], report["errors"], report["warnings"]), (0, 3, 0, 0, [], []))
        self.assertEqual(self.snapshot("IMP"), before)

    def test_edits_made_in_the_other_tool_are_not_applied_but_new_tasks_are_added_where_they_belong(self):
        self.make_task("IMP", "A", status="todo")
        self.make_task("IMP", "B", parent="IMP-1")
        body = self.export().body.decode("utf-8")
        body = body.replace("<Name>A</Name>", "<Name>Renamed in Project</Name>")  # an edit: ignored, the import only adds
        added = ("<Task><UID>3</UID><ID>9</ID><Name>Added in Project</Name><OutlineLevel>2</OutlineLevel><Duration>PT16H0M0S</Duration>"
                 "<PredecessorLink><PredecessorUID>2</PredecessorUID><Type>1</Type><LinkLag>0</LinkLag></PredecessorLink></Task>")  # fmt: skip
        body = body.replace("</Tasks>", added + "</Tasks>", 1)
        report = self.run_import(body.encode("utf-8"))
        self.assertEqual((report["created"], report["skipped_duplicates"], report["links"], report["errors"]), (1, 2, 1, []))
        titles = {t["key"]: t["title"] for t in self.tasks()}
        self.assertEqual(titles, {"IMP-1": "A", "IMP-2": "B", "IMP-3": "Added in Project"})
        new = self.by_title()["Added in Project"]
        self.assertEqual((new["parent"], new["estimate_hours"]), ("IMP-1", 16.0))
        self.assertEqual(self.blockers("IMP-3"), [("IMP-2", "finish_to_start", 0)])

    def test_a_file_from_this_application_does_not_invent_dates(self):
        self.make_task("IMP", "Dateless")
        self.make_task("IMP", "Only due", due_date="2026-12-01")
        after = self.copy_to("COPY")
        self.assertEqual((after["Dateless"]["start"], after["Dateless"]["due"]), (None, None))
        self.assertEqual((after["Only due"]["start"], after["Only due"]["due"]), (None, "2026-12-01"))  # its start was only a suggestion

    def test_random_projects_round_trip(self):
        for seed in range(25):
            rng = random.Random(seed)
            key = "R%d" % seed
            self.make_project(key, members=("bob", "carol"))
            n = rng.randint(2, 14)
            keys, level = [], {}
            for i in range(n):
                parent = rng.choice([k for k in keys if level[k] < 5]) if keys and rng.random() < 0.45 else None
                start = date(2026, 10, 1) + timedelta(days=rng.randrange(120)) if rng.random() < 0.5 else None
                body = {"title": "T%d" % i, "priority": rng.choice(["low", "medium", "high", "critical"]), "assignee": rng.choice([None, "bob", "carol"])}
                if parent:
                    body["parent"] = parent
                if rng.random() < 0.7:
                    body["estimate_hours"] = rng.choice([0.25, 1, 2.5, 8, 13.75, 40])
                if start:
                    body["start_date"] = start.isoformat()
                if rng.random() < 0.6:
                    body["due_date"] = ((start or date(2026, 10, 1)) + timedelta(days=rng.randrange(30))).isoformat()
                if rng.random() < 0.5:
                    body["description"] = "d%d\n<&>" % i
                task_key = self.ok(self.alice.post("/api/projects/%s/tasks" % key, body), 201)["key"]
                keys.append(task_key)
                level[task_key] = level.get(parent, 0) + 1
            parents = {t["parent"] for t in self.tasks(key) if t["parent"]}
            for k in keys:  # statuses last: a closed parent would refuse its open subtasks
                if k not in parents:
                    status = rng.choice(["todo", "in_progress", "in_review", "blocked", "done", "cancelled"])
                    if status != "todo":
                        self.client("alice").patch("/api/tasks/" + k, {"status": status})
            linked = set()
            for _ in range(rng.randint(0, 12)):
                a, b = rng.sample(keys, 2)
                if (b, a) in linked:  # one dependency per pair of tasks: the format and the import keep a single one
                    continue
                answer = self.alice.post("/api/tasks/%s/dependencies" % a, {"blocked_by": b, "type": rng.choice(["finish_to_start", "start_to_start", "finish_to_finish"]), "lag_days": rng.randint(-3, 5)})
                if answer.status == 200:
                    linked.add((a, b))
            for m in range(rng.randint(0, 3)):
                gate = self.ok(self.alice.post("/api/projects/%s/milestones" % key, {"name": "M%d" % m, "due_date": (date(2026, 11, 1) + timedelta(days=rng.randrange(60))).isoformat()}), 201)
                for k in rng.sample(keys, rng.randint(0, min(3, len(keys)))):
                    self.client("alice").patch("/api/tasks/" + k, {"milestone": gate["id"]})
            expected = self.snapshot(key)
            self.assertEqual(self.copy_to("C%d" % seed, source=key), expected, "seed %d" % seed)


class CycleGuardTests(MspCase):
    def test_the_guard_accepts_and_refuses_exactly_what_the_api_does(self):
        kinds = ["finish_to_start", "start_to_start", "finish_to_finish"]
        for seed in range(12):
            rng = random.Random(seed)
            key = "G%d" % seed
            self.make_project(key, members=())
            keys = []
            for i in range(rng.randint(4, 9)):
                parent = rng.choice(keys) if keys and rng.random() < 0.4 else None
                body = {"title": "T%d" % i, **({"parent": parent} if parent else {})}
                keys.append(self.ok(self.alice.post("/api/projects/%s/tasks" % key, body), 201)["key"])
            ids = {t["key"]: t["id"] for t in self.alice_tasks_with_ids(key)}
            tries, seen = [], set()
            for _ in range(60):
                a, b = rng.sample(keys, 2)
                if frozenset((a, b)) not in seen:
                    seen.add(frozenset((a, b)))
                    tries.append((a, b, rng.choice(kinds)))
            conn = connect(self.config.db_path)
            try:
                project_id = conn.execute("SELECT id FROM projects WHERE key = ?", (key,)).fetchone()[0]
                guard = graph.CycleGuard(conn, project_id)
                guarded = [guard.add((ids[a], ids[b], kind)) is None for a, b, kind in tries]
            finally:
                conn.close()
            asked = [self.alice.post("/api/tasks/%s/dependencies" % b, {"blocked_by": a, "type": kind}).status == 200 for a, b, kind in tries]
            self.assertEqual(guarded, asked, "seed %d" % seed)
            self.assertTrue(any(asked) and not all(asked), "seed %d should have both accepted and refused edges" % seed)

    def alice_tasks_with_ids(self, key):
        conn = connect(self.config.db_path)
        try:
            return [{"key": "%s-%d" % (key, r["number"]), "id": r["id"]} for r in conn.execute("SELECT id, number FROM tasks WHERE project_id = (SELECT id FROM projects WHERE key = ?)", (key,))]
        finally:
            conn.close()

    def test_the_loop_in_the_message_is_the_one_the_api_reports(self):
        self.make_project("LOOP", members=())
        for i in range(3):
            self.make_task("LOOP", "T%d" % i)
        for blocked, blocker in (("LOOP-2", "LOOP-1"), ("LOOP-3", "LOOP-2")):
            self.ok(self.alice.post("/api/tasks/%s/dependencies" % blocked, {"blocked_by": blocker}))
        api = self.alice.post("/api/tasks/LOOP-1/dependencies", {"blocked_by": "LOOP-3"})
        ids = {t["key"]: t["id"] for t in self.alice_tasks_with_ids("LOOP")}
        conn = connect(self.config.db_path)
        try:
            guard = graph.CycleGuard(conn, conn.execute("SELECT id FROM projects WHERE key = 'LOOP'").fetchone()[0])
            loop = guard.add((ids["LOOP-3"], ids["LOOP-1"], "finish_to_start"))
        finally:
            conn.close()
        names = {v: k for k, v in ids.items()}
        self.assertEqual([names[i] for i in loop], api.json["error"]["details"]["cycle"])
