"""API tokens: minting, the limits on what a token may do, expiry and revocation, usage tracking, audit trail,
the schema upgrade and the command line."""

import contextlib
import dataclasses
import io
import json
import os
import sqlite3
from datetime import timedelta
from unittest import mock

from helpers import FIXED_NOW, ApiTestCase, Client

from zylker_pm import auth, cli, db
from zylker_pm.app import App
from zylker_pm.db import init_db
from zylker_pm.services import tokens, webhooks

OFFICE = "203.0.113.7"  # the address the test client connects from by default
ELSEWHERE = "198.51.100.9"


class TokenBase(ApiTestCase):
    def mint(self, who="alice", status=201, **body):
        body.setdefault("name", "ci-%d" % len(self.tokens))
        r = self.client(who).post("/api/tokens", body)
        self.assertEqual(r.status, status, r.text)
        made = r.json
        if status == 201:
            self.tokens["tok:" + made["name"]] = made["token"]
        return made

    def using(self, made):
        return Client(self.app, made["token"])

    def listing(self, who="alice", **query):
        return self.ok(self.client(who).get("/api/tokens", query=query))["items"]

    def audit(self, **query):
        return self.ok(self.admin.get("/api/audit", query=query))["items"]

    def row(self, token_id):
        conn = db.connect(self.config.db_path)
        try:
            return dict(conn.execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone())
        finally:
            conn.close()

    def at(self, **delta):
        self.now = FIXED_NOW + timedelta(**delta)


class CreationTests(TokenBase):
    def test_a_user_mints_a_token_and_is_shown_it_once(self):
        made = self.mint("alice", name="nightly build")
        self.assertTrue(made["token"].startswith("zpt_") and len(made["token"]) > 40)
        self.assertEqual(made["hint"], "zpt_..." + made["token"][-4:])
        self.assertEqual(
            {k: made[k] for k in ("name", "user", "role_cap", "read_only", "allowed_ips", "status", "use_count", "last_used_at", "revoked_at")},
            {"name": "nightly build", "user": "alice", "role_cap": "manager", "read_only": False, "allowed_ips": [], "status": "active", "use_count": 0, "last_used_at": None, "revoked_at": None},
        )
        self.assertEqual(made["expires_at"], "2026-11-15T09:00:00Z")  # thirty days from the fixed clock
        self.assertFalse(made["expires_soon"])
        for text in (json.dumps(self.listing()), self.client("alice").get("/api/tokens/%d" % made["id"]).text):
            self.assertNotIn(made["token"], text)
            self.assertNotIn(auth.hash_token(made["token"]), text)
            self.assertNotIn("token_hash", text)
        self.assertEqual(self.listing()[0]["hint"], made["hint"])

    def test_the_stored_value_is_a_hash_not_the_token(self):
        made = self.mint()
        stored = self.row(made["id"])
        self.assertEqual(stored["token_hash"], auth.hash_token(made["token"]))
        self.assertNotIn(made["token"], json.dumps(stored))

    def test_every_token_is_different(self):
        values = {self.mint(name="t%d" % i)["token"] for i in range(5)}
        self.assertEqual(len(values), 5)

    def test_lifetime_is_bounded(self):
        self.assertEqual(self.mint(name="one", expires_in_days=1)["expires_at"], "2026-10-17T09:00:00Z")
        self.assertEqual(self.mint(name="max", expires_in_days=365)["expires_at"], "2027-10-16T09:00:00Z")
        for bad in (0, -1, 366, 1.5, "30", True, None, [30]):
            r = self.client("alice").post("/api/tokens", {"name": "bad", "expires_in_days": bad})
            self.assertEqual(r.status, 422, bad)
            self.assertIn("expires_in_days", r.json["error"]["details"])
        self.assertEqual(self.client("alice").post("/api/tokens", {"name": "bad", "expires_in_days": 366}).json["error"]["details"]["expires_in_days"], "must be <= 365")

    def test_the_name_is_checked(self):
        for bad in (None, "", "   ", "x" * 61, 5, ["a"], "line\nbreak", "bell\x07"):
            r = self.client("alice").post("/api/tokens", {"name": bad})
            self.assertEqual(r.status, 422, repr(bad))
            self.assertIn("name", r.json["error"]["details"])
        self.assertEqual(self.client("alice").post("/api/tokens", {}).json["error"]["details"], {"name": "required"})
        self.assertEqual(self.mint(name="  padded  ")["name"], "padded")
        self.assertEqual(self.mint(name="x" * 60)["name"], "x" * 60)
        self.assertEqual(self.mint(name="Deploy \u2192 prod \u00e9")["name"], "Deploy \u2192 prod \u00e9")

    def test_unknown_fields_and_non_objects_are_refused(self):
        r = self.client("alice").post("/api/tokens", {"name": "x", "token": "zpt_mine", "revoked_at": None})
        self.assertEqual(r.status, 422)
        self.assertEqual(set(r.json["error"]["details"]), {"token", "revoked_at"})
        self.assertEqual(self.client("alice").request("POST", "/api/tokens", raw=b"[1]", content_type="application/json").status, 400)

    def test_a_token_can_be_capped_to_a_lower_role_but_never_a_higher_one(self):
        self.assertEqual(self.mint("alice", name="m", role_cap="member")["role_cap"], "member")
        self.assertEqual(self.mint("alice", name="v", role_cap="viewer")["role_cap"], "viewer")
        self.assertEqual(self.mint("alice", name="same", role_cap="manager")["role_cap"], "manager")
        r = self.client("alice").post("/api/tokens", {"name": "up", "role_cap": "admin"})
        self.assertEqual((r.status, r.json["error"]["details"]), (422, {"role_cap": "cannot be higher than the user's own role (manager)"}))
        self.assertEqual(self.client("bob").post("/api/tokens", {"name": "up", "role_cap": "manager"}).status, 422)
        self.assertEqual(self.client("alice").post("/api/tokens", {"name": "up", "role_cap": "root"}).status, 422)
        self.assertEqual(self.mint("admin", name="a")["role_cap"], "admin")

    def test_allowed_addresses_are_normalised(self):
        made = self.mint(allowed_ips=["203.0.113.7", "10.1.2.3/8", " 2001:db8::1 ", "203.0.113.7", "2001:DB8::/32"])
        self.assertEqual(made["allowed_ips"], ["203.0.113.7/32", "10.0.0.0/8", "2001:db8::1/128", "2001:db8::/32"])
        self.assertEqual(self.mint(name="none", allowed_ips=[])["allowed_ips"], [])
        self.assertEqual(self.mint(name="null", allowed_ips=None)["allowed_ips"], [])

    def test_bad_address_lists_are_refused(self):
        too_many = ["10.0.0.%d" % i for i in range(11)]
        for bad in ("203.0.113.7", {"a": 1}, ["not-an-ip"], ["203.0.113.7/33"], [5], [None], ["203.0.113.0/24", ""], too_many, [["10.0.0.1"]]):
            r = self.client("alice").post("/api/tokens", {"name": "ips", "allowed_ips": bad})
            self.assertEqual(r.status, 422, bad)
            self.assertIn("allowed_ips", r.json["error"]["details"])
        self.assertEqual(self.mint(name="ten", allowed_ips=too_many[:10])["allowed_ips"][-1], "10.0.0.9/32")

    def test_names_are_unique_among_a_users_active_tokens(self):
        first = self.mint(name="Ci")
        r = self.client("alice").post("/api/tokens", {"name": "ci"})  # case does not make it different
        self.assertEqual((r.status, r.error_code), (409, "token_name_taken"))
        self.assertEqual(self.mint("bob", name="Ci")["user"], "bob")  # another user may use it
        self.client("alice").delete("/api/tokens/%d" % first["id"])
        self.assertEqual(self.mint("alice", name="ci")["status"], "active")  # revoked names are free again
        self.at(days=31)
        self.assertEqual(self.mint("alice", name="ci")["status"], "active")  # so are expired ones

    def test_at_most_ten_tokens_are_active_per_user(self):
        made = [self.mint(name="t%d" % i) for i in range(10)]
        r = self.client("alice").post("/api/tokens", {"name": "eleven"})
        self.assertEqual((r.status, r.error_code), (409, "token_limit_reached"))
        self.assertEqual(self.mint("bob", name="elsewhere")["user"], "bob")  # the limit is per user
        self.client("alice").delete("/api/tokens/%d" % made[0]["id"])
        self.assertEqual(self.mint(name="eleven")["status"], "active")
        self.assertEqual(self.client("alice").post("/api/tokens", {"name": "twelve"}).status, 409)
        self.at(days=31)  # expired tokens no longer count
        self.assertEqual(self.mint(name="twelve")["status"], "active")

    def test_every_role_can_mint_for_themselves(self):
        for user in ("admin", "alice", "bob", "vera"):
            made = self.mint(user, name="own")
            self.assertEqual(made["user"], user)
        self.assertEqual(self.client().post("/api/tokens", {"name": "x"}).status, 401)

    def test_an_administrator_can_mint_for_another_user(self):
        made = self.mint("admin", name="service", user="bob", role_cap="viewer")
        self.assertEqual((made["user"], made["role_cap"]), ("bob", "viewer"))
        self.assertEqual(self.mint("admin", name="default-cap", user="BOB")["role_cap"], "member")  # the owner's role, not the admin's
        r = self.client("admin").post("/api/tokens", {"name": "up", "user": "bob", "role_cap": "manager"})
        self.assertEqual(r.status, 422)
        self.assertEqual(self.client("admin").post("/api/tokens", {"name": "x", "user": "nobody"}).json["error"]["details"], {"user": "no such user"})
        self.assertEqual(self.mint("admin", name="mine", user="admin")["user"], "admin")
        created = [e for e in self.audit(action="token.created") if e["details"]["name"] == "service"][0]
        self.assertEqual((created["actor"]["username"], created["details"]["user"]), ("admin", "bob"))

    def test_nobody_else_can_mint_for_someone_else(self):
        for who in ("alice", "bob", "vera"):
            r = self.client(who).post("/api/tokens", {"name": "x", "user": "carol"})
            self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), who)
        self.assertEqual(self.mint("bob", name="myself", user="bob")["user"], "bob")  # naming yourself is fine

    def test_a_deactivated_user_cannot_be_given_a_token(self):
        bob = self.ok(self.admin.get("/api/users"))["items"]
        bob_id = next(u["id"] for u in bob if u["username"] == "bob")
        self.ok(self.admin.patch("/api/users/%d" % bob_id, {"active": False}))
        r = self.client("admin").post("/api/tokens", {"name": "x", "user": "bob"})
        self.assertEqual((r.status, r.error_code), (409, "user_inactive"))

    def test_the_audit_log_records_creation_without_the_secret(self):
        made = self.mint("alice", name="nightly", role_cap="member", read_only=True, allowed_ips=["203.0.113.0/24"])
        event = self.audit(action="token.created")[0]
        self.assertEqual((event["category"], event["severity"], event["actor"]["username"]), ("token", "notice", "alice"))
        self.assertEqual(
            event["details"],
            {"id": made["id"], "name": "nightly", "user": "alice", "hint": made["hint"], "role_cap": "member", "read_only": True, "allowed_ips": ["203.0.113.0/24"], "expires_at": made["expires_at"]},
        )
        everything = json.dumps(self.audit()) + self.admin.get("/api/audit/export.csv").text
        self.assertNotIn(made["token"], everything)
        self.assertNotIn(auth.hash_token(made["token"]), everything)

    def test_limits_are_published(self):
        out = self.ok(self.client("alice").get("/api/tokens"))["limits"]
        self.assertEqual(out, {"max_active_per_user": 10, "default_days": 30, "max_days": 365, "max_addresses": 10, "keep_finished_days": 30})


class UseTests(TokenBase):
    def test_a_token_acts_as_its_owner(self):
        made = self.mint("alice", name="ci")
        me = self.ok(self.using(made).get("/api/me"))
        self.assertEqual((me["username"], me["role"]), ("alice", "manager"))
        project = self.ok(self.using(made).post("/api/projects", {"key": "TOK", "name": "Made by a token", "members": ["bob"]}), 201)
        self.assertEqual(project["owner"]["username"], "alice")
        self.assertEqual(self.ok(self.using(made).get("/api/projects/TOK"))["key"], "TOK")

    def test_the_cap_lowers_what_the_token_may_do(self):
        self.make_project("WEB")
        capped = self.mint("alice", name="member-level", role_cap="member")
        c = self.using(capped)
        self.assertEqual(self.ok(c.get("/api/me"))["role"], "member")
        r = c.post("/api/projects", {"key": "NEW", "name": "Not allowed"})
        self.assertEqual((r.status, r.error_code), (403, "insufficient_role"))
        self.assertIn("(you are 'member')", r.json["error"]["message"])
        self.assertEqual(self.client("alice").post("/api/projects", {"key": "NEW", "name": "Allowed"}).status, 201)  # the primary token still can
        self.assertEqual(c.post("/api/projects/WEB/tasks", {"title": "Members may"}).status, 201)
        viewer = self.using(self.mint("alice", name="viewer-level", role_cap="viewer"))
        self.assertEqual(viewer.get("/api/projects/WEB").status, 200)
        self.assertEqual(viewer.post("/api/projects/WEB/tasks", {"title": "Viewers may not"}).status, 403)

    def test_an_administrators_token_can_be_capped_to_stay_away_from_admin_routes(self):
        capped = self.mint("admin", name="not-admin", role_cap="manager")
        self.assertEqual(self.using(capped).get("/api/audit").status, 403)
        self.assertEqual(self.using(capped).get("/api/webhooks").status, 403)
        self.assertEqual(self.client("admin").get("/api/audit").status, 200)
        uncapped = self.mint("admin", name="full")
        self.assertEqual(self.using(uncapped).get("/api/audit").status, 200)

    def test_a_token_follows_its_owners_role_down_but_not_up(self):
        made = self.mint("alice", name="ci")  # cap: manager
        alice_id = next(u["id"] for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == "alice")
        self.ok(self.admin.patch("/api/users/%d" % alice_id, {"role": "member"}))
        self.assertEqual(self.ok(self.using(made).get("/api/me"))["role"], "member")  # demoted with her
        self.assertEqual(self.using(made).post("/api/projects", {"key": "NEW", "name": "x"}).status, 403)
        self.ok(self.admin.patch("/api/users/%d" % alice_id, {"role": "admin"}))
        self.assertEqual(self.ok(self.using(made).get("/api/me"))["role"], "manager")  # promotion does not widen the cap
        self.assertEqual(self.using(made).get("/api/audit").status, 403)

    def test_a_read_only_token_reads_but_never_writes(self):
        self.make_project("WEB")
        task = self.make_task("WEB", "Existing")
        ro = self.using(self.mint("alice", name="reader", read_only=True))
        for path in ("/api/me", "/api/projects", "/api/projects/WEB", "/api/tasks/" + task["key"], "/api/tokens/current"):
            self.assertEqual(ro.get(path).status, 200, path)
        self.assertEqual(ro.get("/api/search", query={"q": "Existing"}).status, 200)
        self.assertEqual(ro.request("HEAD", "/api/projects").status, 200)
        attempts = [
            ("POST", "/api/projects/WEB/tasks", {"title": "No"}),
            ("PATCH", "/api/tasks/" + task["key"], {"title": "No"}),
            ("DELETE", "/api/tasks/" + task["key"], None),
            ("POST", "/api/tasks/%s/comments" % task["key"], {"body": "No"}),
            ("POST", "/api/projects", {"key": "NEW", "name": "No"}),
            ("POST", "/api/tokens", {"name": "child"}),
            ("DELETE", "/api/tokens", None),
        ]
        for method, path, body in attempts:
            r = ro.request(method, path, body)
            self.assertEqual((r.status, r.error_code), (403, "token_read_only"), (method, path))
        self.assertEqual(self.ok(self.client("alice").get("/api/tasks/" + task["key"]))["title"], "Existing")

    def test_read_only_is_checked_before_the_role(self):
        ro = self.using(self.mint("vera", name="reader", read_only=True))
        self.assertEqual(ro.post("/api/projects", {"key": "NEW", "name": "x"}).error_code, "token_read_only")
        self.assertEqual(self.client("vera").post("/api/projects", {"key": "NEW", "name": "x"}).error_code, "insufficient_role")

    def test_a_read_only_token_can_still_pull_summaries_and_exports(self):
        self.make_project("WEB")
        ro = self.using(self.mint("alice", name="reports", read_only=True))
        self.assertEqual(ro.get("/api/projects/WEB/summary").status, 200)
        self.assertEqual(ro.get("/api/projects/WEB/export.csv").status, 200)

    def test_a_token_cannot_manage_tokens_or_replace_the_primary_token(self):
        made = self.mint("alice", name="ci")
        other = self.mint("alice", name="other")
        c = self.using(made)
        alice_id = self.ok(self.client("alice").get("/api/me"))["id"]
        refused = [
            c.get("/api/tokens"),
            c.get("/api/tokens/%d" % other["id"]),
            c.post("/api/tokens", {"name": "successor"}),
            c.delete("/api/tokens/%d" % other["id"]),
            c.delete("/api/tokens"),
            c.post("/api/users/%d/rotate-token" % alice_id),
        ]
        for r in refused:
            self.assertEqual((r.status, r.error_code), (403, "primary_token_required"), r.text)
        self.assertEqual(len(self.listing()), 2)  # nothing was minted or revoked
        self.assertEqual(self.client("alice").get("/api/me").status, 200)  # the primary token is intact
        self.assertEqual(c.get("/api/me").status, 200)

    def test_an_admin_token_cannot_rotate_anyones_primary_token_either(self):
        made = self.mint("admin", name="ci")
        bob_id = self.ok(self.client("bob").get("/api/me"))["id"]
        r = self.using(made).post("/api/users/%d/rotate-token" % bob_id)
        self.assertEqual((r.status, r.error_code), (403, "primary_token_required"))
        self.assertEqual(self.client("bob").get("/api/me").status, 200)
        self.assertEqual(self.client("admin").post("/api/users/%d/rotate-token" % bob_id).status, 200)  # the primary token can

    def test_rotating_the_primary_token_leaves_api_tokens_alone(self):
        made = self.mint("alice", name="ci")
        alice_id = self.ok(self.client("alice").get("/api/me"))["id"]
        self.ok(self.client("alice").post("/api/users/%d/rotate-token" % alice_id))
        self.assertEqual(self.client("alice").get("/api/me").status, 401)
        self.assertEqual(self.using(made).get("/api/me").status, 200)  # revoke them explicitly: DELETE /api/tokens

    def test_current_says_how_the_request_is_authenticated(self):
        self.assertEqual(self.ok(self.client("alice").get("/api/tokens/current")), {"type": "primary", "role": "manager"})
        made = self.mint("alice", name="ci", role_cap="member", read_only=True)
        now = self.ok(self.using(made).get("/api/tokens/current"))
        self.assertEqual((now["type"], now["role"], now["owner_role"], now["id"], now["name"]), ("api_token", "member", "manager", made["id"], "ci"))
        self.assertEqual((now["read_only"], now["status"], now["expires_at"]), (True, "active", made["expires_at"]))
        self.assertNotIn("token", now)
        self.assertEqual(self.client().get("/api/tokens/current").status, 401)

    def test_a_deactivated_owner_takes_every_token_with_them(self):
        made = [self.mint("bob", name="a"), self.mint("bob", name="b", read_only=True)]
        bob_id = self.ok(self.client("bob").get("/api/me"))["id"]
        self.ok(self.admin.patch("/api/users/%d" % bob_id, {"active": False}))
        for m in made:
            r = self.using(m).get("/api/me")
            self.assertEqual((r.status, r.error_code), (401, "unauthorized"))
        self.ok(self.admin.patch("/api/users/%d" % bob_id, {"active": True}))
        self.assertEqual(self.using(made[0]).get("/api/me").status, 200)  # nothing was deleted

    def test_tokens_work_with_the_bearer_scheme_in_any_case(self):
        made = self.mint("alice", name="ci")
        for header in ("Bearer " + made["token"], "bearer " + made["token"], "BEARER   " + made["token"] + "  "):
            self.assertEqual(Client(self.app).get("/api/me", headers={"Authorization": header}).status, 200, header)


class ExpiryAndRevocationTests(TokenBase):
    def test_a_token_stops_working_the_moment_it_expires(self):
        made = self.mint("alice", name="ci", expires_in_days=7)
        c = self.using(made)
        self.at(days=7, seconds=-1)
        self.assertEqual(c.get("/api/me").status, 200)
        self.at(days=7)
        r = c.get("/api/me")
        self.assertEqual((r.status, r.error_code), (401, "token_expired"))
        self.assertEqual(r.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(r.json["error"]["message"], "This API token has expired")
        self.at(days=400)
        self.assertEqual(c.get("/api/me").error_code, "token_expired")
        self.assertEqual(self.client("alice").get("/api/me").status, 200)  # the primary token does not expire

    def test_expiry_is_visible_in_the_listing(self):
        soon = self.mint("alice", name="soon", expires_in_days=3)
        later = self.mint("alice", name="later", expires_in_days=30)
        items = {t["name"]: t for t in self.listing()}
        self.assertEqual((items["soon"]["expires_soon"], items["later"]["expires_soon"]), (True, False))
        self.at(days=4)
        items = {t["name"]: t for t in self.listing()}
        self.assertEqual((items["soon"]["status"], items["soon"]["expires_soon"], items["later"]["status"]), ("expired", False, "active"))
        self.assertEqual([t["name"] for t in self.listing(status="expired")], ["soon"])
        self.assertEqual({soon["id"], later["id"]}, {t["id"] for t in self.listing()})

    def test_a_revoked_token_is_refused_with_its_own_reason(self):
        made = self.mint("alice", name="ci")
        self.ok(self.using(made).get("/api/me"))
        r = self.client("alice").delete("/api/tokens/%d" % made["id"])
        self.assertEqual((r.status, r.json["status"], r.json["revoked_at"]), (200, "revoked", "2026-10-16T09:00:00Z"))
        r = self.using(made).get("/api/me")
        self.assertEqual((r.status, r.error_code), (401, "token_revoked"))
        self.assertEqual(self.client("alice").get("/api/me").status, 200)

    def test_revoking_twice_changes_nothing_and_logs_once(self):
        made = self.mint("alice", name="ci")
        self.client("alice").delete("/api/tokens/%d" % made["id"])
        self.now += timedelta(hours=1)
        again = self.ok(self.client("alice").delete("/api/tokens/%d" % made["id"]))
        self.assertEqual(again["revoked_at"], "2026-10-16T09:00:00Z")
        self.assertEqual(len(self.audit(action="token.revoked")), 1)

    def test_an_unknown_token_is_just_invalid(self):
        for value in ("zpt_" + "A" * 43, "zpt_", "zpt_nonsense", "zpm_" + "A" * 43):
            r = Client(self.app, value).get("/api/me")
            self.assertEqual((r.status, r.error_code), (401, "unauthorized"), value)

    def test_a_primary_token_is_not_accepted_where_an_api_token_is_expected_or_vice_versa(self):
        made = self.mint("alice", name="ci")
        swapped = made["token"].replace("zpt_", "zpm_", 1)
        self.assertEqual(Client(self.app, swapped).get("/api/me").status, 401)
        self.assertEqual(Client(self.app, "zpt_" + self.tokens["alice"][4:]).get("/api/me").status, 401)

    def test_finished_tokens_are_listed_for_thirty_days_then_forgotten(self):
        gone_revoked = self.mint("alice", name="revoked")
        gone_expired = self.mint("alice", name="expired", expires_in_days=1)
        kept = self.mint("alice", name="kept", expires_in_days=365)
        self.client("alice").delete("/api/tokens/%d" % gone_revoked["id"])
        self.at(days=30)  # expired 29 days ago, revoked 30 days ago: both still listed
        self.mint("alice", name="trigger-1")
        self.assertEqual({t["name"] for t in self.listing()}, {"revoked", "expired", "kept", "trigger-1"})
        self.at(days=32)
        self.mint("alice", name="trigger-2")
        self.assertEqual({t["name"] for t in self.listing()}, {"kept", "trigger-1", "trigger-2"})
        self.assertEqual(self.client("alice").get("/api/tokens/%d" % gone_expired["id"]).error_code, "token_not_found")
        self.assertEqual(self.using(kept).get("/api/me").status, 200)
        self.assertEqual(self.using(gone_expired).get("/api/me").error_code, "unauthorized")  # no longer remembered as expired


class AddressTests(TokenBase):
    def test_a_token_with_an_allow_list_only_works_from_those_addresses(self):
        made = self.mint("alice", name="office", allowed_ips=["203.0.113.0/24", "2001:db8::/32"])
        c = self.using(made)
        self.assertEqual(c.get("/api/me").status, 200)  # the default test address is 203.0.113.7
        self.assertEqual(c.get("/api/me", remote_addr="203.0.113.250").status, 200)
        self.assertEqual(c.get("/api/me", remote_addr="2001:db8:ffff::1").status, 200)
        for outside in (ELSEWHERE, "203.0.114.1", "2001:db9::1"):
            r = c.get("/api/me", remote_addr=outside)
            self.assertEqual((r.status, r.error_code), (401, "token_ip_not_allowed"), outside)
        self.assertEqual(self.client("alice").get("/api/me", remote_addr=ELSEWHERE).status, 200)  # the primary token has no allow-list

    def test_a_single_address_and_an_ipv4_address_in_ipv6_notation(self):
        made = self.mint("alice", name="one", allowed_ips=["203.0.113.7"])
        c = self.using(made)
        self.assertEqual(c.get("/api/me").status, 200)
        self.assertEqual(c.get("/api/me", remote_addr="::ffff:203.0.113.7").status, 200)
        self.assertEqual(c.get("/api/me", remote_addr="203.0.113.8").error_code, "token_ip_not_allowed")

    def test_a_network_written_in_ipv6_notation_matches_the_ipv4_address_too(self):
        c = self.using(self.mint("alice", name="mapped", allowed_ips=["::ffff:203.0.113.0/120"]))
        self.assertEqual(c.get("/api/me").status, 200)  # 203.0.113.7
        self.assertEqual(c.get("/api/me", remote_addr="::ffff:203.0.113.200").status, 200)
        self.assertEqual(c.get("/api/me", remote_addr="203.0.114.7").error_code, "token_ip_not_allowed")

    def test_an_unknown_address_is_never_inside_an_allow_list(self):
        c = self.using(self.mint("alice", name="office", allowed_ips=["0.0.0.0/0"]))
        self.assertEqual(c.get("/api/me", remote_addr="not an address").error_code, "token_ip_not_allowed")
        self.assertEqual(c.get("/api/me", remote_addr="").error_code, "token_ip_not_allowed")
        self.assertEqual(self.using(self.mint("alice", name="open")).get("/api/me", remote_addr="").status, 200)

    def test_behind_a_proxy_the_forwarded_address_counts(self):
        config = dataclasses.replace(self.config, proxy_hops=1)
        app = App(config, clock=lambda: self.now, notifier=self.notifier)
        made = self.mint("alice", name="office", allowed_ips=["198.51.100.0/24"])
        c = Client(app, made["token"])
        self.assertEqual(c.get("/api/me", headers={"X-Forwarded-For": "198.51.100.20"}, remote_addr="10.0.0.1").status, 200)
        self.assertEqual(c.get("/api/me", headers={"X-Forwarded-For": "192.0.2.20"}, remote_addr="198.51.100.20").error_code, "token_ip_not_allowed")
        # a client cannot talk its way in by adding an entry on the left: only what the proxy appended counts
        self.assertEqual(c.get("/api/me", headers={"X-Forwarded-For": "198.51.100.20, 192.0.2.20"}, remote_addr="10.0.0.1").error_code, "token_ip_not_allowed")

    def test_address_matching_helper(self):
        self.assertTrue(auth.address_allowed("10.1.2.3", []))
        self.assertTrue(auth.address_allowed(None, []))
        self.assertTrue(auth.address_allowed("10.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(auth.address_allowed("11.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(auth.address_allowed(None, ["10.0.0.0/8"]))
        self.assertFalse(auth.address_allowed("garbage", ["10.0.0.0/8"]))
        self.assertTrue(auth.address_allowed("::ffff:10.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(auth.address_allowed("10.1.2.3", ["2001:db8::/32"]))
        self.assertFalse(auth.address_allowed("10.1.2.3", ["broken", "11.0.0.0/8"]))  # a damaged entry never opens anything


class VisibilityTests(TokenBase):
    def test_you_see_your_own_tokens_newest_first(self):
        a, b = self.mint("alice", name="a"), self.mint("alice", name="b")
        self.mint("bob", name="bobs")
        self.assertEqual([t["id"] for t in self.listing("alice")], [b["id"], a["id"]])
        self.assertEqual([t["name"] for t in self.listing("bob")], ["bobs"])
        self.assertEqual(self.listing("vera"), [])
        self.assertEqual([t["name"] for t in self.listing("alice", user="ALICE")], ["b", "a"])

    def test_only_administrators_see_other_peoples_tokens(self):
        self.mint("alice", name="a")
        self.mint("bob", name="bobs")
        self.assertEqual({t["name"] for t in self.listing("admin", user="*")}, {"a", "bobs"})
        self.assertEqual([t["name"] for t in self.listing("admin", user="bob")], ["bobs"])
        self.assertEqual(self.listing("admin"), [])  # by default the administrator sees their own
        for who, query in (("bob", {"user": "alice"}), ("bob", {"user": "*"}), ("vera", {"user": "admin"})):
            r = self.client(who).get("/api/tokens", query=query)
            self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), (who, query))
        self.assertEqual(self.client("admin").get("/api/tokens", query={"user": "ghost"}).status, 422)

    def test_someone_elses_token_does_not_exist_as_far_as_you_can_tell(self):
        mine = self.mint("alice", name="a")
        for method in ("get", "delete"):
            r = getattr(self.client("bob"), method)("/api/tokens/%d" % mine["id"])
            self.assertEqual((r.status, r.error_code), (404, "token_not_found"), method)
        self.assertEqual(self.using(self.mint("bob", name="b")).get("/api/me").status, 200)
        self.assertEqual(self.client("admin").get("/api/tokens/%d" % mine["id"]).json["user"], "alice")
        self.assertEqual(self.client("admin").delete("/api/tokens/%d" % mine["id"]).json["status"], "revoked")
        self.assertEqual(self.using(mine).get("/api/me").error_code, "token_revoked")

    def test_bad_ids_are_plain_404s(self):
        for path in ("/api/tokens/abc", "/api/tokens/99999", "/api/tokens/0", "/api/tokens/-1", "/api/tokens/1.5"):
            for method in ("get", "delete"):
                r = getattr(self.client("alice"), method)(path)
                self.assertEqual((r.status, r.error_code), (404, "token_not_found"), (method, path))

    def test_the_status_filter(self):
        live = self.mint("alice", name="live")
        dead = self.mint("alice", name="dead")
        old = self.mint("alice", name="old", expires_in_days=1)
        self.client("alice").delete("/api/tokens/%d" % dead["id"])
        self.at(days=2)
        names = lambda **q: sorted(t["name"] for t in self.listing(**q))  # noqa: E731
        self.assertEqual(names(), ["dead", "live", "old"])
        self.assertEqual(names(status="active"), ["live"])
        self.assertEqual(names(status="revoked"), ["dead"])
        self.assertEqual(names(status="expired,revoked"), ["dead", "old"])
        self.assertEqual(self.client("alice").get("/api/tokens", query={"status": "bogus"}).status, 422)
        self.assertEqual({live["id"], dead["id"], old["id"]}, {t["id"] for t in self.listing()})

    def test_revoke_everything_of_yours_at_once(self):
        a, b = self.mint("alice", name="a"), self.mint("alice", name="b")
        done = self.mint("alice", name="done")
        self.client("alice").delete("/api/tokens/%d" % done["id"])
        keep = self.mint("bob", name="bobs")
        self.assertEqual(self.ok(self.client("alice").delete("/api/tokens")), {"user": "alice", "revoked": 2})
        for made in (a, b):
            self.assertEqual(self.using(made).get("/api/me").error_code, "token_revoked")
        self.assertEqual(self.using(keep).get("/api/me").status, 200)
        self.assertEqual(self.ok(self.client("alice").delete("/api/tokens")), {"user": "alice", "revoked": 0})
        events = self.audit(action="token.revoked_all")
        self.assertEqual(len(events), 1)  # nothing to revoke, nothing logged
        self.assertEqual((events[0]["details"], events[0]["severity"]), ({"user": "alice", "count": 2}, "notice"))

    def test_an_administrator_can_revoke_everything_of_anyone(self):
        made = self.mint("bob", name="leaked")
        self.assertEqual(self.client("alice").delete("/api/tokens", query={"user": "bob"}).status, 403)
        self.assertEqual(self.client("bob").delete("/api/tokens", query={"user": "BOB"}).json["revoked"], 1)  # naming yourself is fine
        made = self.mint("bob", name="leaked-again")
        self.assertEqual(self.ok(self.admin.delete("/api/tokens", query={"user": "bob"})), {"user": "bob", "revoked": 1})
        self.assertEqual(self.using(made).get("/api/me").error_code, "token_revoked")
        self.assertEqual(self.admin.delete("/api/tokens", query={"user": "ghost"}).status, 422)
        revoked = self.audit(action="token.revoked_all")[0]
        self.assertEqual((revoked["actor"]["username"], revoked["details"]["user"]), ("admin", "bob"))


class UsageTests(TokenBase):
    def test_the_first_use_is_recorded_at_once_and_later_ones_are_batched(self):
        made = self.mint("alice", name="ci")
        c = self.using(made)
        c.get("/api/me")
        first = self.row(made["id"])
        self.assertEqual((first["last_used_at"], first["last_used_ip"], first["use_count"]), ("2026-10-16T09:00:00Z", OFFICE, 1))
        for _ in range(5):
            self.now += timedelta(seconds=5)
            c.get("/api/me", remote_addr="203.0.113.99")
        self.assertEqual(self.row(made["id"])["use_count"], 1)  # held in memory: reads do not turn into writes
        self.at(seconds=61)
        c.get("/api/me", remote_addr="203.0.113.99")
        later = self.row(made["id"])
        self.assertEqual((later["use_count"], later["last_used_at"], later["last_used_ip"]), (7, "2026-10-16T09:01:01Z", "203.0.113.99"))
        shown = self.listing()[0]
        self.assertEqual((shown["use_count"], shown["last_used_at"], shown["last_used_ip"]), (7, "2026-10-16T09:01:01Z", "203.0.113.99"))

    def test_each_token_is_tracked_on_its_own(self):
        a, b = self.mint("alice", name="a"), self.mint("alice", name="b")
        self.using(a).get("/api/me")
        self.using(a).get("/api/me")
        self.client("alice").get("/api/me")  # the primary token has nothing to track
        self.assertEqual((self.row(a["id"])["use_count"], self.row(b["id"])["use_count"], self.row(b["id"])["last_used_at"]), (1, 0, None))

    def test_refused_requests_still_count_as_use_but_unauthenticated_ones_do_not(self):
        made = self.mint("alice", name="ci", role_cap="viewer")
        c = self.using(made)
        self.assertEqual(c.post("/api/projects", {"key": "NEW", "name": "x"}).status, 403)
        self.assertEqual(c.get("/api/projects/NOPE").status, 404)
        self.assertEqual(self.row(made["id"])["use_count"], 1)  # the first write; the second is pending
        self.at(days=31)
        self.assertEqual(c.get("/api/me").error_code, "token_expired")
        self.assertEqual(self.row(made["id"])["use_count"], 1)

    def test_a_failure_to_record_use_never_fails_the_request(self):
        made = self.mint("alice", name="ci")
        conn = db.connect(self.config.db_path)
        conn.execute("CREATE TRIGGER no_usage BEFORE UPDATE ON api_tokens BEGIN SELECT RAISE(ABORT, 'read-only today'); END")
        conn.close()
        with self.assertLogs("zylker.access", "WARNING") as logs:
            r = self.using(made).get("/api/me")
        self.assertEqual(r.status, 200)
        self.assertIn("could not record the use of API token %d" % made["id"], logs.output[0])

    def test_the_bookkeeping_stays_bounded(self):
        c = self.using(self.mint("bob", name="ci"))
        with mock.patch("zylker_pm.app.MAX_TRACKED_USERS", 5):
            for token_id in range(1000, 1010):  # ten other tokens already in memory
                self.app._token_flushed[token_id] = self.now
                self.app._token_pending[token_id] = 3
            c.get("/api/me")  # its first use: a write
            self.assertLessEqual(len(self.app._token_flushed), 5)
            c.get("/api/me")  # its second, a minute-less one: counted in memory
            self.assertLessEqual(len(self.app._token_pending), 5)

    def test_every_token_has_its_own_request_budget(self):
        config = dataclasses.replace(self.config, rate_limit_per_minute=50)  # a burst of 10
        app = App(config, clock=lambda: self.now, notifier=self.notifier)
        a, b = self.mint("alice", name="a"), self.mint("alice", name="b")
        ca, cb, primary = Client(app, a["token"]), Client(app, b["token"]), Client(app, self.tokens["alice"])
        answers = [ca.get("/api/me") for _ in range(12)]
        self.assertEqual([r.status for r in answers], [200] * 10 + [429, 429])
        self.assertEqual(answers[0].headers["X-RateLimit-Limit"], "50")
        self.assertEqual(answers[10].error_code, "rate_limited")
        self.assertEqual(cb.get("/api/me").status, 200)  # a busy integration does not starve its siblings
        self.assertEqual(primary.get("/api/me").status, 200)  # or its owner
        self.assertEqual(self.client("bob").get("/api/me").status, 200)

    def test_refused_tokens_count_towards_the_brute_force_block(self):
        config = dataclasses.replace(self.config, auth_failure_limit=3)
        app = App(config, clock=lambda: self.now, notifier=self.notifier)
        made = self.mint("alice", name="old", expires_in_days=1)
        self.at(days=2)
        c = Client(app, made["token"])
        self.assertEqual([c.get("/api/me").status for _ in range(3)], [401, 401, 401])
        r = c.get("/api/me")
        self.assertEqual((r.status, r.error_code), (429, "too_many_failures"))
        self.assertEqual(Client(app, self.tokens["alice"]).get("/api/me").status, 200)  # a good token from the same address still works


class AuditTests(TokenBase):
    def failures(self):
        return self.audit(action="auth.failed")

    def test_refusals_say_why_and_whose_token_it_was(self):
        expired = self.mint("alice", name="old", expires_in_days=1)
        revoked = self.mint("bob", name="gone")
        office = self.mint("carol", name="office", allowed_ips=["203.0.113.0/24"])
        self.client("bob").delete("/api/tokens/%d" % revoked["id"])
        self.at(days=2)
        self.using(expired).get("/api/me")
        self.using(revoked).get("/api/me")
        self.using(office).get("/api/me", remote_addr=ELSEWHERE)
        Client(self.app, "zpt_nonsense").get("/api/me")
        events = {e["details"]["reason"]: e for e in self.failures()}
        self.assertEqual(set(events), {"token_expired", "token_revoked", "token_ip_not_allowed", "invalid_token"})
        for reason, who, made in (("token_expired", "alice", expired), ("token_revoked", "bob", revoked), ("token_ip_not_allowed", "carol", office)):
            event = events[reason]
            self.assertEqual((event["actor"]["username"], event["details"]["user"], event["details"]["token"], event["severity"]), (who, who, made["hint"], "warning"), reason)
        self.assertEqual(events["token_ip_not_allowed"]["ip"], ELSEWHERE)
        self.assertIsNone(events["invalid_token"]["actor"])
        self.assertNotIn("token", events["invalid_token"]["details"])
        self.assertNotIn(expired["token"], json.dumps(self.audit()))

    def test_repeats_from_one_address_are_one_counted_row_per_token(self):
        a = self.mint("alice", name="a", expires_in_days=1)
        b = self.mint("alice", name="b", expires_in_days=1)
        self.at(days=2)
        for _ in range(4):
            self.using(a).get("/api/me")
        self.using(b).get("/api/me")
        rows = sorted((e["details"]["token"], e["details"]["count"]) for e in self.failures())
        self.assertEqual(rows, sorted([(a["hint"], 4), (b["hint"], 1)]))

    def test_a_write_refused_by_a_read_only_token_is_a_denial(self):
        made = self.mint("alice", name="reader", read_only=True)
        self.using(made).post("/api/projects", {"key": "NEW", "name": "x"})
        denial = self.audit(action="auth.denied")[0]
        self.assertEqual((denial["actor"]["username"], denial["details"]["code"], denial["details"]["route"]), ("alice", "token_read_only", "/api/projects"))

    def test_a_session_started_by_a_token_names_the_token(self):
        made = self.mint("alice", name="nightly")
        self.client("alice").get("/api/me")
        self.using(made).get("/api/me")
        self.using(made).get("/api/me")  # the same session
        sessions = self.audit(action="auth.session_started")
        mine = [s for s in sessions if s["actor"]["username"] == "alice"]
        self.assertEqual(sorted(s["details"].get("token", "") for s in mine), ["", "nightly"])  # two credentials, two sessions

    def test_revocation_is_logged_with_who_did_it(self):
        made = self.mint("bob", name="ci")
        self.admin.delete("/api/tokens/%d" % made["id"])
        event = self.audit(action="token.revoked")[0]
        self.assertEqual((event["actor"]["username"], event["details"], event["category"]), ("admin", {"id": made["id"], "name": "ci", "user": "bob", "hint": made["hint"]}, "token"))

    def test_the_category_is_listed_and_filterable(self):
        self.assertEqual(self.ok(self.client("vera").get("/api/meta"))["audit_categories"]["token"], "API tokens")
        made = self.mint("alice", name="ci")
        self.client("alice").delete("/api/tokens/%d" % made["id"])
        self.assertEqual(sorted(e["action"] for e in self.audit(category="token")), ["token.created", "token.revoked"])

    def test_the_events_can_be_sent_to_a_webhook(self):
        self.assertIn("token", webhooks.subscribable_categories())
        hook = self.ok(self.admin.post("/api/webhooks", {"name": "tokens", "url": "https://hooks.example.com/in", "events": ["token.*"]}), 201)
        made = self.mint("alice", name="ci")
        queued = self.ok(self.admin.get("/api/webhooks/%d/deliveries" % hook["id"]))["items"]
        self.assertEqual([d["event"] for d in queued], ["token.created"])
        detail = self.ok(self.admin.get("/api/webhooks/%d/deliveries/%d" % (hook["id"], queued[0]["id"])))
        self.assertEqual(detail["payload"]["details"]["name"], "ci")
        self.assertNotIn(made["token"], json.dumps(detail))


class Fuzz(TokenBase):
    def test_no_authorization_header_ever_causes_a_server_error(self):
        made = self.mint("alice", name="ci")
        headers = [
            "Bearer zpt_", "Bearer zpt_" + "A" * 20000, "Bearer zpt_\x00\x01", "Bearer zpt_\u00e9\u4e2d", "Bearer  ", "Bearer", "zpt_" + "x" * 43,
            "Basic " + made["token"], "Bearer " + made["token"] + "\x00", "Bearer " + made["token"][:-1], "Bearer " + made["token"][::-1],
            "Bearer zpt_' OR 1=1 --", "Bearer zpt_%s" % ("%" * 50), "Bearer zpt_\\", "Bearer " + made["token"].upper(), "Bearer ZPT_abc",
        ]
        for value in headers:
            r = Client(self.app).get("/api/me", headers={"Authorization": value})
            self.assertEqual(r.status, 401, value[:60])

    def test_hostile_bodies_never_cause_a_server_error(self):
        bodies = [
            {"name": "x", "allowed_ips": ["::ffff:999.1.1.1"]}, {"name": "x", "allowed_ips": ["1" * 5000]}, {"name": "x", "allowed_ips": [{"a": 1}]},
            {"name": "x", "role_cap": ["admin"]}, {"name": "x", "read_only": "yes"}, {"name": "x", "user": {"a": 1}}, {"name": "x", "user": "a" * 500},
            {"name": "x", "expires_in_days": 10**30}, {"name": "x" * 10_000}, {"name": {"$ne": 1}}, {"name": "x", "read_only": None}, {"name": "x", "user": None},
        ]
        for body in bodies:
            r = self.client("admin").post("/api/tokens", body)
            self.assertIn(r.status, (201, 422), (body, r.text[:200]))
        self.assertLess(self.client("admin").get("/api/tokens", query={"user": "%" * 400}).status, 500)
        self.assertLess(self.client("admin").get("/api/tokens", query={"status": "a," * 100}).status, 500)


class UpgradeTests(TokenBase):
    def v6_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        for version in (1, 2, 3, 4, 5):
            db._migrate(conn, version)
        conn.close()

    def names(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_an_older_database_upgrades_and_every_existing_token_keeps_working(self):
        old = os.path.join(self.tmp, "v6.db")
        self.v6_database(old)
        conn = db.connect(old)
        conn.execute("INSERT INTO users (username, display_name, role, token_hash, created_at) VALUES ('u', 'U', 'admin', ?, 'x')", (auth.hash_token("zpm_legacy"),))
        conn.close()
        self.assertNotIn("api_tokens", self.names(old)[0])
        init_db(old)
        fresh = os.path.join(self.tmp, "fresh7.db")
        init_db(fresh)
        self.assertEqual(self.names(old), self.names(fresh))
        self.assertEqual(self.names(old)[1], db.SCHEMA_VERSION)
        conn = db.connect(old)
        self.assertEqual(auth.authenticate(conn, "Bearer zpm_legacy")["username"], "u")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0], 0)
        conn.close()

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v6b.db")
        self.v6_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE api_tokens (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        names, version = self.names(path)
        self.assertEqual(version, 6)
        self.assertNotIn("idx_api_tokens_user", names)

    def test_the_schema_rejects_what_the_service_would_never_write(self):
        conn = db.connect(self.config.db_path)
        user = conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        insert = "INSERT INTO api_tokens (user_id, name, token_hash, hint, role_cap, read_only, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)"
        conn.execute(insert, (user, "ok", "h1", "zpt_...abcd", "member", 0, "x", "y"))
        for args in ((user, "dup", "h1", "zpt_...abcd", "member", 0, "x", "y"), (user, "cap", "h2", "zpt_...abcd", "root", 0, "x", "y"), (user, "ro", "h3", "zpt_...abcd", "member", 2, "x", "y"), (9999, "orphan", "h4", "zpt_...abcd", "member", 0, "x", "y")):
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(insert, args)
        conn.close()


class CommandTests(TokenBase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZYLKER_")}
        clocked = lambda cfg: App(cfg, clock=lambda: self.now, notifier=self.notifier)  # noqa: E731 - the fixed test clock
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(cli, "App", clocked), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", self.config.db_path, *args])
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def token_in(text):
        return next(line.strip() for line in text.splitlines() if line.strip().startswith("zpt_"))

    def test_create_token_prints_a_working_token_once(self):
        code, out, err = self.run_cli("create-token", "bob", "--name", "ci-nightly")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Created API token 'ci-nightly' for 'bob' (role member, expires 2026", out)
        token = self.token_in(out)
        me = self.ok(Client(self.app, token).get("/api/me"))
        self.assertEqual((me["username"], me["role"]), ("bob", "member"))
        self.assertEqual(token in self.client("bob").get("/api/tokens").text, False)
        event = self.audit(action="token.created")[0]
        self.assertIsNone(event["actor"])  # done from the command line, by nobody in particular
        self.assertEqual(event["details"]["user"], "bob")

    def test_the_options(self):
        code, out, _ = self.run_cli("create-token", "alice", "--name", "reports", "--days", "7", "--role-cap", "viewer", "--read-only", "--ip", "203.0.113.0/24", "--ip", "2001:db8::1")
        self.assertEqual(code, 0)
        self.assertIn("role viewer, read-only", out)
        made = self.listing("alice")[0]
        self.assertEqual((made["role_cap"], made["read_only"], made["allowed_ips"], made["expires_at"]), ("viewer", True, ["203.0.113.0/24", "2001:db8::1/128"], "2026-10-23T09:00:00Z"))
        c = Client(self.app, self.token_in(out))
        self.assertEqual(c.get("/api/projects").status, 200)
        self.assertEqual(c.post("/api/projects", {"key": "NEW", "name": "x"}).error_code, "token_read_only")
        self.assertEqual(c.get("/api/me", remote_addr=ELSEWHERE).error_code, "token_ip_not_allowed")

    def test_bad_input_is_reported_not_raised(self):
        code, out, err = self.run_cli("create-token", "ghost", "--name", "x")
        self.assertEqual((code, out, err), (1, "", "No such user: ghost\n"))
        code, _, err = self.run_cli("create-token", "bob", "--name", "x", "--days", "0")
        self.assertEqual(code, 1)
        self.assertIn("expires_in_days: must be >= 1", err)
        code, _, err = self.run_cli("create-token", "bob", "--name", "x", "--ip", "nope")
        self.assertEqual(code, 1)
        self.assertIn("allowed_ips", err)
        code, _, err = self.run_cli("create-token", "vera", "--name", "x", "--role-cap", "admin")
        self.assertEqual(code, 1)
        self.assertIn("cannot be higher", err)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli("create-token", "bob", "--name", "x", "--role-cap", "root")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_cli("create-token", "bob")  # --name is required
        self.assertEqual(self.listing("bob"), [])

    def test_a_deactivated_user_gets_nothing(self):
        bob_id = self.ok(self.client("bob").get("/api/me"))["id"]
        self.ok(self.admin.patch("/api/users/%d" % bob_id, {"active": False}))
        code, _, err = self.run_cli("create-token", "bob", "--name", "x")
        self.assertEqual(code, 1)
        self.assertIn("deactivated", err)

    def test_list_tokens(self):
        self.run_cli("create-token", "alice", "--name", "alpha", "--read-only")
        self.run_cli("create-token", "bob", "--name", "beta")
        self.run_cli("create-token", "bob", "--name", "gamma")
        bob = self.listing("bob")
        self.client("bob").delete("/api/tokens/%d" % next(t["id"] for t in bob if t["name"] == "gamma"))
        code, out, _ = self.run_cli("list-tokens")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[2] for line in out.splitlines()], ["gamma", "beta", "alpha"])
        self.assertIn("manager/ro", out)
        self.assertIn("revoked", out)
        code, out, _ = self.run_cli("list-tokens", "bob", "--status", "active")
        self.assertEqual([line.split()[2] for line in out.splitlines()], ["beta"])
        self.assertIn("expires 2026-11-15", out)
        self.assertEqual(self.run_cli("list-tokens", "vera")[1], "No tokens.\n")
        code, _, err = self.run_cli("list-tokens", "--status", "nope")
        self.assertEqual(code, 1)
        self.assertIn("status", err)
        code, _, err = self.run_cli("list-tokens", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("no such user", err)

    def test_revoke_tokens(self):
        self.run_cli("create-token", "bob", "--name", "one")
        code, out, _ = self.run_cli("create-token", "bob", "--name", "two")
        token = self.token_in(out)
        self.run_cli("create-token", "carol", "--name", "other")
        code, out, err = self.run_cli("revoke-tokens", "bob")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Revoked 2 API token(s) of 'bob'", out)
        self.assertEqual(Client(self.app, token).get("/api/me").error_code, "token_revoked")
        self.assertEqual(self.client("bob").get("/api/me").status, 200)  # the primary token is a different thing
        self.assertEqual(len(self.listing("carol")), 1)
        self.assertEqual(self.run_cli("revoke-tokens", "bob")[1].split()[1], "0")
        code, _, err = self.run_cli("revoke-tokens", "ghost")
        self.assertEqual((code, err), (1, "No such user: ghost\n"))


class HelperTests(TokenBase):
    def test_effective_role_is_the_lower_of_the_two(self):
        self.assertEqual(auth.effective_role("admin", "member"), "member")
        self.assertEqual(auth.effective_role("viewer", "admin"), "viewer")
        self.assertEqual(auth.effective_role("manager", "manager"), "manager")

    def test_token_of_and_rate_key(self):
        self.assertIsNone(auth.token_of(None))
        self.assertIsNone(auth.token_of({"id": 1}))
        self.assertEqual(auth.rate_key({"id": 7}), 7)
        self.assertEqual(auth.rate_key({"id": 7, "token": {"id": 3}}), ("token", 3))

    def test_expiry_cannot_be_skipped_by_leaving_out_the_clock(self):
        made = self.mint("alice", name="ci", expires_in_days=1)
        conn = db.connect(self.config.db_path)
        conn.execute("UPDATE api_tokens SET expires_at = '2020-01-01T00:00:00Z'")
        with self.assertRaises(auth.Unauthorized) as cm:
            auth.authenticate(conn, "Bearer " + made["token"])  # no `now` given: the real time is used
        self.assertEqual(cm.exception.code, "token_expired")
        conn.close()

    def test_the_status_helper(self):
        row = {"revoked_at": None, "expires_at": "2026-10-16T10:00:00Z"}
        self.assertEqual(tokens._status(row, "2026-10-16T09:59:59Z"), "active")
        self.assertEqual(tokens._status(row, "2026-10-16T10:00:00Z"), "expired")
        self.assertEqual(tokens._status({**row, "revoked_at": "2026-10-16T08:00:00Z"}, "2026-10-16T08:00:01Z"), "revoked")
