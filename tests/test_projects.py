"""Project lifecycle, membership integrity and archiving."""

from helpers import ApiTestCase


class ProjectTests(ApiTestCase):
    def test_create_normalises_key_and_adds_owner_and_members(self):
        p = self.ok(self.client("alice").post("/api/projects", {"key": "web2", "name": "Web", "members": ["bob"]}), 201)
        self.assertEqual(p["key"], "WEB2")
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["owner"]["username"], "alice")
        self.assertEqual(sorted(m["username"] for m in p["members"]), ["alice", "bob"])
        self.assertEqual(p["task_counts"], {"total": 0, "open": 0, "done": 0})

    def test_validation_errors_are_reported_per_field(self):
        r = self.client("alice").post("/api/projects", {"key": "1bad", "name": "", "start_date": "2026-13-01", "bogus": 1})
        self.assertEqual((r.status, r.error_code), (422, "validation_error"))
        fields = r.json["error"]["details"]
        self.assertIn("name", fields)
        self.assertIn("start_date", fields)
        self.assertEqual(fields["bogus"], "unknown field")
        r = self.client("alice").post("/api/projects", {"key": "1bad", "name": "x"})
        self.assertIn("key", r.json["error"]["details"])
        r = self.client("alice").post("/api/projects", {"key": "OK", "name": "x", "start_date": "2026-05-02", "due_date": "2026-05-01"})
        self.assertIn("due_date", r.json["error"]["details"])

    def test_duplicate_key_and_unknown_member(self):
        self.make_project("WEB")
        r = self.client("alice").post("/api/projects", {"key": "WEB", "name": "dup"})
        self.assertEqual((r.status, r.error_code), (409, "key_taken"))
        r = self.client("alice").post("/api/projects", {"key": "ZZ", "name": "x", "members": ["nobody"]})
        self.assertEqual(r.status, 422)

    def test_unknown_project_is_404_and_lookup_is_case_insensitive(self):
        self.make_project("WEB")
        self.assertEqual(self.client("bob").get("/api/projects/NOPE").status, 404)
        self.assertEqual(self.client("bob").get("/api/projects/web").json["key"], "WEB")

    def test_list_hides_archived_by_default_and_filters(self):
        self.make_project("AAA")
        self.make_project("BBB")
        self.ok(self.client("alice").patch("/api/projects/BBB", {"status": "archived"}))
        keys = lambda **q: [p["key"] for p in self.ok(self.client("bob").get("/api/projects", query=q))["items"]]  # noqa: E731
        self.assertEqual(keys(), ["AAA"])
        self.assertEqual(keys(include_archived="true"), ["AAA", "BBB"])
        self.assertEqual(keys(status="archived"), ["BBB"])
        self.assertEqual(keys(q="aaa"), ["AAA"])
        self.assertEqual(keys(member="me"), ["AAA"])
        self.assertEqual(self.client("bob").get("/api/projects", query={"status": "bogus"}).status, 422)

    def test_cannot_complete_a_project_with_open_tasks(self):
        self.make_project("WEB")
        self.make_task("WEB", "Open one")
        r = self.client("alice").patch("/api/projects/WEB", {"status": "completed"})
        self.assertEqual((r.status, r.error_code), (409, "open_tasks_remaining"))
        self.assertEqual(r.json["error"]["details"]["open_tasks"], 1)
        self.ok(self.set_status("WEB-1", "done"))
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "completed"}))

    def test_status_change_notifies_cliq_but_other_edits_do_not(self):
        self.make_project("WEB")
        self.ok(self.client("alice").patch("/api/projects/WEB", {"name": "Renamed"}))
        self.assertEqual(self.notifier.messages, [])
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "on_hold"}))
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("moved from active to on_hold", self.notifier.messages[0])

    def test_archived_projects_are_read_only_until_reopened(self):
        self.make_project("WEB")
        self.make_task("WEB", "T")
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "archived"}))
        for call in (
            lambda c: c.post("/api/projects/WEB/tasks", {"title": "no"}),
            lambda c: c.patch("/api/tasks/WEB-1", {"title": "no"}),
            lambda c: c.post("/api/tasks/WEB-1/comments", {"body": "no"}),
            lambda c: c.post("/api/projects/WEB/milestones", {"name": "m", "due_date": "2026-12-01"}),
        ):
            r = call(self.client("alice"))
            self.assertEqual((r.status, r.error_code), (409, "project_archived"))
        self.assertEqual(self.client("bob").get("/api/tasks/WEB-1").status, 200)  # still readable
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "active"}))
        self.ok(self.client("alice").patch("/api/tasks/WEB-1", {"title": "yes"}))

    def test_membership_integrity(self):
        self.make_project("WEB", members=("bob",))
        self.make_task("WEB", "T", assignee="bob")
        r = self.client("alice").delete("/api/projects/WEB/members/bob")
        self.assertEqual((r.status, r.error_code), (409, "member_has_open_tasks"))
        r = self.client("alice").delete("/api/projects/WEB/members/alice")
        self.assertEqual((r.status, r.error_code), (409, "owner_is_member"))
        self.ok(self.set_status("WEB-1", "done"))
        p = self.ok(self.client("alice").delete("/api/projects/WEB/members/bob"))
        self.assertEqual([m["username"] for m in p["members"]], ["alice"])
        p = self.ok(self.client("alice").post("/api/projects/WEB/members", {"username": "carol"}))
        self.assertIn("carol", [m["username"] for m in p["members"]])
        self.assertEqual(self.client("alice").post("/api/projects/WEB/members", {"username": "ghost"}).status, 422)

    def test_delete_requires_confirmation_and_removes_everything(self):
        self.make_project("WEB")
        self.make_task("WEB", "T")
        self.assertEqual(self.admin.delete("/api/projects/WEB").json["error"]["code"], "confirmation_required")
        self.assertEqual(self.admin.delete("/api/projects/WEB", query={"confirm": "wrong"}).status, 409)
        self.assertEqual(self.admin.delete("/api/projects/WEB", query={"confirm": "WEB"}).status, 204)
        self.assertEqual(self.admin.get("/api/projects/WEB").status, 404)
        self.assertEqual(self.admin.get("/api/tasks/WEB-1").status, 404)
        self.assertEqual(self.ok(self.admin.get("/api/tasks"))["total"], 0)
