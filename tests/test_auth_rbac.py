"""Authentication, roles and project membership rules."""

import sqlite3

from helpers import ApiTestCase


class AuthTests(ApiTestCase):
    def test_health_is_public_everything_else_needs_a_token(self):
        anon = self.client()
        self.assertEqual(anon.get("/api/health").status, 200)
        for path in ("/api/me", "/api/projects", "/api/tasks", "/api/reports/portfolio"):
            r = anon.get(path)
            self.assertEqual(r.status, 401, path)
            self.assertEqual(r.headers["WWW-Authenticate"], "Bearer")

    def test_bad_and_malformed_tokens_are_rejected(self):
        self.assertEqual(self.client().get("/api/me", token="zpm_nope").status, 401)
        r = self.client().get("/api/me", headers={"Authorization": "Basic abc"}, token=None)
        self.assertEqual(r.status, 401)

    def test_tokens_are_stored_hashed(self):
        conn = sqlite3.connect(self.config.db_path)
        hashes = [row[0] for row in conn.execute("SELECT token_hash FROM users")]
        conn.close()
        for token in self.tokens.values():
            self.assertNotIn(token, hashes)
            self.assertTrue(all(len(h) == 64 for h in hashes))

    def test_deactivated_user_loses_access_immediately(self):
        bob = self.ok(self.client("bob").get("/api/me"))
        self.ok(self.admin.patch("/api/users/%d" % bob["id"], {"active": False}))
        self.assertEqual(self.client("bob").get("/api/me").status, 401)

    def test_rotating_a_token_invalidates_the_old_one(self):
        me = self.ok(self.client("carol").get("/api/me"))
        new = self.ok(self.client("carol").post("/api/users/%d/rotate-token" % me["id"]))["token"]
        self.assertEqual(self.client("carol").get("/api/me").status, 401)
        self.assertEqual(self.client(token=new).get("/api/me").status, 200)

    def test_cannot_rotate_someone_elses_token_unless_admin(self):
        carol = self.ok(self.client("carol").get("/api/me"))
        self.assertEqual(self.client("bob").post("/api/users/%d/rotate-token" % carol["id"]).status, 403)
        self.assertEqual(self.admin.post("/api/users/%d/rotate-token" % carol["id"]).status, 200)


class RoleTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob",))

    def test_only_manager_or_admin_can_create_projects(self):
        body = {"key": "NEW", "name": "New"}
        for user, expected in (("vera", 403), ("bob", 403), ("alice", 201), ("admin", 201)):
            body["key"] = "N" + user.upper()[:4]
            self.assertEqual(self.client(user).post("/api/projects", body).status, expected, user)

    def test_viewer_can_read_but_not_write(self):
        vera = self.client("vera")
        self.assertEqual(vera.get("/api/projects/WEB").status, 200)
        self.assertEqual(vera.get("/api/projects/WEB/summary").status, 200)
        r = vera.post("/api/projects/WEB/tasks", {"title": "x"})
        self.assertEqual((r.status, r.error_code), (403, "insufficient_role"))

    def test_member_needs_project_membership_to_write(self):
        r = self.client("carol").post("/api/projects/WEB/tasks", {"title": "x"})
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        self.assertEqual(self.client("bob").post("/api/projects/WEB/tasks", {"title": "x"}).status, 201)
        # Managers can write anywhere without being members.
        self.assertEqual(self.client("alice").post("/api/projects/WEB/tasks", {"title": "y"}).status, 201)

    def test_member_cannot_edit_tasks_in_other_projects(self):
        self.make_task("WEB", "T")
        r = self.client("carol").patch("/api/tasks/WEB-1", {"title": "hacked"})
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))

    def test_delete_task_needs_manager_and_delete_project_needs_admin(self):
        self.make_task("WEB", "T")
        self.assertEqual(self.client("bob").delete("/api/tasks/WEB-1").status, 403)
        self.assertEqual(self.client("alice").delete("/api/tasks/WEB-1").status, 204)
        self.assertEqual(self.client("alice").delete("/api/projects/WEB", query={"confirm": "WEB"}).status, 403)

    def test_user_management_is_admin_only(self):
        body = {"username": "newbie", "display_name": "New B"}
        self.assertEqual(self.client("alice").post("/api/users", body).status, 403)
        r = self.admin.post("/api/users", body)
        self.assertEqual(r.status, 201)
        self.assertTrue(r.json["token"].startswith("zpm_"))
        self.assertEqual(r.json["user"]["role"], "member")
        self.assertNotIn("token_hash", r.json["user"])
        self.assertEqual(self.admin.post("/api/users", body).status, 409)

    def test_users_can_edit_their_profile_but_not_their_role(self):
        bob = self.ok(self.client("bob").get("/api/me"))
        self.ok(self.client("bob").patch("/api/users/%d" % bob["id"], {"display_name": "Robert"}))
        r = self.client("bob").patch("/api/users/%d" % bob["id"], {"role": "admin"})
        self.assertEqual((r.status, r.error_code), (403, "insufficient_role"))
        carol = self.ok(self.client("carol").get("/api/me"))
        self.assertEqual(self.client("bob").patch("/api/users/%d" % carol["id"], {"display_name": "x"}).status, 403)

    def test_last_admin_cannot_be_demoted_or_deactivated(self):
        me = self.ok(self.admin.get("/api/me"))
        for body in ({"role": "member"}, {"active": False}):
            r = self.admin.patch("/api/users/%d" % me["id"], body)
            self.assertEqual((r.status, r.error_code), (409, "last_admin"), body)
        self.make_user("admin2", "admin")
        self.assertEqual(self.admin.patch("/api/users/%d" % me["id"], {"role": "manager"}).status, 200)

    def test_user_audit_trail_is_admin_visible(self):
        self.admin.post("/api/users", {"username": "zed", "display_name": "Zed"})
        actions = [a["action"] for a in self.ok(self.admin.get("/api/activity"))["items"]]
        self.assertIn("user.created", actions)
        self.assertEqual(self.client("alice").get("/api/activity").status, 403)
