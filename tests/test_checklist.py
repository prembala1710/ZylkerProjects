"""Checklists inside a task: items, order, ticking, limits and permissions."""

from unittest import mock

from helpers import ApiTestCase


class ChecklistBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.make_task("WEB", "Release")
        self.alice, self.bob = self.client("alice"), self.client("bob")

    def add(self, text, user="bob", **extra):
        return self.client(user).post("/api/tasks/WEB-1/checklist", {"text": text, **extra})

    def texts(self, key="WEB-1"):
        return [i["text"] for i in self.ok(self.alice.get("/api/tasks/%s/checklist" % key))["items"]]


class ItemTests(ChecklistBase):
    def test_items_keep_their_order_and_feed_the_task_summary(self):
        a = self.ok(self.add("Write notes"), 201)
        self.ok(self.add("Tag release"), 201)
        self.assertEqual((a["position"], a["done"], a["done_by"], a["task"]), (1, False, None, "WEB-1"))
        self.assertEqual(self.texts(), ["Write notes", "Tag release"])
        task = self.ok(self.alice.get("/api/tasks/WEB-1"))
        self.assertEqual(task["checklist"], {"total": 2, "done": 0})
        self.assertEqual([i["text"] for i in task["checklist_items"]], ["Write notes", "Tag release"])
        listed = self.ok(self.alice.get("/api/tasks", query={"project": "WEB"}))["items"][0]
        self.assertEqual(listed["checklist"], {"total": 2, "done": 0})
        self.assertNotIn("checklist_items", listed)  # only the detail view carries the items themselves

    def test_a_new_task_has_an_empty_checklist(self):
        self.assertEqual(self.ok(self.alice.get("/api/tasks/WEB-1"))["checklist"], {"total": 0, "done": 0})

    def test_inserting_at_a_position_pushes_the_rest_down(self):
        for text in ("one", "two", "three"):
            self.ok(self.add(text), 201)
        inserted = self.ok(self.add("zero", position=1), 201)
        self.assertEqual(inserted["position"], 1)
        self.ok(self.add("middle", position=3), 201)
        self.assertEqual(self.texts(), ["zero", "one", "middle", "two", "three"])
        items = self.ok(self.alice.get("/api/tasks/WEB-1/checklist"))["items"]
        self.assertEqual([i["position"] for i in items], [1, 2, 3, 4, 5])
        self.ok(self.add("far away", position=50), 201)  # beyond the end means "at the end"
        self.assertEqual(self.texts()[-1], "far away")

    def test_ticking_records_who_and_when_and_unticking_forgets(self):
        item = self.ok(self.add("Smoke test"), 201)
        url = "/api/checklist/%d" % item["id"]
        ticked = self.ok(self.client("carol").patch(url, {"done": True}))
        self.assertEqual((ticked["done"], ticked["done_by"]["username"], ticked["done_at"]), (True, "carol", "2026-10-16T09:00:00Z"))
        self.assertEqual(self.ok(self.alice.get("/api/tasks/WEB-1"))["checklist"], {"total": 1, "done": 1})
        unticked = self.ok(self.bob.patch(url, {"done": False}))
        self.assertEqual((unticked["done"], unticked["done_by"], unticked["done_at"]), (False, None, None))

    def test_an_item_can_be_created_already_ticked(self):
        item = self.ok(self.add("Already done", done=True), 201)
        self.assertEqual((item["done"], item["done_by"]["username"]), (True, "bob"))

    def test_editing_and_reordering(self):
        ids = [self.ok(self.add(t), 201)["id"] for t in ("a", "b", "c")]
        self.assertEqual(self.ok(self.bob.patch("/api/checklist/%d" % ids[1], {"text": "  B!  "}))["text"], "B!")
        moved = self.ok(self.bob.patch("/api/checklist/%d" % ids[2], {"position": 1}))
        self.assertEqual(moved["position"], 1)
        self.assertEqual(self.texts(), ["c", "a", "B!"])
        self.ok(self.bob.patch("/api/checklist/%d" % ids[2], {"position": 50}))  # past the end = last
        self.assertEqual(self.texts(), ["a", "B!", "c"])

    def test_deleting_closes_the_gap(self):
        ids = [self.ok(self.add(t), 201)["id"] for t in ("a", "b", "c")]
        self.ok(self.bob.delete("/api/checklist/%d" % ids[0]), 204)
        items = self.ok(self.alice.get("/api/tasks/WEB-1/checklist"))["items"]
        self.assertEqual([(i["text"], i["position"]) for i in items], [("b", 1), ("c", 2)])
        self.assertEqual(self.bob.delete("/api/checklist/%d" % ids[0]).status, 404)

    def test_checklists_belong_to_their_own_task_and_go_with_it(self):
        self.make_task("WEB", "Other")
        self.ok(self.add("mine"), 201)
        self.assertEqual(self.texts("WEB-2"), [])
        self.ok(self.alice.delete("/api/tasks/WEB-1"), 204)
        with self.app.session(write=False) as ctx:
            self.assertEqual(ctx.conn.execute("SELECT COUNT(*) FROM task_checklist_items").fetchone()[0], 0)

    def test_the_checklist_never_gates_the_workflow(self):
        self.ok(self.add("never ticked"), 201)
        self.ok(self.set_status("WEB-1", "in_progress"))
        self.ok(self.set_status("WEB-1", "done"))  # an unticked item does not stop the task closing
        self.ok(self.add("added after closing"), 201)  # and a closed task can still be annotated
        self.ok(self.bob.patch("/api/checklist/1", {"done": True}))


class RuleTests(ChecklistBase):
    def test_validation(self):
        for body in ({}, {"text": ""}, {"text": "   "}, {"text": "x" * 201}, {"text": 5}, {"text": "ok", "done": "yes"},
                     {"text": "ok", "position": 0}, {"text": "ok", "position": "1"}, {"text": "ok", "colour": "red"}):  # fmt: skip
            self.assertEqual(self.client("bob").post("/api/tasks/WEB-1/checklist", body).status, 422, body)
        self.ok(self.add("x" * 200), 201)  # exactly the limit is fine
        item = self.ok(self.add("fine"), 201)
        self.assertEqual(self.bob.patch("/api/checklist/%d" % item["id"], {"text": ""}).status, 422)
        self.assertEqual(self.bob.patch("/api/checklist/%d" % item["id"], {"done": "no"}).status, 422)

    def test_html_is_stored_as_text(self):
        item = self.ok(self.add("<img src=x onerror=alert(1)>"), 201)
        self.assertEqual(item["text"], "<img src=x onerror=alert(1)>")

    def test_a_task_holds_at_most_fifty_items(self):
        with mock.patch("zylker_pm.services.checklist.MAX_CHECKLIST_ITEMS", 3):
            for n in range(3):
                self.ok(self.add("item %d" % n), 201)
            response = self.add("one too many")
            self.assertEqual((response.status, response.error_code), (409, "checklist_full"))
        self.assertEqual(len(self.texts()), 3)

    def test_who_may_change_a_checklist(self):
        item = self.ok(self.add("shared"), 201)
        url = "/api/checklist/%d" % item["id"]
        self.assertEqual(self.client("vera").get("/api/tasks/WEB-1/checklist").status, 200)  # viewers can read
        self.assertEqual(self.client("vera").post("/api/tasks/WEB-1/checklist", {"text": "x"}).status, 403)
        self.assertEqual(self.client("vera").patch(url, {"done": True}).status, 403)
        self.assertEqual(self.client("vera").delete(url).status, 403)
        self.make_project("API", members=("carol",))  # bob is not in API
        self.make_task("API", "Elsewhere")
        r = self.client("bob").post("/api/tasks/API-1/checklist", {"text": "x"})
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        self.assertEqual(self.alice.post("/api/tasks/API-1/checklist", {"text": "managers write anywhere"}).status, 201)
        self.assertEqual(self.client("bob").get("/api/tasks/API-1/checklist").status, 200)

    def test_archived_projects_are_read_only(self):
        item = self.ok(self.add("before"), 201)
        self.ok(self.alice.patch("/api/projects/WEB", {"status": "archived"}))
        for response in (self.add("after"), self.bob.patch("/api/checklist/%d" % item["id"], {"done": True}), self.bob.delete("/api/checklist/%d" % item["id"])):
            self.assertEqual((response.status, response.error_code), (409, "project_archived"))
        self.assertEqual(self.texts(), ["before"])

    def test_unknown_tasks_and_items(self):
        self.assertEqual(self.alice.get("/api/tasks/WEB-99/checklist").status, 404)
        self.assertEqual(self.alice.post("/api/tasks/WEB-99/checklist", {"text": "x"}).status, 404)
        self.assertEqual(self.bob.patch("/api/checklist/999", {"done": True}).status, 404)
        self.assertEqual(self.bob.patch("/api/checklist/abc", {"done": True}).status, 404)

    def test_every_change_is_in_the_task_history(self):
        item = self.ok(self.add("Ship it"), 201)
        url = "/api/checklist/%d" % item["id"]
        self.ok(self.bob.patch(url, {"text": "Ship it today"}))
        self.ok(self.bob.patch(url, {"done": True}))
        self.ok(self.bob.patch(url, {"done": True}))  # repeating a tick is not a new event
        self.ok(self.bob.patch(url, {"done": False}))
        self.ok(self.bob.delete(url), 204)
        actions = [a["action"] for a in self.ok(self.alice.get("/api/tasks/WEB-1/activity"))["items"]][::-1]
        self.assertEqual(actions[1:], ["checklist.added", "checklist.edited", "checklist.checked", "checklist.unchecked", "checklist.removed"])
