"""Project templates: saving a project's shape, previewing and starting new projects from it."""

import json
import os
import sqlite3
from unittest import mock

from helpers import ApiTestCase

from zylker_pm import db
from zylker_pm.db import init_db
from zylker_pm.services import templates

TEMPLATE_START = "2026-11-02"  # a Monday four weeks after the source project's start


class TemplateBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.build_source()

    def build_source(self):
        """WEB: starts Monday 2026-10-05, due 2026-12-18; two milestones; a small task tree with dependencies; closed work."""
        self.make_project("WEB", members=("bob", "carol"), start_date="2026-10-05", due_date="2026-12-18", description="The website")
        a = self.client("alice")
        beta = self.ok(a.post("/api/projects/WEB/milestones", {"name": "Beta", "due_date": "2026-11-13"}), 201)["id"]
        launch = self.ok(a.post("/api/projects/WEB/milestones", {"name": "Launch", "due_date": "2026-12-18"}), 201)["id"]
        self.t = {}
        self.t["design"] = self.make_task("WEB", "Design", type="epic", estimate_hours=20, start_date="2026-10-05", due_date="2026-10-16", assignee="bob", labels=["ux"], milestone=beta, priority="high", description="All the design")["key"]
        self.t["wire"] = self.make_task("WEB", "Wireframes", parent=self.t["design"], estimate_hours=8, start_date="2026-10-05", due_date="2026-10-09", assignee="bob")["key"]
        self.t["mock"] = self.make_task("WEB", "Mockups", parent=self.t["design"], estimate_hours=12, start_date="2026-10-12", due_date="2026-10-16", assignee="carol", labels=["ux", "visual"])["key"]
        self.t["build"] = self.make_task("WEB", "Build", type="story", estimate_hours=40, start_date="2026-10-19", due_date="2026-11-13", assignee="alice", milestone=beta)["key"]
        self.t["launch"] = self.make_task("WEB", "Launch checklist", due_date="2026-12-18", milestone=launch, priority="critical")["key"]
        self.t["old"] = self.make_task("WEB", "Old thing", estimate_hours=3, due_date="2026-10-02")["key"]
        self.t["gone"] = self.make_task("WEB", "Cancelled thing")["key"]
        self.dep(self.t["mock"], self.t["wire"])  # mockups wait for wireframes
        self.dep(self.t["build"], self.t["mock"])
        self.dep(self.t["launch"], self.t["old"])  # will be dropped when the closed task is left out
        self.set_status(self.t["old"], "done")
        self.set_status(self.t["gone"], "cancelled")

    def dep(self, task, blocked_by):
        self.ok(self.client("alice").post("/api/tasks/%s/dependencies" % task, {"blocked_by": blocked_by}))

    def save(self, name="Website", who="alice", status=201, project="WEB", **extra):
        r = self.client(who).post("/api/projects/%s/save-as-template" % project, {"name": name, **extra})
        self.assertEqual(r.status, status, r.text)
        return r.json

    def start(self, tid, key="NEW", name="New site", start=TEMPLATE_START, who="alice", status=201, **extra):
        r = self.client(who).post("/api/templates/%d/instantiate" % tid, {"key": key, "name": name, "start_date": start, **extra})
        self.assertEqual(r.status, status, r.text)
        return r.json

    def audit(self, **query):
        return self.ok(self.admin.get("/api/audit", query=query))["items"]

    def tasks_of(self, key):
        items = self.ok(self.client("alice").get("/api/tasks", query={"project": key, "sort": "number", "limit": 200}))["items"]
        return {t["title"]: t for t in items}

    def count(self, table):
        conn = db.connect(self.config.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        finally:
            conn.close()


class SaveTests(TemplateBase):
    def test_what_is_captured(self):
        t = self.save(description="Standard site build")
        self.assertEqual((t["name"], t["description"], t["source_project"], t["anchor_date"]), ("Website", "Standard site build", "WEB", "2026-10-05"))
        self.assertEqual((t["tasks"], t["milestones"], t["dependencies"], t["times_used"]), (5, 2, 2, 0))  # closed work and its dependency are left out
        self.assertEqual(t["project"], {"span_days": 74, "description": "The website"})
        tasks = {x["title"]: x for x in t["task_list"]}
        self.assertEqual([x["ref"] for x in t["task_list"]][:2], ["T1", "T2"])
        design = tasks["Design"]
        self.assertEqual((design["type"], design["priority"], design["estimate_hours"], design["labels"], design["description"]), ("epic", "high", 20, ["ux"], "All the design"))
        self.assertEqual((design["start_offset"], design["due_offset"], design["parent"], design["slot"]), (0, 11, None, "bob"))
        self.assertEqual((tasks["Wireframes"]["parent"], tasks["Mockups"]["parent"]), (design["ref"], design["ref"]))
        self.assertEqual((tasks["Mockups"]["start_offset"], tasks["Mockups"]["labels"]), (7, ["ux", "visual"]))
        self.assertEqual(tasks["Launch checklist"]["start_offset"], None)
        self.assertEqual(tasks["Launch checklist"]["due_offset"], 74)
        milestones = {m["name"]: m for m in t["milestone_list"]}
        self.assertEqual((milestones["Beta"]["offset"], milestones["Launch"]["offset"]), (39, 74))
        self.assertEqual(tasks["Design"]["milestone"], milestones["Beta"]["ref"])
        wire, mock_, build = tasks["Wireframes"]["ref"], tasks["Mockups"]["ref"], tasks["Build"]["ref"]
        self.assertEqual(sorted(t["dependency_list"]), sorted([[wire, mock_], [mock_, build]]))  # [blocker, blocked]
        self.assertEqual([s["name"] for s in t["slots"]], ["alice", "bob", "carol"])

    def test_parents_always_come_before_their_children(self):
        t = self.save()
        seen = set()
        for task in t["task_list"]:
            if task["parent"]:
                self.assertIn(task["parent"], seen)
            seen.add(task["ref"])

    def test_closed_work_can_be_included(self):
        t = self.save(include_closed=True)
        self.assertEqual((t["tasks"], t["dependencies"]), (7, 3))
        titles = [x["title"] for x in t["task_list"]]
        self.assertIn("Old thing", titles)
        self.assertIn("Cancelled thing", titles)

    def test_slots_can_be_named_for_the_role(self):
        t = self.save(slots={"bob": "Designer", "carol": "Visual designer"})
        self.assertEqual([(s["name"], s["source_user"]) for s in t["slots"]], [("alice", "alice"), ("Designer", "bob"), ("Visual designer", "carol")])
        self.assertEqual({x["slot"] for x in t["task_list"]}, {"alice", "Designer", "Visual designer", None})

    def test_bad_slot_names(self):
        for bad in ({"nobody": "x"}, {"bob": "same", "carol": "SAME"}, {"bob": "x" * 41}, {"bob": "bell\x07"}, {"bob": 5}, [], "bob"):
            r = self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "T", "slots": bad})
            self.assertEqual(r.status, 422, bad)
            self.assertIn("slots", r.json["error"]["details"], bad)
        self.assertEqual(self.count("project_templates"), 0)

    def test_the_anchor_day(self):
        t = self.save(anchor_date="2026-10-12")
        design = next(x for x in t["task_list"] if x["title"] == "Design")
        self.assertEqual((t["anchor_date"], design["start_offset"], design["due_offset"]), ("2026-10-12", -7, 4))  # before the anchor is negative
        r = self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "Far", "anchor_date": "2010-01-01"})
        self.assertEqual((r.status, list(r.json["error"]["details"])), (422, ["anchor_date"]))

    def test_the_anchor_falls_back_to_the_earliest_date(self):
        self.make_project("BARE", members=("bob",))
        self.make_task("BARE", "Later", due_date="2026-11-20")
        self.make_task("BARE", "Earlier", start_date="2026-11-02", due_date="2026-11-06")
        t = self.save("Bare", project="BARE")
        self.assertEqual(t["anchor_date"], "2026-11-02")
        self.assertIsNone(t["project"]["span_days"])
        self.make_project("UNDATED", members=("bob",))
        self.make_task("UNDATED", "No dates")
        self.assertEqual(self.save("Undated", project="UNDATED")["anchor_date"], "2026-10-16")  # the day the project was created

    def test_nothing_to_save(self):
        self.make_project("EMPTY", members=("bob",))
        r = self.client("alice").post("/api/projects/EMPTY/save-as-template", {"name": "x"})
        self.assertEqual((r.status, r.error_code), (409, "template_empty"))
        self.make_project("DONE", members=("bob",))
        self.set_status(self.make_task("DONE", "Only closed")["key"], "done")
        self.assertEqual(self.client("alice").post("/api/projects/DONE/save-as-template", {"name": "x"}).error_code, "template_empty")
        self.assertEqual(self.save("with-closed", project="DONE", include_closed=True)["tasks"], 1)

    def test_names_are_unique_ignoring_case(self):
        self.save("Website")
        r = self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "WEBSITE"})
        self.assertEqual((r.status, r.error_code), (409, "template_name_taken"))

    def test_validation(self):
        for bad, field in (({}, "name"), ({"name": ""}, "name"), ({"name": "n" * 81}, "name"), ({"name": "x", "colour": 1}, "colour"), ({"name": "x", "anchor_date": "soon"}, "anchor_date"), ({"name": "x", "include_closed": "yes"}, "include_closed"), ({"name": "x", "description": "d" * 2001}, "description")):
            r = self.client("alice").post("/api/projects/WEB/save-as-template", bad)
            self.assertEqual(r.status, 422, bad)
            self.assertIn(field, r.json["error"]["details"], bad)
        self.assertEqual(self.client("alice").post("/api/projects/NOPE/save-as-template", {"name": "x"}).status, 404)

    def test_who_may_save(self):
        for who in ("bob", "vera"):
            self.assertEqual(self.client(who).post("/api/projects/WEB/save-as-template", {"name": "x"}).status, 403, who)
        self.save("by-admin", who="admin")
        self.assertEqual(self.client().post("/api/projects/WEB/save-as-template", {"name": "x"}).status, 401)

    def test_limits(self):
        with mock.patch.object(templates, "MAX_TASKS", 3):
            r = self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "big"})
            self.assertEqual((r.status, r.error_code), (409, "template_too_large"))
        with mock.patch.object(templates, "MAX_MILESTONES", 1):
            self.assertEqual(self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "big"}).error_code, "template_too_large")
        with mock.patch.object(templates, "MAX_DOCUMENT_BYTES", 500):
            self.assertEqual(self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "big"}).error_code, "template_too_large")
        with mock.patch.object(templates, "MAX_TEMPLATES", 1):
            self.save("one")
            self.assertEqual(self.client("alice").post("/api/projects/WEB/save-as-template", {"name": "two"}).error_code, "template_limit")
        self.assertEqual(self.count("project_templates"), 1)

    def test_a_task_under_a_closed_parent_moves_up_to_the_nearest_open_ancestor(self):
        top = self.make_task("WEB", "Top level")["key"]
        middle = self.make_task("WEB", "Middle", parent=top)["key"]
        self.make_task("WEB", "Leaf", parent=middle)["key"]
        conn = db.connect(self.config.db_path)
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = (SELECT t.id FROM tasks t JOIN projects p ON p.id = t.project_id WHERE p.key = 'WEB' AND t.number = ?)", (int(middle.split("-")[1]),))
        conn.close()
        t = self.save()
        by = {x["title"]: x for x in t["task_list"]}
        self.assertNotIn("Middle", by)
        self.assertEqual(by["Leaf"]["parent"], by["Top level"]["ref"])

    def test_the_source_project_is_untouched_and_the_template_is_a_snapshot(self):
        before = self.tasks_of("WEB")
        t = self.save()
        self.assertEqual(self.tasks_of("WEB"), before)
        self.ok(self.client("alice").patch("/api/tasks/" + self.t["design"], {"title": "Renamed later"}))
        self.assertEqual(self.ok(self.client("alice").get("/api/templates/%d" % t["id"]))["task_list"][0]["title"], "Design")

    def test_saving_is_audited(self):
        t = self.save()
        event = self.audit(action="template.created")[0]
        self.assertEqual((event["category"], event["project"], event["details"]["id"], event["details"]["tasks"], event["details"]["skipped_closed"]), ("template", "WEB", t["id"], 5, 2))
        self.assertEqual(self.ok(self.client("vera").get("/api/meta"))["audit_categories"]["template"], "Project templates")


class ManageTests(TemplateBase):
    def test_listing_and_reading(self):
        b = self.save("B template")
        a = self.save("a template")
        items = self.ok(self.client("vera").get("/api/templates"))["items"]
        self.assertEqual([i["name"] for i in items], ["a template", "B template"])  # by name, ignoring case
        self.assertNotIn("task_list", items[0])
        full = self.ok(self.client("vera").get("/api/templates/%d" % b["id"]))
        self.assertEqual(len(full["task_list"]), 5)
        self.assertEqual(a["id"], items[0]["id"])
        self.assertEqual(self.client().get("/api/templates").status, 401)

    def test_bad_ids(self):
        for path in ("/api/templates/abc", "/api/templates/999", "/api/templates/0"):
            r = self.client("alice").get(path)
            self.assertEqual((r.status, r.error_code), (404, "template_not_found"), path)
        for method, path in (("patch", "/api/templates/9"), ("delete", "/api/templates/9"), ("post", "/api/templates/9/preview"), ("post", "/api/templates/9/instantiate")):
            body = {"name": "x"} if method == "patch" else {}
            args = (path, body) if method == "patch" else (path,)
            self.assertEqual(getattr(self.client("alice"), method)(*args).error_code, "template_not_found", path)

    def test_rename_and_describe(self):
        t = self.save("Old name")
        self.save("Other")
        r = self.ok(self.client("alice").patch("/api/templates/%d" % t["id"], {"name": "New name", "description": "Better"}))
        self.assertEqual((r["name"], r["description"]), ("New name", "Better"))
        self.assertEqual(self.client("alice").patch("/api/templates/%d" % t["id"], {"name": "OTHER"}).error_code, "template_name_taken")
        self.assertEqual(self.client("alice").patch("/api/templates/%d" % t["id"], {"tasks": []}).status, 422)  # the structure is a snapshot
        self.assertEqual(self.client("bob").patch("/api/templates/%d" % t["id"], {"name": "x"}).status, 403)
        self.ok(self.client("alice").patch("/api/templates/%d" % t["id"], {"name": "New name"}))
        self.assertEqual(len(self.audit(action="template.updated")), 1)  # a no-op is not an event

    def test_delete(self):
        t = self.save()
        self.start(t["id"])
        self.assertEqual(self.client("bob").delete("/api/templates/%d" % t["id"]).status, 403)
        self.assertEqual(self.client("alice").delete("/api/templates/%d" % t["id"]).status, 204)
        self.assertEqual(self.client("alice").get("/api/templates/%d" % t["id"]).status, 404)
        self.assertEqual(self.client("alice").get("/api/projects/NEW").status, 200)  # projects made from it live on
        gone = self.audit(action="template.deleted")[0]
        self.assertEqual((gone["severity"], gone["details"]["times_used"]), ("notice", 1))


class PreviewTests(TemplateBase):
    def test_dates_move_with_the_start_day(self):
        t = self.save()
        p = self.ok(self.client("alice").post("/api/templates/%d/preview" % t["id"], {"key": "new", "name": "New", "start_date": TEMPLATE_START}))
        self.assertTrue(p["dry_run"])
        self.assertEqual((p["project"]["key"], p["project"]["start_date"], p["project"]["due_date"]), ("NEW", "2026-11-02", "2027-01-15"))
        tasks = {x["title"]: x for x in p["tasks"]}
        self.assertEqual((tasks["Design"]["start_date"], tasks["Design"]["due_date"]), ("2026-11-02", "2026-11-13"))
        self.assertEqual((tasks["Mockups"]["start_date"], tasks["Mockups"]["due_date"]), ("2026-11-09", "2026-11-13"))
        self.assertEqual((tasks["Launch checklist"]["start_date"], tasks["Launch checklist"]["due_date"]), (None, "2027-01-15"))
        self.assertEqual({m["name"]: m["due_date"] for m in p["milestones"]}, {"Beta": "2026-12-11", "Launch": "2027-01-15"})
        self.assertEqual(p["counts"], {"tasks": 5, "milestones": 2, "dependencies": 2, "assigned": 0})
        self.assertEqual(p["unassigned_slots"], ["alice", "bob", "carol"])

    def test_nothing_is_written(self):
        t = self.save()
        projects, tasks_, events = self.count("projects"), self.count("tasks"), self.count("activity")
        self.ok(self.client("alice").post("/api/templates/%d/preview" % t["id"], {"key": "NEW", "name": "New", "start_date": TEMPLATE_START}))
        self.assertEqual((self.count("projects"), self.count("tasks"), self.count("activity")), (projects, tasks_, events))
        self.assertEqual(self.ok(self.client("alice").get("/api/templates/%d" % t["id"]))["times_used"], 0)

    def test_weekend_dates_roll_to_monday_without_reordering(self):
        t = self.save()
        start = "2026-11-04"  # a Wednesday: offsets that land on a weekend now exist
        plain = self.ok(self.client("alice").post("/api/templates/%d/preview" % t["id"], {"key": "NEW", "name": "N", "start_date": start}))
        rolled = self.ok(self.client("alice").post("/api/templates/%d/preview" % t["id"], {"key": "NEW", "name": "N", "start_date": start, "roll_weekends": True}))
        from datetime import date

        weekend = [x for x in plain["tasks"] + plain["milestones"] if x.get("due_date") and date.fromisoformat(x["due_date"]).weekday() >= 5]
        self.assertTrue(weekend)  # the fixture does produce weekend dates
        for x in rolled["tasks"]:
            for field in ("start_date", "due_date"):
                if x[field]:
                    self.assertLess(date.fromisoformat(x[field]).weekday(), 5, x)
            if x["start_date"] and x["due_date"]:
                self.assertLessEqual(x["start_date"], x["due_date"])
        for before, after in zip(plain["tasks"], rolled["tasks"], strict=True):
            if before["due_date"]:
                self.assertGreaterEqual(after["due_date"], before["due_date"])
                self.assertLessEqual((date.fromisoformat(after["due_date"]) - date.fromisoformat(before["due_date"])).days, 2)

    def test_assignments_and_members(self):
        t = self.save(slots={"bob": "Designer"})
        self.make_user("dana", "member")
        p = self.ok(self.client("alice").post("/api/templates/%d/preview" % t["id"], {"key": "NEW", "name": "N", "start_date": TEMPLATE_START, "assignments": {"Designer": "dana", "alice": "alice"}, "members": ["carol"]}))
        by = {x["title"]: x for x in p["tasks"]}
        self.assertEqual((by["Design"]["assignee"], by["Build"]["assignee"], by["Mockups"]["assignee"]), ("dana", "alice", None))
        self.assertEqual(p["project"]["members"], ["carol", "dana", "alice"])  # given members first, then the assigned people
        self.assertEqual((p["unassigned_slots"], p["counts"]["assigned"]), (["carol"], 3))

    def test_every_problem_is_reported_at_once(self):
        t = self.save()
        r = self.client("alice").post("/api/templates/%d/preview" % t["id"], {"key": "9", "name": "", "start_date": "tomorrow", "assignments": {"ghost": "bob"}, "members": ["nobody"], "owner": 5})
        self.assertEqual(r.status, 422)
        for field in ("name", "start_date", "assignments", "owner", "key"):
            self.assertIn(field, r.json["error"]["details"])

    def test_refusals(self):
        t = self.save()
        body = {"key": "NEW", "name": "N", "start_date": TEMPLATE_START}
        post = lambda **extra: self.client("alice").post("/api/templates/%d/preview" % t["id"], {**body, **extra})  # noqa: E731
        self.assertEqual((post(key="WEB").status, post(key="web").error_code), (409, "key_taken"))
        self.assertEqual(post(assignments={"bob": "ghost"}).status, 422)
        self.assertIn("unknown slot", post(assignments={"Designer": "bob"}).json["error"]["details"]["assignments"])
        self.assertEqual(post(assignments=["bob"]).status, 422)
        self.assertEqual(post(members="bob").status, 422)
        self.assertEqual(post(start_date="1999-12-31").status, 422)
        self.assertEqual(post(start_date="2101-01-01").status, 422)
        self.assertEqual(post(roll_weekends="yes").status, 422)
        self.assertEqual(post(extra=1).status, 422)
        self.ok(self.admin.patch("/api/users/%d" % self.ok(self.client("carol").get("/api/me"))["id"], {"active": False}))
        self.assertEqual(post(assignments={"carol": "carol"}).status, 422)
        self.assertEqual(self.client("bob").post("/api/templates/%d/preview" % t["id"], body).status, 403)

    def test_a_template_with_a_loop_is_refused(self):
        t = self.save()
        conn = db.connect(self.config.db_path)
        doc = json.loads(conn.execute("SELECT data FROM project_templates").fetchone()[0])
        doc["dependencies"].append([doc["dependencies"][-1][1], doc["dependencies"][0][0]])  # close the circle
        conn.execute("UPDATE project_templates SET data = ?", (json.dumps(doc),))
        conn.close()
        r = self.client("alice").post("/api/templates/%d/preview" % t["id"], {"key": "NEW", "name": "N", "start_date": TEMPLATE_START})
        self.assertEqual((r.status, r.error_code), (409, "template_cycle"))

    def test_the_loop_check(self):
        self.assertTrue(templates._acyclic({"b": "a", "c": "a"}, [("b", "c")]))
        self.assertFalse(templates._acyclic({}, [("a", "b"), ("b", "c"), ("c", "a")]))
        self.assertFalse(templates._acyclic({"a": "b"}, [("b", "a")]))  # a must finish before its parent b, and b before a
        self.assertTrue(templates._acyclic({}, []))


class InstantiateTests(TemplateBase):
    def test_a_whole_project_appears(self):
        t = self.save(slots={"bob": "Designer", "carol": "Visual"})
        self.make_user("dana", "member")
        made = self.start(t["id"], assignments={"Designer": "dana", "Visual": "carol", "alice": "alice"}, description="Rebuild")
        self.assertEqual(made["created"], {"tasks": 5, "milestones": 2, "dependencies": 2, "assigned": 4})
        self.assertEqual(made["unassigned_slots"], [])
        project = made["project"]
        self.assertEqual((project["key"], project["name"], project["description"], project["start_date"], project["due_date"]), ("NEW", "New site", "Rebuild", "2026-11-02", "2027-01-15"))
        self.assertEqual(project["owner"]["username"], "alice")
        self.assertEqual(sorted(m["username"] for m in project["members"]), ["alice", "carol", "dana"])  # assigned people join
        tasks = self.tasks_of("NEW")
        self.assertEqual(set(tasks), {"Design", "Wireframes", "Mockups", "Build", "Launch checklist"})
        self.assertEqual({k: v["status"] for k, v in tasks.items()}, dict.fromkeys(tasks, "todo"))
        self.assertEqual(made["tasks"]["T1"], tasks["Design"]["key"])
        design = self.ok(self.client("alice").get("/api/tasks/" + tasks["Design"]["key"]))
        self.assertEqual((design["type"], design["priority"], design["estimate_hours"], design["labels"], design["description"]), ("epic", "high", 20, ["ux"], "All the design"))
        self.assertEqual((design["start_date"], design["due_date"], design["assignee"]["username"], design["reporter"]["username"]), ("2026-11-02", "2026-11-13", "dana", "alice"))
        self.assertEqual(design["milestone"]["name"], "Beta")
        sub = self.ok(self.client("alice").get("/api/tasks/" + tasks["Mockups"]["key"]))
        self.assertEqual((sub["parent"], sub["labels"], sub["assignee"]["username"]), (design["key"], ["ux", "visual"], "carol"))
        self.assertEqual([b["key"] for b in sub["blocked_by"]], [tasks["Wireframes"]["key"]])
        build = self.ok(self.client("alice").get("/api/tasks/" + tasks["Build"]["key"]))
        self.assertEqual([b["key"] for b in build["blocked_by"]], [tasks["Mockups"]["key"]])
        self.assertEqual(self.ok(self.client("alice").get("/api/projects/NEW/milestones"))["items"][0]["name"], "Beta")

    def test_the_task_keys_are_numbered_from_one_in_template_order(self):
        t = self.save()
        made = self.start(t["id"])
        self.assertEqual(list(made["tasks"].values()), ["NEW-%d" % i for i in range(1, 6)])
        next_task = self.make_task("NEW", "Added by hand")
        self.assertEqual(next_task["key"], "NEW-6")

    def test_the_caller_is_the_owner_unless_told_otherwise(self):
        t = self.save()
        self.assertEqual(self.start(t["id"], key="ONE", who="admin")["project"]["owner"]["username"], "admin")
        made = self.start(t["id"], key="TWO", owner="bob", members=["carol"])
        self.assertEqual((made["project"]["owner"]["username"], sorted(m["username"] for m in made["project"]["members"])), ("bob", ["bob", "carol"]))

    def test_a_template_can_be_used_many_times(self):
        t = self.save()
        self.start(t["id"], key="AAA")
        self.start(t["id"], key="BBB", start="2027-02-01")
        again = self.ok(self.client("alice").get("/api/templates/%d" % t["id"]))
        self.assertEqual((again["times_used"], again["last_used_at"]), (2, "2026-10-16T09:00:00Z"))
        bbb = self.tasks_of("BBB")
        self.assertEqual(bbb["Design"]["start_date"], "2027-02-01")
        self.assertEqual(self.tasks_of("AAA")["Design"]["start_date"], "2026-11-02")  # the first one did not move

    def test_nothing_is_created_when_it_fails(self):
        t = self.save()
        before = {name: self.count(name) for name in ("projects", "tasks", "milestones", "task_dependencies", "activity")}
        for extra in ({"assignments": {"bob": "ghost"}}, {"members": ["ghost"]}, {"key": "WEB"}, {"name": ""}):
            r = self.client("alice").post("/api/templates/%d/instantiate" % t["id"], {"key": "NEW", "name": "N", "start_date": TEMPLATE_START, **extra})
            self.assertIn(r.status, (409, 422), extra)
        self.assertEqual({name: self.count(name) for name in before}, before)
        self.assertEqual(self.ok(self.client("alice").get("/api/templates/%d" % t["id"]))["times_used"], 0)

    def test_a_failure_part_way_leaves_nothing_behind(self):
        t = self.save()
        before = {name: self.count(name) for name in ("projects", "tasks", "milestones", "task_dependencies")}
        with mock.patch.object(templates.tasks, "create_in_project", side_effect=[(1, "X-1"), RuntimeError("boom")]):
            r = self.client("alice").post("/api/templates/%d/instantiate" % t["id"], {"key": "NEW", "name": "N", "start_date": TEMPLATE_START})
        self.assertEqual(r.status, 500)
        self.assertEqual({name: self.count(name) for name in before}, before)

    def test_required_custom_fields_are_honoured(self):
        self.ok(self.admin.post("/api/project-fields", {"key": "client", "label": "Client", "type": "text", "required": True}), 201)
        t = self.save()
        r = self.client("alice").post("/api/templates/%d/instantiate" % t["id"], {"key": "NEW", "name": "N", "start_date": TEMPLATE_START})
        self.assertEqual(r.status, 422)
        self.assertIn("custom_fields.client", r.json["error"]["details"])
        self.assertEqual(self.count("projects"), 1)
        made = self.start(t["id"], custom_fields={"client": "Acme"})
        self.assertEqual(made["project"]["custom_fields"]["client"], "Acme")

    def test_the_new_project_is_searchable_and_audited(self):
        t = self.save()
        self.start(t["id"])
        found = self.ok(self.client("alice").get("/api/search", query={"q": "wireframes project:NEW"}))
        self.assertEqual([i["key"] for i in found["items"]], [self.tasks_of("NEW")["Wireframes"]["key"]])
        event = self.audit(action="template.instantiated")[0]
        self.assertEqual((event["category"], event["project"], event["details"]["template"], event["details"]["project"], event["details"]["tasks"]), ("template", "NEW", "Website", "NEW", 5))
        self.assertEqual(event["details"]["unassigned_slots"], ["alice", "bob", "carol"])
        history = self.ok(self.client("alice").get("/api/tasks/%s/activity" % self.tasks_of("NEW")["Mockups"]["key"]))["items"]
        self.assertIn("task.dependency_added", [h["action"] for h in history])
        self.assertIn("task.created", [h["action"] for h in history])

    def test_no_assignment_messages_are_sent(self):
        t = self.save()
        before = len(self.notifier.messages)
        self.start(t["id"], assignments={"bob": "bob", "carol": "carol"})
        self.assertEqual(len(self.notifier.messages), before)  # one message per task would be a flood

    def test_who_may_start_projects(self):
        t = self.save()
        for who in ("bob", "vera"):
            self.assertEqual(self.client(who).post("/api/templates/%d/instantiate" % t["id"], {"key": "NEW", "name": "N", "start_date": TEMPLATE_START}).status, 403, who)
        self.assertEqual(self.client().post("/api/templates/%d/instantiate" % t["id"], {}).status, 401)

    def test_a_read_only_api_token_cannot_start_projects(self):
        t = self.save()
        made = self.ok(self.client("alice").post("/api/tokens", {"name": "ro", "read_only": True}), 201)
        c = self.client(token=made["token"])
        self.assertEqual(c.get("/api/templates").status, 200)
        self.assertEqual(c.post("/api/templates/%d/instantiate" % t["id"], {"key": "NEW", "name": "N", "start_date": TEMPLATE_START}).error_code, "token_read_only")

    def test_weekend_rolling_applies_to_what_is_created(self):
        t = self.save()
        self.start(t["id"], key="PLAIN", start="2026-11-04")
        self.start(t["id"], key="ROLLED", start="2026-11-04", roll_weekends=True)
        from datetime import date

        for task in self.tasks_of("ROLLED").values():
            for field in ("start_date", "due_date"):
                if task[field]:
                    self.assertLess(date.fromisoformat(task[field]).weekday(), 5)
        self.assertTrue(any(date.fromisoformat(t_["due_date"]).weekday() >= 5 for t_ in self.tasks_of("PLAIN").values() if t_["due_date"]))

    def test_a_project_without_dates_makes_one_without_dates(self):
        self.make_project("BARE", members=("bob",))
        self.make_task("BARE", "One", assignee="bob")
        self.make_task("BARE", "Two")
        t = self.save("Bare", project="BARE")
        made = self.start(t["id"], key="CLONE", assignments={"bob": "bob"})
        self.assertIsNone(made["project"]["due_date"])
        self.assertEqual({v["due_date"] for v in self.tasks_of("CLONE").values()}, {None})

    def test_events_can_be_sent_to_a_webhook(self):
        hook = self.ok(self.admin.post("/api/webhooks", {"name": "t", "url": "https://hooks.example.com/in", "events": ["template.*"]}), 201)
        t = self.save()
        self.start(t["id"])
        events = [d["event"] for d in self.ok(self.admin.get("/api/webhooks/%d/deliveries" % hook["id"]))["items"]]
        self.assertEqual(sorted(events), ["template.created", "template.instantiated"])


class SafetyTests(TemplateBase):
    def test_hostile_input_never_causes_a_server_error(self):
        t = self.save()
        bodies = [None, [], "x", 5, {"key": {"a": 1}}, {"key": "NEW", "name": "N", "start_date": ["x"]}, {"key": "NEW", "name": "N", "start_date": "9999-12-31"}, {"key": "NEW", "name": "N", "start_date": TEMPLATE_START, "assignments": {"bob": {"a": 1}}},
                  {"key": "NEW", "name": "N", "start_date": TEMPLATE_START, "assignments": {"x" * 5000: "bob"}}, {"key": "NEW", "name": "N", "start_date": TEMPLATE_START, "members": [None, 5, {"a": 1}]},
                  {"key": "NEW", "name": "N", "start_date": TEMPLATE_START, "custom_fields": "nope"}, {"key": "NEW", "name": "N", "start_date": TEMPLATE_START, "owner": ["x"]}]
        for body in bodies:
            for action in ("preview", "instantiate"):
                r = self.client("alice").request("POST", "/api/templates/%d/%s" % (t["id"], action), raw=json.dumps(body).encode(), content_type="application/json")
                self.assertLess(r.status, 500, (action, body))
        for body in bodies + [{"name": "x", "slots": {"bob": ["x"]}}, {"name": "\x00"}]:
            r = self.client("alice").request("POST", "/api/projects/WEB/save-as-template", raw=json.dumps(body).encode(), content_type="application/json")
            self.assertLess(r.status, 500, body)
        self.assertEqual(self.count("projects"), 1)


class UpgradeTests(TemplateBase):
    def v8_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        for version in range(1, 8):
            db._migrate(conn, version)
        conn.close()

    def names(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_an_older_database_upgrades_to_the_same_schema_as_a_new_one(self):
        old = os.path.join(self.tmp, "v8.db")
        self.v8_database(old)
        self.assertNotIn("project_templates", self.names(old)[0])
        init_db(old)
        fresh = os.path.join(self.tmp, "fresh9.db")
        init_db(fresh)
        self.assertEqual(self.names(old), self.names(fresh))
        self.assertEqual(self.names(old)[1], db.SCHEMA_VERSION)

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v8b.db")
        self.v8_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE project_templates (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        self.assertEqual(self.names(path)[1], 8)
