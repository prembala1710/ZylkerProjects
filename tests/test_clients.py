"""MSP clients: who they are, what is shared with them, and the one thing a client contact can never do - see anything else."""

import itertools
import json
import re
import sqlite3
from unittest import mock

from helpers import ApiTestCase
from webhook_helpers import WebhookCase

from zylker_pm.app import ROUTES, App
from zylker_pm.services import clients, users


class ClientCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.client("alice")  # manager
        self.root = self.client("admin")

    def sql(self, query, args=()):
        conn = sqlite3.connect(self.config.db_path)
        try:
            return conn.execute(query, args).fetchall()
        finally:
            conn.close()

    def new_client(self, name="Acme", user="alice"):
        return self.ok(self.client(user).post("/api/clients", {"name": name}), 201)

    def share(self, client, key, level="full", update=None, user="alice"):
        body = {"share": level}
        if update is not None:
            body["update"] = update
        return self.ok(self.client(user).request("PUT", "/api/clients/%d/projects/%s" % (client["id"], key), body))

    def contact(self, client, username="rita", name="Rita Raman", **extra):
        made = self.ok(self.root.post("/api/clients/%d/contacts" % client["id"], {"username": username, "display_name": name, **extra}), 201)
        self.tokens[username] = made["token"]
        return made["contact"]

    def portal(self, username="rita"):
        return self.client(username)

    def reconfigure(self):
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.a, self.root = self.client("alice"), self.client("admin")


class ClientTests(ClientCase):
    def test_creating_listing_renaming_and_suspending_a_client(self):
        made = self.new_client("  Acme Retail  ")
        self.assertEqual(made, {"id": 1, "name": "Acme Retail", "active": True, "created_at": "2026-10-16T09:00:00Z", "updated_at": "2026-10-16T09:00:00Z"})
        self.new_client("Beta Corp")
        listing = self.ok(self.client("vera").get("/api/clients"))  # a viewer may look
        self.assertEqual([c["name"] for c in listing["items"]], ["Acme Retail", "Beta Corp"])
        self.assertEqual(listing["limits"], {"max_clients": 500, "max_name_chars": 80})
        self.assertEqual(listing["items"][0]["projects"], {"total": 0, "shared": 0, "open_tasks": 0, "overdue_tasks": 0})
        self.assertEqual((listing["items"][0]["contacts"], listing["items"][0]["health"], listing["items"][0]["forecast"]), (0, None, None))
        self.advance_days(1)
        renamed = self.ok(self.a.patch("/api/clients/1", {"name": "Acme Retail Ltd", "active": False}))
        self.assertEqual((renamed["name"], renamed["active"], renamed["updated_at"]), ("Acme Retail Ltd", False, "2026-10-17T09:00:00Z"))
        self.assertEqual(self.ok(self.a.patch("/api/clients/1", {"name": "Acme Retail Ltd"}))["updated_at"], "2026-10-17T09:00:00Z", "nothing changed: nothing touched")
        self.assertEqual([c["name"] for c in self.ok(self.a.get("/api/clients"))["items"]], ["Beta Corp", "Acme Retail Ltd"], "suspended clients last")

    def test_names(self):
        self.new_client("Acme")
        for name in ("acme", "ACME", " Acme "):
            r = self.a.post("/api/clients", {"name": name})
            self.assertEqual((r.status, r.error_code), (409, "client_name_taken"), name)
        self.new_client("Beta")
        r = self.a.patch("/api/clients/2", {"name": "ACME"})
        self.assertEqual((r.status, r.error_code), (409, "client_name_taken"))
        self.assertEqual(self.ok(self.a.patch("/api/clients/2", {"name": "BETA"}))["name"], "BETA", "a client may change the case of its own name")
        for bad in ("", "   ", "x" * 81, "bad\x07name", "line\nbreak", "evil\u202ename", 5, None):
            r = self.a.post("/api/clients", {"name": bad})
            self.assertEqual((r.status, r.error_code), (422, "validation_error"), repr(bad))
            self.assertIn("name", r.json["error"]["details"])
        self.assertEqual(self.a.post("/api/clients", {"name": "x", "colour": "red"}).status, 422)
        self.assertEqual(self.a.post("/api/clients", {}).status, 422)
        self.assertEqual(self.a.request("POST", "/api/clients", ["a"]).status, 400)
        self.assertEqual(self.new_client("x" * 80)["name"], "x" * 80)
        self.assertEqual(self.new_client("With ünïcode ✓")["name"], "With ünïcode ✓")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM clients")[0][0], 4, "refused requests stored nothing")

    def test_at_most_so_many_clients(self):
        with mock.patch.object(clients, "MAX_CLIENTS", 2):
            self.new_client("One")
            self.new_client("Two")
            r = self.a.post("/api/clients", {"name": "Three"})
            self.assertEqual((r.status, r.error_code), (409, "too_many_clients"))

    def test_who_may_do_what(self):
        for user in ("bob", "vera"):
            self.assertEqual(self.client(user).post("/api/clients", {"name": "No"}).status, 403, user)
        made = self.new_client("Acme", user="admin")
        for user in ("admin", "alice", "bob", "carol", "vera"):
            self.assertEqual(self.client(user).get("/api/clients").status, 200, user)
            self.assertEqual(self.client(user).get("/api/clients/%d" % made["id"]).status, 200, user)
        for user in ("bob", "vera"):
            self.assertEqual(self.client(user).patch("/api/clients/1", {"name": "No"}).status, 403, user)
        self.assertEqual(self.a.delete("/api/clients/1").status, 403, "deleting needs an admin")
        self.assertEqual(self.client().get("/api/clients").status, 401)
        self.assertEqual(self.client(token="nonsense").get("/api/clients").status, 401)

    def test_unknown_clients(self):
        for bad in ("99", "abc", "1.5", "-1", "0", "9223372036854775807", "9223372036854775808", "99999999999999999999", "1" * 5000):
            for method, path in (("GET", "/api/clients/"), ("PATCH", "/api/clients/"), ("DELETE", "/api/clients/")):
                r = self.root.request(method, path + bad, {} if method == "PATCH" else None)
                self.assertEqual((r.status, r.error_code), (404, "client_not_found"), (method, bad[:30]))

    def test_deleting_a_client(self):
        self.make_project("WEB")
        made = self.new_client("Acme")
        self.share(made, "WEB")
        self.ok(self.root.delete("/api/clients/1"), 204)
        self.assertEqual(self.a.get("/api/clients/1").status, 404)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM client_projects")[0][0], 0, "the link goes with the client")
        self.assertEqual(self.ok(self.a.get("/api/projects/WEB"))["key"], "WEB", "the project stays")
        self.assertEqual(self.new_client("Acme")["name"], "Acme", "and the name is free again")

    def test_a_client_with_contacts_cannot_be_deleted(self):
        made = self.new_client("Acme")
        self.contact(made)
        r = self.root.delete("/api/clients/1")
        self.assertEqual((r.status, r.error_code), (409, "client_has_contacts"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM clients")[0][0], 1)
        with self.assertRaises(sqlite3.IntegrityError):  # and the database itself says no
            conn = sqlite3.connect(self.config.db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM clients WHERE id = 1")

    def test_the_audit_trail_records_who_did_what(self):
        self.make_project("WEB")
        made = self.new_client("Acme")
        self.ok(self.a.patch("/api/clients/1", {"name": "Acme Ltd", "active": False}))
        self.share(made, "WEB", "summary")
        self.ok(self.a.delete("/api/clients/1/projects/WEB"), 204)
        self.ok(self.root.delete("/api/clients/1"), 204)
        rows = [(a, actor, json.loads(details)) for a, actor, details in self.sql("SELECT action, actor_id, details FROM activity WHERE action LIKE 'client.%' ORDER BY id")]
        self.assertEqual([r[0] for r in rows], ["client.created", "client.updated", "client.project_linked", "client.project_unlinked", "client.deleted"])
        self.assertEqual(rows[1][2], {"id": 1, "name": "Acme Ltd", "changes": {"name": ["Acme", "Acme Ltd"], "active": [True, False]}})
        self.assertEqual(rows[2][2], {"client": "Acme Ltd", "share": "summary"})

    def test_a_database_without_the_tables_says_so_until_the_server_restarts(self):
        conn = sqlite3.connect(self.config.db_path)
        conn.executescript("DROP TABLE client_users; DROP TABLE client_projects; DROP TABLE clients;")
        conn.close()
        for response in (self.a.get("/api/clients"), self.a.post("/api/clients", {"name": "x"}), self.a.get("/api/clients/1"), self.a.get("/api/portal/projects", query={"client": 1})):
            self.assertEqual((response.status, response.error_code), (409, "clients_not_initialised"))
        self.assertEqual(self.client("bob").get("/api/me").status, 200, "everybody else carries on")
        self.reconfigure()
        self.assertEqual(self.new_client("Back")["name"], "Back")


class RollupTests(ClientCase):
    """What the overview says about a client with several projects: the worst of them, and the sums."""

    def setUp(self):
        super().setUp()
        self.make_project("CALM", members=("bob",), start_date="2026-10-01", due_date="2026-12-31")
        for key in ("LATE", "SLOW"):
            self.make_project(key, members=("bob",), start_date="2026-09-01", due_date="2026-10-20")
        self.make_task("CALM", "Already done", status="done")
        self.make_task("CALM", "Fine", due_date="2026-12-01", estimate_hours=4)
        self.make_task("LATE", "Overdue and critical", due_date="2026-10-10", priority="critical", estimate_hours=4)
        self.make_task("LATE", "Another", due_date="2026-10-14", estimate_hours=4)
        self.make_task("SLOW", "Big", due_date="2026-10-19", estimate_hours=80, assignee="bob")  # two weeks of work for one person, due in three days
        self.make_task("SLOW", "Bigger", due_date="2026-10-19", estimate_hours=80, assignee="bob")
        self.acme, self.beta = self.new_client("Acme"), self.new_client("Beta")

    def item(self, name):
        return next(c for c in self.ok(self.a.get("/api/clients"))["items"] if c["name"] == name)

    def test_the_worst_project_gives_the_client_its_health_and_forecast(self):
        health = {p["key"]: p["health"]["status"] for p in self.ok(self.a.get("/api/reports/portfolio"))["projects"]}
        self.assertEqual((health["CALM"], health["LATE"]), ("on_track", "off_track"))
        for key in ("CALM", "LATE", "SLOW"):
            self.share(self.acme, key)
        acme = self.item("Acme")
        self.assertEqual(acme["health"], "off_track")
        self.assertEqual(acme["projects"], {"total": 3, "shared": 3, "open_tasks": 5, "overdue_tasks": 2})
        views = {p["project"]: p for p in self.ok(self.a.get("/api/reports/projection"))["projects"]}
        self.assertEqual(views["SLOW"]["verdict"], "will_miss", "the fixture is meant to have a project that will miss its date")
        late = [views[k] for k in ("CALM", "LATE", "SLOW")]
        self.assertEqual(
            acme["forecast"],
            {"verdict": "will_miss", "late_by_days": max(v["late_by_days"] or 0 for v in late), "late_tasks": sum(v["late_tasks"] for v in late), "finish": max(v["finish"] for v in late if v["finish"])},
        )
        self.assertEqual(self.item("Beta")["health"], None)

    def test_a_calm_client_is_calm_and_the_order_follows_the_worst(self):
        self.share(self.acme, "CALM")
        self.share(self.beta, "LATE")
        self.assertEqual([(c["name"], c["health"]) for c in self.ok(self.a.get("/api/clients"))["items"]], [("Beta", "off_track"), ("Acme", "on_track")])
        self.ok(self.a.patch("/api/clients/%d" % self.beta["id"], {"active": False}))
        self.assertEqual([c["name"] for c in self.ok(self.a.get("/api/clients"))["items"]], ["Acme", "Beta"], "a suspended client goes last however it is doing")

    def test_archived_projects_stay_linked_but_out_of_the_numbers(self):
        self.share(self.acme, "LATE")
        self.share(self.acme, "CALM")
        self.ok(self.a.patch("/api/projects/LATE", {"status": "archived"}))
        acme = self.item("Acme")
        self.assertEqual((acme["health"], acme["projects"]["total"], acme["projects"]["open_tasks"]), ("on_track", 2, 1))


class DatabaseTests(ClientCase):
    def test_the_database_itself_refuses_to_untie_a_contact_or_move_it(self):
        self.make_project("WEB")
        acme, beta = self.new_client("Acme"), self.new_client("Beta")
        self.contact(acme)
        conn = sqlite3.connect(self.config.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            for statement in ("DELETE FROM client_users", "UPDATE client_users SET client_id = %d" % beta["id"], "UPDATE client_users SET user_id = 1",
                              "DELETE FROM users WHERE username = 'rita'", "DELETE FROM clients WHERE id = %d" % acme["id"]):
                with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                    conn.execute(statement)
            self.assertEqual(conn.execute("SELECT client_id FROM client_users").fetchall(), [(acme["id"],)])
        finally:
            conn.close()


    def test_starting_the_server_again_changes_nothing(self):
        self.make_project("WEB")
        acme = self.new_client("Acme")
        self.share(acme, "WEB")
        self.contact(acme)
        self.reconfigure()
        self.reconfigure()
        self.assertEqual([c["name"] for c in self.ok(self.a.get("/api/clients"))["items"]], ["Acme"])
        self.assertEqual(self.client("rita").get("/api/portal/projects/WEB").status, 200)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'client_users_%'")[0][0], 2)


class ProjectLinkTests(ClientCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB")
        self.make_project("APP")
        self.acme, self.beta = self.new_client("Acme"), self.new_client("Beta")

    def put(self, client, key, body, user="alice"):
        return self.client(user).request("PUT", "/api/clients/%d/projects/%s" % (client["id"], key), body)

    def test_a_project_is_not_shared_until_somebody_says_so(self):
        link = self.ok(self.put(self.acme, "WEB", {}))
        self.assertEqual(link, {"client": 1, "key": "WEB", "name": "Project WEB", "status": "active", "share": "none", "update": None})
        detail = self.ok(self.a.get("/api/clients/1"))
        self.assertEqual([(p["key"], p["share"]) for p in detail["projects"]], [("WEB", "none")])
        self.assertEqual(detail["available_projects"], [{"key": "APP", "name": "Project APP", "status": "active"}])
        self.assertEqual(self.ok(self.a.get("/api/clients"))["items"][0]["projects"]["shared"], 0)

    def test_changing_how_a_project_is_shared_and_leaving_a_message(self):
        self.put(self.acme, "WEB", {"share": "summary"})
        self.advance_days(1)
        link = self.ok(self.put(self.acme, "WEB", {"share": "full", "update": "  Design is signed off.\nBeta next week.  "}))
        self.assertEqual(link["share"], "full")
        self.assertEqual(link["update"], {"text": "Design is signed off.\nBeta next week.", "by": {"id": self.sql("SELECT id FROM users WHERE username='alice'")[0][0], "username": "alice", "display_name": "Alice"}, "at": "2026-10-17T09:00:00Z"})
        self.advance_days(1)
        again = self.ok(self.put(self.acme, "WEB", {"update": "Design is signed off.\nBeta next week."}))
        self.assertEqual(again["update"]["at"], "2026-10-17T09:00:00Z", "the same words are not a new message")
        self.ok(self.put(self.acme, "WEB", {"update": None}))
        self.assertIsNone(self.ok(self.a.get("/api/clients/1"))["projects"][0]["update"])
        self.ok(self.put(self.acme, "WEB", {"update": "Back again"}, user="admin"))
        self.assertEqual(self.ok(self.a.get("/api/clients/1"))["projects"][0]["update"]["by"]["username"], "admin")
        entries = [json.loads(d) for (d,) in self.sql("SELECT details FROM activity WHERE action = 'client.project_updated' ORDER BY id")]
        self.assertEqual(entries[0]["changes"], {"share": ["summary", "full"], "update": ["", "Design is signed off.\nBeta next week."]})

    def test_validation(self):
        self.put(self.acme, "WEB", {})
        for body, field in (({"share": "everything"}, "share"), ({"share": 1}, "share"), ({"update": "x" * 1001}, "update"), ({"update": "bell\x07"}, "update"),
                            ({"update": "evil\u202etext"}, "update"), ({"update": 5}, "update"), ({"colour": "red"}, "colour")):
            r = self.put(self.acme, "WEB", body)
            self.assertEqual((r.status, r.error_code), (422, "validation_error"), body)
            self.assertIn(field, r.json["error"]["details"])
        self.assertEqual(self.client("alice").request("PUT", "/api/clients/1/projects/WEB", ["a"]).status, 400)
        self.assertEqual(self.ok(self.put(self.acme, "WEB", {"update": "x" * 1000}))["update"]["text"], "x" * 1000)
        self.assertEqual(self.ok(self.put(self.acme, "WEB", {"update": "two\nlines\tand a tab"}))["update"]["text"], "two\nlines\tand a tab")

    def test_a_project_belongs_to_one_client(self):
        self.put(self.acme, "WEB", {})
        r = self.put(self.beta, "WEB", {"share": "full"})
        self.assertEqual((r.status, r.error_code), (409, "project_assigned_elsewhere"))
        self.assertEqual(self.ok(self.a.get("/api/clients/2"))["projects"], [])
        self.ok(self.a.delete("/api/clients/1/projects/WEB"), 204)
        self.assertEqual(self.put(self.beta, "WEB", {"share": "full"}).status, 200)

    def test_unknown_things(self):
        self.assertEqual((self.put(self.acme, "NOPE", {}).status, self.put({"id": 99}, "WEB", {}).status), (404, 404))
        self.assertEqual(self.put({"id": 99}, "WEB", {}).error_code, "client_not_found")
        self.assertEqual(self.put(self.acme, "NOPE", {}).error_code, "project_not_found")
        r = self.a.delete("/api/clients/1/projects/WEB")
        self.assertEqual((r.status, r.error_code), (404, "project_not_linked"))
        self.put(self.acme, "WEB", {})
        r = self.a.delete("/api/clients/2/projects/WEB")
        self.assertEqual((r.status, r.error_code), (404, "project_not_linked"), "it is linked to a different client")
        self.assertEqual(self.put(self.acme, "web", {}).status, 200, "keys are case-insensitive")

    def test_who_may_share(self):
        for user in ("bob", "vera"):
            self.assertEqual(self.put(self.acme, "WEB", {"share": "full"}, user=user).status, 403, user)
            self.assertEqual(self.client(user).delete("/api/clients/1/projects/WEB").status, 403, user)
        self.assertEqual(self.put(self.acme, "WEB", {"share": "full"}, user="admin").status, 200)

    def test_an_archived_project_can_still_be_linked_and_unlinked_and_is_not_offered(self):
        self.ok(self.a.patch("/api/projects/APP", {"status": "archived"}))
        self.assertEqual(self.ok(self.a.get("/api/clients/1"))["available_projects"], [{"key": "WEB", "name": "Project WEB", "status": "active"}])
        self.assertEqual(self.put(self.acme, "APP", {"share": "full"}).status, 200)
        self.assertEqual(self.ok(self.a.get("/api/clients/1"))["projects"][0]["health"], None, "no health for what is archived")
        self.ok(self.a.delete("/api/clients/1/projects/APP"), 204)

    def test_the_project_activity_shows_what_was_shared(self):
        self.put(self.acme, "WEB", {"share": "summary"})
        self.put(self.acme, "WEB", {"share": "full"})
        self.ok(self.a.delete("/api/clients/1/projects/WEB"), 204)
        actions = [e["action"] for e in self.ok(self.a.get("/api/projects/WEB/activity"))["items"] if e["action"].startswith("client.")]
        self.assertEqual(actions, ["client.project_unlinked", "client.project_updated", "client.project_linked"])

    def test_deleting_the_project_removes_the_link(self):
        self.put(self.acme, "WEB", {"share": "full"})
        self.ok(self.root.delete("/api/projects/WEB", query={"confirm": "WEB"}), 204)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM client_projects")[0][0], 0)
        self.assertEqual(self.ok(self.a.get("/api/clients/1"))["projects"], [])


class ContactTests(ClientCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB")
        self.acme = self.new_client("Acme")
        self.share(self.acme, "WEB")

    def test_creating_a_contact(self):
        r = self.root.post("/api/clients/1/contacts", {"username": "Rita", "display_name": "Rita Raman", "email": "rita@acme.example"})
        made = self.ok(r, 201)
        self.assertEqual(set(made), {"contact", "token"})
        self.assertTrue(made["token"].startswith("zpm_"))
        self.assertEqual(made["contact"], {"id": made["contact"]["id"], "username": "rita", "display_name": "Rita Raman", "email": "rita@acme.example", "active": True, "created_at": "2026-10-16T09:00:00Z"})
        row = self.sql("SELECT role, weekly_capacity_hours FROM users WHERE username = 'rita'")[0]
        self.assertEqual(row, ("viewer", 0.0), "a viewer who adds no capacity")
        detail = self.ok(self.a.get("/api/clients/1"))
        self.assertEqual([c["username"] for c in detail["contacts"]], ["rita"])
        self.assertEqual(self.ok(self.a.get("/api/clients"))["items"][0]["contacts"], 1)
        self.assertNotIn(made["token"], json.dumps(detail))
        self.assertNotIn(made["token"], json.dumps(self.sql("SELECT details FROM activity")), "the token is never written down")

    def test_who_may_create_contacts_and_what_may_not_be_asked_for(self):
        for user in ("alice", "bob", "vera"):
            self.assertEqual(self.client(user).post("/api/clients/1/contacts", {"username": "rita", "display_name": "Rita"}).status, 403, user)
        for body in ({"username": "rita", "display_name": "R", "role": "admin"}, {"username": "rita", "display_name": "R", "weekly_capacity_hours": 40}, {"username": "rita", "display_name": "R", "client": 2}):
            r = self.root.post("/api/clients/1/contacts", body)
            self.assertEqual(r.status, 422, body)
        r = self.root.post("/api/clients/1/contacts", {"username": "x"})
        self.assertEqual((r.status, r.error_code), (422, "validation_error"))
        self.assertEqual(self.root.post("/api/clients/1/contacts", {"username": "Bad Name", "display_name": "R"}).status, 422)
        self.assertEqual(self.root.post("/api/clients/9/contacts", {"username": "rita", "display_name": "R"}).error_code, "client_not_found")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM client_users")[0][0], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM users WHERE username = 'rita'")[0][0], 0, "nothing half-created")

    def test_a_username_is_taken_by_staff_and_contacts_alike(self):
        self.contact(self.acme)
        for name in ("rita", "bob", "admin"):
            r = self.root.post("/api/clients/1/contacts", {"username": name, "display_name": "Again"})
            self.assertEqual((r.status, r.error_code), (409, "username_taken"), name)
        r = self.root.post("/api/users", {"username": "rita", "display_name": "Staff Rita", "role": "member"})
        self.assertEqual((r.status, r.error_code), (409, "username_taken"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM client_users")[0][0], 1, "a refused contact leaves no row behind")

    def test_a_contact_is_not_staff(self):
        contact = self.contact(self.acme)
        listed = [u["username"] for u in self.ok(self.client("vera").get("/api/users"))["items"]]
        self.assertNotIn("rita", listed)
        self.assertNotIn("rita", [u["username"] for u in self.ok(self.root.get("/api/users", query={"include_inactive": "true"}))["items"]])
        r = self.root.patch("/api/users/%d" % contact["id"], {"role": "admin"})
        self.assertEqual((r.status, r.error_code), (409, "client_contact"), "no way to promote a contact")
        self.assertEqual(self.sql("SELECT role FROM users WHERE username = 'rita'")[0][0], "viewer")
        for user_field in ("owner", "members"):
            body = {"key": "NEW", "name": "New", "owner": "rita"} if user_field == "owner" else {"key": "NEW", "name": "New", "members": ["rita"]}
            r = self.a.post("/api/projects", body)
            self.assertEqual(r.status, 422, user_field)
            self.assertIn("client contact", r.json["error"]["details"][user_field])
        r = self.a.post("/api/projects/WEB/members", {"username": "rita"})
        self.assertEqual(r.status, 422)
        self.make_task("WEB", "A task")
        r = self.a.patch("/api/tasks/WEB-1", {"assignee": "rita"})
        self.assertEqual(r.status, 422, "she is not a member, and cannot become one")
        with self.app.session(write=False) as ctx:
            self.assertIsNone(users.find_by_identity(ctx.conn, "Rita Raman"), "an import cannot match a contact by name")
            self.assertIsNone(users.find_by_identity(ctx.conn, "rita"))
            self.assertIsNotNone(users.find_by_identity(ctx.conn, "bob"))
            self.assertTrue(users.is_client_contact(ctx.conn, contact["id"]))
            self.assertFalse(users.is_client_contact(ctx.conn, 1))

    def test_a_csv_import_does_not_turn_a_contact_into_a_member(self):
        self.contact(self.acme)
        csv_text = "Summary,Assignee\nA task,rita\nAnother,bob\n"
        report = self.ok(self.a.request("POST", "/api/projects/WEB/import", raw=csv_text.encode(), content_type="text/csv", query={"add_missing_members": "true"}))
        self.assertEqual(report["created"], 2)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM project_members pm JOIN users u ON u.id = pm.user_id WHERE u.username = 'rita'")[0][0], 0)
        tasks = self.ok(self.a.get("/api/tasks", query={"project": "WEB", "sort": "key"}))["items"]
        self.assertEqual([(t["title"], t["assignee"] and t["assignee"]["username"]) for t in tasks], [("A task", None), ("Another", "bob")])

    def test_deactivating_reactivating_and_rotating(self):
        contact = self.contact(self.acme)
        c = self.client("rita")
        self.assertEqual(c.get("/api/portal/projects").status, 200)
        path = "/api/clients/1/contacts/%d" % contact["id"]
        self.assertEqual(self.ok(self.root.patch(path, {"active": False}))["active"], False)
        r = c.get("/api/portal/projects")
        self.assertEqual((r.status, r.error_code), (401, "unauthorized"), "a deactivated contact is signed out at once")
        self.ok(self.root.patch(path, {"active": True, "display_name": "Rita R.", "email": "rita@acme.example"}))
        self.assertEqual(c.get("/api/portal/projects").status, 200)
        old = self.tokens["rita"]
        made = self.ok(self.root.post(path + "/rotate-token"))
        self.assertEqual(made["contact"]["username"], "rita")
        self.assertEqual(self.client(token=old).get("/api/portal/projects").status, 401)
        self.assertEqual(self.client(token=made["token"]).get("/api/portal/projects").status, 200)
        r = self.root.patch(path, {"role": "admin"})
        self.assertEqual(r.status, 422, "only a name, an e-mail and active can be changed")
        self.assertEqual(self.root.patch(path, {"email": "not an email"}).status, 422)
        self.assertEqual(self.sql("SELECT role FROM users WHERE username = 'rita'")[0][0], "viewer")
        for user in ("alice", "bob", "vera"):
            self.assertEqual(self.client(user).patch(path, {"active": False}).status, 403, user)
            self.assertEqual(self.client(user).post(path + "/rotate-token").status, 403, user)

    def test_a_contact_belongs_to_its_own_client_only(self):
        beta = self.new_client("Beta")
        contact = self.contact(self.acme)
        for method, path in (("PATCH", "/api/clients/%d/contacts/%d" % (beta["id"], contact["id"])), ("POST", "/api/clients/%d/contacts/%d/rotate-token" % (beta["id"], contact["id"]))):
            r = self.root.request(method, path, {"active": False} if method == "PATCH" else None)
            self.assertEqual((r.status, r.error_code), (404, "contact_not_found"), method)
        staff = self.sql("SELECT id FROM users WHERE username = 'bob'")[0][0]
        r = self.root.patch("/api/clients/1/contacts/%d" % staff, {"active": False})
        self.assertEqual((r.status, r.error_code), (404, "contact_not_found"), "a staff member is not a contact")
        self.assertEqual(self.root.patch("/api/clients/1/contacts/abc", {"active": False}).error_code, "contact_not_found")
        self.assertEqual(self.sql("SELECT active FROM users WHERE username = 'bob'")[0][0], 1)

    def test_the_account_actions_are_audited_but_never_sent_to_webhooks(self):
        contact = self.contact(self.acme)
        self.ok(self.root.patch("/api/clients/1/contacts/%d" % contact["id"], {"active": False}))
        self.ok(self.root.post("/api/clients/1/contacts/%d/rotate-token" % contact["id"]))
        actions = [a for (a,) in self.sql("SELECT action FROM activity WHERE action LIKE 'user.%' ORDER BY id")]
        self.assertEqual(actions[-3:], ["user.client_contact_added", "user.client_contact_updated", "user.token_rotated"])
        added = json.loads(self.sql("SELECT details FROM activity WHERE action = 'user.client_contact_added'")[0][0])
        self.assertEqual(added, {"client": "Acme", "username": "rita"})


class DeliveryTests(WebhookCase):
    def test_client_events_can_be_subscribed_to_and_contact_events_cannot(self):
        self.make_project("WEB")
        hook = self.make_hook(name="Clients", events=["client.*"])
        everything = self.make_hook(name="All", events=["*"])
        alice = self.client("alice")
        made = self.ok(alice.post("/api/clients", {"name": "Acme"}), 201)
        self.ok(alice.request("PUT", "/api/clients/1/projects/WEB", {"share": "full"}))
        self.ok(alice.request("PUT", "/api/clients/1/projects/WEB", {"update": "Hello"}))
        self.ok(self.admin.post("/api/clients/%d/contacts" % made["id"], {"username": "rita", "display_name": "Rita"}), 201)
        self.ok(alice.delete("/api/clients/1/projects/WEB"), 204)
        self.assertEqual(self.events_queued(hook["id"]), ["client.created", "client.project_linked", "client.project_updated", "client.project_unlinked"])
        self.assertNotIn("user.client_contact_added", self.events_queued(everything["id"]), "people and credentials are never delivered")
        catalogue = self.ok(self.admin.get("/api/webhooks/events"))
        self.assertIn("client.project_linked", catalogue["events"])
        self.assertIn("client.*", catalogue["patterns"])


# ------------------------------------------------------------------------------------------------------- isolation


class World(ClientCase):
    """Two clients and a project that belongs to nobody, every one of them full of things that must not cross over."""

    def setUp(self):
        super().setUp()
        for key, name, who in (("ACME", "Acme Portal Rebuild", "bob"), ("BETA", "Beta Secret Migration", "carol"), ("INT", "Internal Margin Review", "bob")):
            self.make_project(key, members=("bob", "carol"), name=name, description="DESC-%s internal notes" % key)
            self.make_task(key, "Task for %s" % key, assignee=who, due_date="2026-10-20", estimate_hours=10, description="TASKDESC-%s" % key, labels=["lbl-%s" % key.lower()])
            self.ok(self.client(who).post("/api/tasks/%s-1/comments" % key, {"body": "COMMENT-%s hours are over budget" % key}), 201)
            self.ok(self.client(who).post("/api/tasks/%s-1/time" % key, {"hours": 2, "work_date": "2026-10-15", "note": "TIMENOTE-%s" % key}), 201)
            self.ok(self.a.post("/api/projects/%s/baselines" % key, {"name": "BASELINE-%s" % key}), 201)
            self.ok(self.a.post("/api/projects/%s/milestones" % key, {"name": "Milestone %s" % key, "due_date": "2026-11-01"}), 201)
        self.acme, self.beta = self.new_client("Acme Corp"), self.new_client("Beta Ltd")
        self.share(self.acme, "ACME", "full", update="Hello Acme")
        self.share(self.beta, "BETA", "full", update="Hello Beta")
        self.contact(self.acme, "rita", "Rita Raman")
        self.contact(self.beta, "ben", "Ben Bauer")

    def everything_the_portal_tells(self, username):
        """Every answer the portal gives that contact, as one string."""
        c = self.client(username)
        out = []
        listing = c.get("/api/portal/projects")
        out.append(listing.text)
        for item in listing.json["items"]:
            for days in (None, 1, 90):
                out.append(c.get("/api/portal/projects/" + item["key"], query={"days": days} if days else None).text)
        return "\n".join(out)

    def test_a_contact_sees_its_own_project_and_nothing_of_anyone_elses(self):
        everything = {u: self.everything_the_portal_tells(u) + self.client(u).get("/api/me").text + self.client(u).get("/api/meta").text for u in ("rita", "ben")}
        self.assertIn("Acme Portal Rebuild", everything["rita"])
        self.assertIn("Hello Acme", everything["rita"])
        for marker in ("BETA", "Beta", "Hello Beta", "Ben Bauer", "Internal Margin", "INT"):
            self.assertNotIn(marker, everything["rita"], marker)
        self.assertIn("Beta Secret Migration", everything["ben"])
        for marker in ("ACME", "Acme", "Hello Acme", "Rita", "Internal Margin", "INT"):
            self.assertNotIn(marker, everything["ben"], marker)

    def test_nothing_internal_reaches_the_portal_even_for_the_contacts_own_project(self):
        text = self.everything_the_portal_tells("rita")
        for marker in (
            "DESC-ACME", "TASKDESC-ACME", "COMMENT-ACME", "TIMENOTE-ACME", "BASELINE-ACME", "lbl-acme",  # what the task and project hold
            "Bob", "Carol", "bob", "carol", "Admin", "admin", "Vera", "Rita",  # the people (the project manager, Alice, is the one exception)
            "estimate", "logged", "hours", "capacity", "token", "email", "weekly",
        ):
            self.assertNotIn(marker, text, marker)
        report = self.client("rita").get("/api/portal/projects/ACME").json
        self.assertEqual(report["project"]["owner"], "Alice")
        self.assertNotIn("assignee", json.dumps(report))
        self.assertNotIn("explanation", json.dumps(report))

    def test_the_other_clients_project_is_not_found_in_exactly_the_way_a_missing_one_is(self):
        c = self.client("rita")
        foreign, missing = c.get("/api/portal/projects/BETA"), c.get("/api/portal/projects/ZZZ")
        self.assertEqual((foreign.status, foreign.error_code, missing.status, missing.error_code), (404, "project_not_found", 404, "project_not_found"))
        self.assertEqual(foreign.text.replace("BETA", "KEY"), missing.text.replace("ZZZ", "KEY"), "the same answer, word for word")
        for key in ("INT", "beta", "Beta", "BETA%20", "..", "%2e%2e", "ACME/../BETA"):
            self.assertEqual(c.get("/api/portal/projects/" + key).status, 404, key)

    def test_a_contact_cannot_ask_for_someone_elses_portal(self):
        c = self.client("rita")
        self.assertEqual(c.get("/api/portal/projects", query={"client": self.beta["id"]}).error_code, "client_access_denied")
        self.assertEqual(c.get("/api/portal/projects/BETA", query={"client": self.beta["id"]}).status, 403)
        self.assertEqual(c.get("/api/portal/projects/ACME", query={"client": self.beta["id"]}).status, 403)
        self.assertEqual(c.get("/api/portal/projects", query={"client": "99"}).status, 403)
        self.assertEqual(c.get("/api/portal/projects", query={"client": self.acme["id"]}).status, 200, "her own id is fine")
        self.assertEqual(c.get("/api/portal/projects", query={"client": ""}).status, 200)

    def test_what_a_contact_may_call_is_exactly_this(self):
        allowed = sorted((r.method, r.pattern) for r in ROUTES if r.client)
        self.assertEqual(allowed, [("GET", "/api/me"), ("GET", "/api/meta"), ("GET", "/api/portal/projects"), ("GET", "/api/portal/projects/{key}")],
                         "a route that a client contact may call has to be added here on purpose, and thought about")

    def test_every_other_route_refuses_a_contact_whatever_it_is_asked(self):
        c = self.client("rita")
        values = {"key": ("ACME", "BETA", "ACME-1", "BETA-1", "INT-1"), "client_id": ("1", "2"), "user_id": ("1", "6", "7")}
        tried = 0
        for r in ROUTES:
            if r.public or r.client:
                continue
            names = re.findall(r"\{(\w+)\}", r.pattern)
            for combo in itertools.product(*[values.get(n, ("1", "ACME-1", "BETA-1", "7")) for n in names]):
                path = r.pattern
                for name, value in zip(names, combo, strict=True):
                    path = path.replace("{%s}" % name, value)
                response = c.request(r.method, path, {} if r.method in ("POST", "PATCH", "PUT") else None)
                self.assertEqual((response.status, response.error_code), (403, "client_access_denied"), (r.method, path))
                tried += 1
        self.assertGreater(tried, 150, "the enumeration itself is broken")

    def test_nor_does_a_trick_with_the_verb_or_the_path_get_through(self):
        c = self.client("rita")
        for method in ("DELETE", "PATCH", "PUT", "POST", "OPTIONS"):
            r = c.request(method, "/api/portal/projects/ACME")
            self.assertEqual(r.status, 405, method)
            self.assertNotIn("Acme Portal", r.text, method)
        self.assertEqual(c.request("HEAD", "/api/portal/projects/ACME").status, 200, "HEAD is GET without a body")
        self.assertEqual(c.request("HEAD", "/api/projects/ACME").status, 403)
        self.assertEqual(c.request("HEAD", "/api/projects/ACME").body, b"")
        self.assertEqual(c.get("/api/health").status, 200)  # the public route stays public
        for path in ("/api/projects/ACME", "/api/tasks/ACME-1", "/api/projects/ACME/summary", "/api/reports/portfolio", "/api/reports/projection", "/api/projects/ACME/projection",
                     "/api/projects/ACME/baselines", "/api/projects/ACME/baselines/1", "/api/projects/ACME/dependencies", "/api/tasks", "/api/tasks/ACME-1/attachments",
                     "/api/tasks/ACME-1/time", "/api/projects/ACME/activity", "/api/users", "/api/clients", "/api/clients/1", "/api/activity", "/api/webhooks", "/api/project-fields"):
            r = c.get(path)
            self.assertEqual((r.status, r.error_code), (403, "client_access_denied"), path)

    def test_a_contacts_token_does_nothing_for_staff_things_even_when_it_is_asked_nicely(self):
        c = self.client("rita")
        self.assertEqual(c.post("/api/projects/ACME/tasks", {"title": "x"}).status, 403)
        self.assertEqual(c.patch("/api/tasks/ACME-1", {"status": "done"}).status, 403)
        self.assertEqual(c.post("/api/tasks/ACME-1/comments", {"body": "hi"}).status, 403)
        self.assertEqual(c.post("/api/users/%d/rotate-token" % self.sql("SELECT id FROM users WHERE username='rita'")[0][0]).status, 403)
        self.assertEqual(c.patch("/api/users/1", {"role": "admin"}).status, 403)
        self.assertEqual(self.sql("SELECT status FROM tasks WHERE project_id = 1")[0][0], "todo")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM comments WHERE body = 'hi'")[0][0], 0)

    def test_a_suspended_client_is_shut_out_completely_and_comes_back_with_its_token(self):
        c = self.client("rita")
        self.ok(self.a.patch("/api/clients/%d" % self.acme["id"], {"active": False}))
        for path in ("/api/me", "/api/meta", "/api/portal/projects", "/api/portal/projects/ACME", "/api/projects"):
            r = c.get(path)
            self.assertEqual((r.status, r.error_code), (403, "client_suspended"), path)
        self.assertEqual(self.client("ben").get("/api/portal/projects").status, 200, "the other client is untouched")
        self.ok(self.a.patch("/api/clients/%d" % self.acme["id"], {"active": True}))
        self.assertEqual(c.get("/api/portal/projects/ACME").status, 200)

    def test_moving_or_unsharing_a_project_takes_effect_at_once(self):
        c = self.client("rita")
        self.assertEqual(c.get("/api/portal/projects/ACME").status, 200)
        self.share(self.acme, "ACME", "none")
        self.assertEqual(c.get("/api/portal/projects/ACME").status, 404)
        self.assertEqual(c.get("/api/portal/projects").json["items"], [])
        self.share(self.acme, "ACME", "summary")
        self.assertEqual(c.get("/api/portal/projects/ACME").json["level"], "summary")
        self.ok(self.a.delete("/api/clients/%d/projects/ACME" % self.acme["id"]), 204)
        self.assertEqual(c.get("/api/portal/projects/ACME").status, 404)
        self.share(self.beta, "ACME", "full")  # now it is Beta's
        self.assertEqual(c.get("/api/portal/projects/ACME").status, 404)
        self.assertEqual(self.client("ben").get("/api/portal/projects/ACME").status, 200)
        self.assertEqual(self.client("ben").get("/api/portal/projects/BETA").status, 200)

    def test_an_archived_project_leaves_the_portal(self):
        self.ok(self.a.patch("/api/projects/ACME", {"status": "archived"}))
        self.assertEqual(self.client("rita").get("/api/portal/projects/ACME").status, 404)
        self.assertEqual(self.client("rita").get("/api/portal/projects").json["items"], [])
        self.ok(self.a.patch("/api/projects/ACME", {"status": "active"}))
        self.assertEqual(self.client("rita").get("/api/portal/projects/ACME").status, 200)

    def test_the_summary_level_shows_no_task_at_all(self):
        self.share(self.acme, "ACME", "summary")
        report = self.client("rita").get("/api/portal/projects/ACME").json
        self.assertEqual(report["level"], "summary")
        for absent in ("forecast", "completed", "upcoming", "attention", "changes"):
            self.assertNotIn(absent, report)
        self.assertNotIn("Task for ACME", json.dumps(report))
        self.assertEqual(set(report["milestones"]["items"][0]), {"name", "due_date", "status", "tasks_done", "tasks_total", "progress"})
        self.share(self.acme, "ACME", "full")
        full = self.client("rita").get("/api/portal/projects/ACME").json
        self.assertIn("Task for ACME", json.dumps(full))

    def test_staff_can_preview_what_a_client_sees_and_only_what_a_client_sees(self):
        for user in ("admin", "alice", "bob", "vera"):
            c = self.client(user)
            listing = self.ok(c.get("/api/portal/projects", query={"client": self.acme["id"]}))
            self.assertEqual([i["key"] for i in listing["items"]], ["ACME"], user)
            self.assertEqual(listing["client"], {"id": 1, "name": "Acme Corp"})
            report = self.ok(c.get("/api/portal/projects/ACME", query={"client": self.acme["id"]}))
            self.assertNotIn("TASKDESC", json.dumps(report), "the preview is the portal, not a back door to more")
            self.assertEqual(c.get("/api/portal/projects/BETA", query={"client": self.acme["id"]}).status, 404, "it is the client's portal")
        r = self.a.get("/api/portal/projects")
        self.assertEqual((r.status, r.error_code), (422, "validation_error"), "staff have to say whose")
        self.assertEqual(self.a.get("/api/portal/projects", query={"client": "99"}).error_code, "client_not_found")
        for bad in ("abc", "1.5", "-1", "0", "²", "99999999999999999999"):
            self.assertEqual(self.a.get("/api/portal/projects", query={"client": bad}).status, 404, bad)
        self.assertEqual(self.client().get("/api/portal/projects", query={"client": 1}).status, 401)

    def test_who_am_i(self):
        me = self.client("rita").get("/api/me").json
        self.assertEqual((me["username"], me["role"], me["client"]), ("rita", "viewer", {"id": 1, "name": "Acme Corp"}))
        self.assertNotIn("client", self.client("bob").get("/api/me").json)

    def test_the_overview_for_staff_knows_both_clients(self):
        items = {c["name"]: c for c in self.ok(self.a.get("/api/clients"))["items"]}
        self.assertEqual((items["Acme Corp"]["projects"]["shared"], items["Acme Corp"]["contacts"], items["Acme Corp"]["projects"]["open_tasks"]), (1, 1, 1))
        detail = self.ok(self.a.get("/api/clients/1"))
        self.assertEqual([p["key"] for p in detail["projects"]], ["ACME"])
        self.assertEqual(detail["available_projects"], [{"key": "INT", "name": "Internal Margin Review", "status": "active"}])
        self.assertNotIn("BETA", json.dumps(detail))


class ValidationTests(World):
    def test_days(self):
        c = self.client("rita")
        for good in ("1", "7", "07", "90"):
            self.assertEqual(c.get("/api/portal/projects/ACME", query={"days": good}).status, 200, good)
        for bad in ("0", "91", "x", "7.5", "-1", "1e1", " 7", "\u00b2", "1000", "\uff10", "\uff17"):
            r = c.get("/api/portal/projects/ACME", query={"days": bad})
            self.assertEqual((r.status, r.error_code), (422, "validation_error"), repr(bad))
        report = c.get("/api/portal/projects/ACME", query={"days": "3"}).json
        self.assertEqual(report["period"], {"from": "2026-10-14", "to": "2026-10-16", "days": 3})
