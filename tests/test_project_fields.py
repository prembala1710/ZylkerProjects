"""Custom project fields: definitions, value validation (text / number / URL), permissions, audit and migration."""

import json
import os
import sqlite3

from helpers import ApiTestCase

from zylker_pm import db
from zylker_pm.config import Config
from zylker_pm.db import init_db


class FieldsBase(ApiTestCase):
    def field(self, key="client", type="text", user="admin", **extra):
        body = {"key": key, "label": extra.pop("label", key.replace("_", " ").title()), "type": type, **extra}
        return self.client(user).post("/api/project-fields", body)

    def make_field(self, *args, **kw):
        return self.ok(self.field(*args, **kw), 201)

    def project(self, key="WEB", custom=None, **extra):
        body = {"key": key, "name": "Project " + key, **extra}
        if custom is not None:
            body["custom_fields"] = custom
        return self.client("alice").post("/api/projects", body)

    def values(self, key="WEB"):
        return self.ok(self.client("bob").get("/api/projects/" + key))["custom_fields"]


class DefinitionTests(FieldsBase):
    def test_create_list_and_defaults_per_type(self):
        t = self.make_field("client", "text")
        n = self.make_field("budget", "number")
        u = self.make_field("wiki", "url", label="Wiki page")
        self.assertEqual((t["max_length"], t["decimals"], t["min_value"], t["required"]), (255, None, None, False))
        self.assertEqual((n["decimals"], n["max_length"], n["min_value"], n["max_value"]), (2, None, None, None))
        self.assertEqual((u["max_length"], u["decimals"]), (None, None))
        items = self.ok(self.client("vera").get("/api/project-fields"))["items"]  # viewers can read
        self.assertEqual([i["key"] for i in items], ["client", "budget", "wiki"])
        meta = self.ok(self.client("vera").get("/api/meta"))
        self.assertEqual(meta["project_field_types"], ["text", "number", "url"])
        self.assertEqual(meta["project_field_limits"]["max_fields"], 20)

    def test_only_admins_manage_definitions(self):
        for user in ("alice", "bob", "vera"):
            r = self.field(user=user)
            self.assertEqual((r.status, r.error_code), (403, "insufficient_role"), user)
        self.make_field("client")
        for user in ("alice", "bob"):
            self.assertEqual(self.client(user).patch("/api/project-fields/client", {"label": "x"}).status, 403)
            self.assertEqual(self.client(user).delete("/api/project-fields/client", query={"confirm": "client"}).status, 403)
        self.assertEqual(self.client().get("/api/project-fields").status, 401)

    def test_validation_reports_every_problem(self):
        r = self.admin.post("/api/project-fields", {"key": "ok_key", "label": "", "type": "date", "bogus": 1})
        d = r.json["error"]["details"]
        self.assertEqual(r.status, 422)
        self.assertEqual(sorted(d), ["bogus", "label", "type"])  # all reported in one response
        self.assertEqual(d["bogus"], "unknown field")
        r = self.field("Bad Key")
        self.assertIn("key", r.json["error"]["details"])
        self.assertEqual(self.admin.post("/api/project-fields", {}).status, 422)  # key, label and type are required
        for bad in ("1abc", "a", "has-dash", "UPPER", "x" * 41):
            self.assertEqual(self.field(bad).status, 422, bad)

    def test_type_specific_settings_are_enforced(self):
        cases = [
            ("text", {"min_value": 1}, "min_value"),
            ("text", {"decimals": 1}, "decimals"),
            ("number", {"max_length": 10}, "max_length"),
            ("url", {"max_length": 10}, "max_length"),
            ("text", {"max_length": 0}, "max_length"),
            ("text", {"max_length": 2001}, "max_length"),
            ("number", {"decimals": 4}, "decimals"),
            ("number", {"decimals": -1}, "decimals"),
            ("number", {"min_value": "1"}, "min_value"),
            ("number", {"min_value": 2e12}, "min_value"),
            ("number", {"min_value": 10, "max_value": 5}, "max_value"),
            ("text", {"required": "yes"}, "required"),
            ("text", {"description": "x" * 201}, "description"),
        ]
        for i, (kind, extra, bad_field) in enumerate(cases):
            r = self.field("f%d" % i, kind, **extra)
            self.assertEqual(r.status, 422, extra)
            self.assertIn(bad_field, r.json["error"]["details"], extra)

    def test_duplicate_key_and_label_ignoring_case(self):
        self.make_field("client", label="Client")
        r = self.field("client", label="Other")
        self.assertEqual((r.status, r.error_code), (409, "field_key_taken"))
        r = self.field("client2", label="CLIENT")
        self.assertEqual((r.status, r.error_code), (409, "field_label_taken"))

    def test_field_limit(self):
        for i in range(20):
            self.make_field("field_%02d" % i)
        r = self.field("one_too_many")
        self.assertEqual((r.status, r.error_code), (409, "field_limit_reached"))

    def test_update_changes_settings_but_never_key_or_type(self):
        self.make_field("client")
        f = self.ok(self.admin.patch("/api/project-fields/client", {"label": "Customer", "required": True, "max_length": 40, "description": "Who"}))
        self.assertEqual((f["label"], f["required"], f["max_length"], f["description"]), ("Customer", True, 40, "Who"))
        for body in ({"key": "other"}, {"type": "number"}):
            r = self.admin.patch("/api/project-fields/client", body)
            self.assertEqual(r.status, 422)
            self.assertIn("cannot be changed", list(r.json["error"]["details"].values())[0])
        r = self.admin.patch("/api/project-fields/client", {"decimals": 1})  # does not apply to text
        self.assertIn("decimals", r.json["error"]["details"])
        self.assertEqual(self.admin.patch("/api/project-fields/nope", {"label": "x"}).status, 404)
        self.make_field("other", label="Other")
        r = self.admin.patch("/api/project-fields/other", {"label": "customer"})
        self.assertEqual((r.status, r.error_code), (409, "field_label_taken"))
        self.ok(self.admin.patch("/api/project-fields/other", {"label": "OTHER"}))  # re-casing your own label is fine

    def test_tightening_a_limit_below_existing_values_is_refused(self):
        self.make_field("client", max_length=50)
        self.make_field("budget", "number")
        self.ok(self.project("WEB", {"client": "A fairly long client name", "budget": 1234.56}), 201)
        r = self.admin.patch("/api/project-fields/client", {"max_length": 10})
        self.assertEqual((r.status, r.error_code), (409, "existing_values_invalid"))
        self.assertEqual(r.json["error"]["details"]["projects"], ["WEB"])
        for body in ({"max_value": 1000}, {"min_value": 2000}, {"decimals": 1}, {"decimals": 0}):
            r = self.admin.patch("/api/project-fields/budget", body)
            self.assertEqual((r.status, r.error_code), (409, "existing_values_invalid"), body)
        self.ok(self.admin.patch("/api/project-fields/client", {"max_length": 25}))  # still fits
        self.ok(self.admin.patch("/api/project-fields/budget", {"decimals": 3, "max_value": 5000}))
        self.assertEqual(self.values()["budget"], 1234.56)

    def test_delete_needs_confirmation_and_removes_values_everywhere(self):
        self.make_field("client")
        self.make_field("wiki", "url")
        self.ok(self.project("WEB", {"client": "Acme", "wiki": "https://example.com"}), 201)
        r = self.admin.delete("/api/project-fields/client")
        self.assertEqual((r.status, r.error_code), (409, "confirmation_required"))
        self.assertEqual(r.json["error"]["details"]["projects_with_a_value"], 1)
        self.assertEqual(self.admin.delete("/api/project-fields/client", query={"confirm": "wrong"}).status, 409)
        self.assertEqual(self.admin.delete("/api/project-fields/client", query={"confirm": "client"}).status, 204)
        self.assertEqual(self.values(), {"wiki": "https://example.com"})
        conn = sqlite3.connect(self.config.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM project_field_values").fetchone()[0], 1)
        conn.close()
        self.assertEqual(self.admin.delete("/api/project-fields/client", query={"confirm": "client"}).status, 404)
        # the key can be reused afterwards, and the old value does not come back
        self.make_field("client")
        self.assertEqual(self.values()["client"], None)

    def test_definition_changes_are_in_the_admin_audit_trail(self):
        self.make_field("client")
        self.ok(self.admin.patch("/api/project-fields/client", {"required": True}))
        self.admin.delete("/api/project-fields/client", query={"confirm": "client"})
        items = self.ok(self.admin.get("/api/activity"))["items"]
        actions = [a["action"] for a in items if a["action"].startswith("project_field.")]
        self.assertEqual(actions, ["project_field.deleted", "project_field.updated", "project_field.created"])
        updated = next(a for a in items if a["action"] == "project_field.updated")
        self.assertEqual(updated["details"]["changes"], {"required": [False, True]})


class TextTests(FieldsBase):
    def setUp(self):
        super().setUp()
        self.make_field("client", max_length=20)
        self.make_project("WEB")

    def set(self, value, key="client"):
        return self.client("alice").patch("/api/projects/WEB", {"custom_fields": {key: value}})

    def test_stores_trims_and_clears(self):
        self.assertEqual(self.ok(self.set("  Acme Corp  "))["custom_fields"], {"client": "Acme Corp"})
        self.assertEqual(self.ok(self.set(None))["custom_fields"], {"client": None})
        self.ok(self.set("Again"))
        self.assertEqual(self.ok(self.set("   "))["custom_fields"], {"client": None})  # blank means clear

    def test_rejects_wrong_types_control_characters_and_overlong_values(self):
        for bad in (5, True, ["a"], {"a": 1}, "two\nlines", "tab\there", "nul\x00", "x" * 21, "line\u2028sep"):
            r = self.set(bad)
            self.assertEqual(r.status, 422, repr(bad))
            self.assertIn("custom_fields.client", r.json["error"]["details"])
        self.assertEqual(self.set("x" * 20).status, 200)
        self.assertEqual(self.set("é" * 20).status, 200)  # characters, not bytes

    def test_markup_is_stored_verbatim_for_the_ui_to_escape(self):
        body = "<img src=x onerror=alert(1)>"
        self.make_field("note", max_length=100)
        self.assertEqual(self.ok(self.set(body, "note"))["custom_fields"]["note"], body)


class NumberTests(FieldsBase):
    def setUp(self):
        super().setUp()
        self.make_field("budget", "number", decimals=2, min_value=0, max_value=1000000)
        self.make_field("headcount", "number", decimals=0, label="Head count")
        self.make_project("WEB")

    def set(self, value, key="budget"):
        return self.client("alice").patch("/api/projects/WEB", {"custom_fields": {key: value}})

    def test_accepts_integers_and_decimals_within_limits(self):
        self.assertEqual(self.ok(self.set(1500))["custom_fields"]["budget"], 1500)
        self.assertEqual(self.ok(self.set(1234.56))["custom_fields"]["budget"], 1234.56)
        self.assertEqual(self.ok(self.set(0.1))["custom_fields"]["budget"], 0.1)
        self.assertEqual(self.ok(self.set(0))["custom_fields"]["budget"], 0)  # zero is a value, not "empty"
        self.assertEqual(self.ok(self.set(1000000))["custom_fields"]["budget"], 1000000)  # boundary is inclusive
        self.assertEqual(self.ok(self.set(None))["custom_fields"]["budget"], None)

    def test_whole_number_fields_return_integers_and_accept_x_point_zero(self):
        self.assertEqual(self.ok(self.set(5.0, "headcount"))["custom_fields"]["headcount"], 5)
        self.assertIsInstance(self.values()["headcount"], int)
        self.assertEqual(self.set(5.5, "headcount").status, 422)
        self.assertEqual(self.ok(self.set(-3, "headcount"))["custom_fields"]["headcount"], -3)  # no minimum configured

    def test_rejects_non_numbers_and_out_of_range_values(self):
        for bad in ("5", "", True, False, [1], {"n": 1}):
            self.assertEqual(self.set(bad).status, 422, repr(bad))
        for bad in (-0.01, 1000000.01, 1e13, 10**400):
            self.assertEqual(self.set(bad).status, 422, repr(bad))
        self.assertEqual(self.set(-1, "headcount").status, 200)

    def test_decimal_places_are_refused_not_rounded(self):
        r = self.set(1.005)
        self.assertEqual(r.status, 422)
        self.assertIn("at most 2 decimal places", r.json["error"]["details"]["custom_fields.budget"])
        self.assertEqual(self.set(1e-05).status, 422)
        self.assertEqual(self.set(10.10).status, 200)
        self.assertEqual(self.values()["budget"], 10.1)

    def test_not_a_number_and_infinity_literals_are_rejected_at_the_wire(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            raw = ('{"custom_fields": {"budget": %s}}' % literal).encode()
            r = self.client("alice").request("PATCH", "/api/projects/WEB", raw=raw, content_type="application/json")
            self.assertEqual(r.status, 422, literal)
        self.assertEqual(self.values()["budget"], None)

    def test_negative_zero_is_normalised(self):
        self.make_field("delta", "number", min_value=-5)
        self.ok(self.set(-0.0, "delta"))
        self.assertEqual(json.dumps(self.values()["delta"]), "0.0")


class UrlTests(FieldsBase):
    GOOD = [
        "https://example.com",
        "http://example.com/a/b?x=1&y=2#frag",
        "HTTPS://Example.com/Path",
        "https://sub.domain.example.co.uk:8443/x",
        "http://localhost:3000",
        "http://127.0.0.1/x",
        "http://[::1]:8080/",
        "https://example.com/caf%C3%A9",
        "https://example.com/it's",
    ]
    BAD = [
        "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:x", "file:///etc/passwd",
        "ftp://example.com/x", "//evil.example/x", "example.com", "www.example.com", "https://", "http:///path", "https:example.com",
        "https://exa mple.com", "https://example.com/a b", "https://example.com\\@evil.example", "https://user@example.com",
        "https://user:pw@example.com", "https://example.com:99999", "https://example.com:0", "https://example.com:abc",
        "https://.example.com", "https://exa..mple.com", "https://example.com/<script>", 'https://example.com/"onmouseover="x',
        "https://example.com/`x`", "https://exämple.com", "https://example.com/\nX-Injected: 1", "ja\tvascript:alert(1)",
        "https://example.com/" + "a" * 2000, 12345, True,
    ]  # fmt: skip

    def setUp(self):
        super().setUp()
        self.make_field("wiki", "url")
        self.make_project("WEB")

    def set(self, value):
        return self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"wiki": value}})

    def test_valid_web_addresses_are_stored_exactly_as_typed(self):
        for url in self.GOOD:
            self.assertEqual(self.ok(self.set(url))["custom_fields"]["wiki"], url, url)
        self.assertEqual(self.ok(self.set("  https://example.com  "))["custom_fields"]["wiki"], "https://example.com")

    def test_dangerous_or_malformed_addresses_are_refused(self):
        for url in self.BAD:
            r = self.set(url)
            self.assertEqual(r.status, 422, repr(url)[:80])
            self.assertIn("custom_fields.wiki", r.json["error"]["details"])
        self.assertEqual(self.values()["wiki"], None)  # nothing leaked through

    def test_clearing(self):
        self.ok(self.set("https://example.com"))
        self.assertEqual(self.ok(self.set(""))["custom_fields"]["wiki"], None)


class ProjectIntegrationTests(FieldsBase):
    def setUp(self):
        super().setUp()
        self.make_field("client", max_length=30)
        self.make_field("budget", "number", decimals=0, min_value=0)
        self.make_field("wiki", "url")

    def test_projects_without_values_still_list_every_field_as_null(self):
        self.ok(self.project("WEB"), 201)
        self.assertEqual(self.values(), {"client": None, "budget": None, "wiki": None})
        listed = self.ok(self.client("bob").get("/api/projects"))["items"][0]
        self.assertEqual(listed["custom_fields"], {"client": None, "budget": None, "wiki": None})

    def test_create_with_values_and_they_appear_in_detail_list_and_portfolio(self):
        p = self.ok(self.project("WEB", {"client": "Acme", "budget": 5000, "wiki": "https://wiki.example.com"}), 201)
        want = {"client": "Acme", "budget": 5000, "wiki": "https://wiki.example.com"}
        self.assertEqual(p["custom_fields"], want)
        self.assertEqual(self.ok(self.client("vera").get("/api/projects"))["items"][0]["custom_fields"], want)
        portfolio = self.ok(self.client("vera").get("/api/reports/portfolio"))["projects"][0]
        self.assertEqual(portfolio["custom_fields"], want)

    def test_patch_merges_so_untouched_fields_keep_their_values(self):
        self.ok(self.project("WEB", {"client": "Acme", "budget": 5000}), 201)
        p = self.ok(self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"budget": 6000}}))
        self.assertEqual((p["custom_fields"]["client"], p["custom_fields"]["budget"]), ("Acme", 6000))
        p = self.ok(self.client("alice").patch("/api/projects/WEB", {"name": "Renamed"}))  # no custom_fields key at all
        self.assertEqual((p["name"], p["custom_fields"]["client"], p["custom_fields"]["budget"]), ("Renamed", "Acme", 6000))
        p = self.ok(self.client("alice").patch("/api/projects/WEB", {"custom_fields": {}}))
        self.assertEqual(p["custom_fields"]["client"], "Acme")
        p = self.ok(self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"client": None}}))
        self.assertEqual((p["custom_fields"]["client"], p["custom_fields"]["budget"]), (None, 6000))

    def test_one_422_lists_project_and_field_problems_together(self):
        r = self.project("1bad", {"client": "x" * 31, "budget": -1, "wiki": "javascript:alert(1)", "ghost": "boo"}, name="")
        self.assertEqual(r.status, 422)
        d = r.json["error"]["details"]
        for k in ("name", "custom_fields.client", "custom_fields.budget", "custom_fields.wiki", "custom_fields.ghost"):
            self.assertIn(k, d)
        self.assertEqual(d["custom_fields.ghost"], "unknown custom field")
        for bad in ("text", ["a"], 5):
            r = self.project("OK", bad)
            self.assertEqual((r.status, r.json["error"]["details"]), (422, {"custom_fields": "must be an object mapping field keys to values"}))
        r = self.project("OK2", None, custom_fields=None)  # explicit null is the same as absent
        self.assertEqual(r.status, 201)

    def test_a_rejected_request_changes_nothing(self):
        self.ok(self.project("WEB", {"client": "Acme"}), 201)
        r = self.client("alice").patch("/api/projects/WEB", {"name": "New name", "custom_fields": {"client": "Beta", "budget": -5}})
        self.assertEqual(r.status, 422)
        p = self.ok(self.client("bob").get("/api/projects/WEB"))
        self.assertEqual((p["name"], p["custom_fields"]["client"]), ("Project WEB", "Acme"))

    def test_only_managers_and_admins_can_write_values(self):
        self.ok(self.project("WEB", members=["bob"]), 201)
        for user in ("bob", "vera"):
            r = self.client(user).patch("/api/projects/WEB", {"custom_fields": {"client": "Hax"}})
            self.assertEqual(r.status, 403, user)
        self.assertEqual(self.values()["client"], None)
        self.assertEqual(self.client("admin").patch("/api/projects/WEB", {"custom_fields": {"client": "ok"}}).status, 200)

    def test_values_removed_with_their_project(self):
        self.ok(self.project("WEB", {"client": "Acme"}), 201)
        self.assertEqual(self.admin.delete("/api/projects/WEB", query={"confirm": "WEB"}).status, 204)
        conn = sqlite3.connect(self.config.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM project_field_values").fetchone()[0], 0)
        conn.close()

    def test_search_matches_text_and_url_values_but_not_numbers(self):
        self.ok(self.project("AAA", {"client": "Globex Corporation", "budget": 777, "wiki": "https://wiki.example.com/zebra"}), 201)
        self.ok(self.project("BBB", {"client": "Initech"}), 201)
        keys = lambda q: [p["key"] for p in self.ok(self.client("bob").get("/api/projects", query={"q": q}))["items"]]  # noqa: E731
        self.assertEqual(keys("globex"), ["AAA"])
        self.assertEqual(keys("zebra"), ["AAA"])
        self.assertEqual(keys("initech"), ["BBB"])
        self.assertEqual(keys("777"), [])
        self.assertEqual(keys("%"), [])  # wildcard characters stay literal

    def test_custom_field_only_edits_do_not_notify_cliq(self):
        self.ok(self.project("WEB"), 201)
        self.ok(self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"client": "Acme"}}))
        self.assertEqual(self.notifier.messages, [])

    def test_unchanged_values_write_no_audit_entry_and_do_not_touch_updated_at(self):
        p = self.ok(self.project("WEB", {"client": "Acme"}), 201)
        self.advance_days(1)
        again = self.ok(self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"client": "Acme"}}))
        self.assertEqual(again["updated_at"], p["updated_at"])
        acts = self.ok(self.client("bob").get("/api/projects/WEB/activity"))["items"]
        self.assertEqual([a["action"] for a in acts], ["project.created"])

    def test_changes_are_audited_with_old_and_new_values(self):
        self.ok(self.project("WEB", {"client": "Acme", "budget": 100}), 201)
        self.advance_days(1)
        p = self.ok(self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"client": "Beta", "budget": None}}))
        self.assertNotEqual(p["updated_at"], p["created_at"])  # a custom-field-only edit still bumps updated_at
        acts = self.ok(self.client("bob").get("/api/projects/WEB/activity"))["items"]
        self.assertEqual(acts[0]["action"], "project.updated")
        self.assertEqual(acts[0]["details"]["changes"], {"custom_fields.client": ["Acme", "Beta"], "custom_fields.budget": [100, None]})
        self.assertEqual(acts[1]["details"]["custom_fields"], {"client": "Acme", "budget": 100})  # recorded on creation

    def test_long_values_are_truncated_in_the_audit_trail_only(self):
        self.make_field("blurb", max_length=2000)
        long = "x" * 1500
        self.ok(self.project("WEB", {"blurb": long}), 201)
        self.assertEqual(self.values()["blurb"], long)
        acts = self.ok(self.client("bob").get("/api/projects/WEB/activity"))["items"]
        self.assertLess(len(acts[0]["details"]["custom_fields"]["blurb"]), 300)

    def test_hostile_keys_are_just_unknown_fields(self):
        self.ok(self.project("WEB"), 201)
        evil = "x'; DROP TABLE project_fields; --"
        r = self.client("alice").patch("/api/projects/WEB", {"custom_fields": {evil: "v", "k" * 5000: "v"}})
        self.assertEqual(r.status, 422)
        self.assertTrue(all(len(k) < 100 for k in r.json["error"]["details"]))  # never echo unbounded input
        self.assertEqual(len(self.ok(self.client("bob").get("/api/project-fields"))["items"]), 3)


class RequiredFieldTests(FieldsBase):
    def setUp(self):
        super().setUp()
        self.make_field("client", required=True)

    def test_required_on_create(self):
        r = self.project("WEB")
        self.assertEqual(r.status, 422)
        self.assertEqual(r.json["error"]["details"]["custom_fields.client"], "is required")
        r = self.project("WEB", {"client": "   "})  # blank is not a value
        self.assertEqual(r.status, 422)
        self.ok(self.project("WEB", {"client": "Acme"}), 201)

    def test_required_cannot_be_cleared_but_can_be_changed(self):
        self.ok(self.project("WEB", {"client": "Acme"}), 201)
        for value in (None, "", "  "):
            r = self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"client": value}})
            self.assertEqual(r.status, 422, repr(value))
            self.assertEqual(r.json["error"]["details"]["custom_fields.client"], "is required and cannot be cleared")
        self.ok(self.client("alice").patch("/api/projects/WEB", {"custom_fields": {"client": "Beta"}}))

    def test_projects_that_predate_a_required_field_stay_editable(self):
        self.admin.patch("/api/project-fields/client", {"required": False})
        self.ok(self.project("OLD"), 201)
        self.admin.patch("/api/project-fields/client", {"required": True})
        p = self.ok(self.client("alice").patch("/api/projects/OLD", {"name": "Still editable", "status": "on_hold"}))
        self.assertEqual((p["name"], p["custom_fields"]["client"]), ("Still editable", None))

    def test_a_field_added_later_does_not_break_existing_projects(self):
        self.admin.patch("/api/project-fields/client", {"required": False})
        self.ok(self.project("OLD"), 201)
        self.make_field("owner_team", required=True, label="Owner team")
        self.assertEqual(self.ok(self.client("alice").patch("/api/projects/OLD", {"name": "fine"}))["name"], "fine")
        self.assertEqual(self.project("NEW").status, 422)


class SeedTests(FieldsBase):
    def test_demo_data_includes_valid_custom_fields(self):
        from helpers import Client

        from zylker_pm.seed import seed_demo

        init_db(os.path.join(self.tmp, "seed.db"))
        from zylker_pm.app import App

        app = App(Config(db_path=os.path.join(self.tmp, "seed.db")), clock=lambda: self.now)
        tokens = seed_demo(app)
        c = Client(app, tokens["alice"])
        fields = {f["key"]: f["type"] for f in self.ok(c.get("/api/project-fields"))["items"]}
        self.assertEqual(fields, {"client": "text", "budget_usd": "number", "wiki": "url"})
        web = self.ok(c.get("/api/projects/WEB"))["custom_fields"]
        self.assertEqual((web["client"], web["budget_usd"]), ("Zylker Retail", 120000))
        self.assertIsNone(self.ok(c.get("/api/projects/INT"))["custom_fields"]["client"])

    def test_seeding_leaves_an_organisations_own_fields_alone(self):
        from helpers import Client

        from zylker_pm.app import App
        from zylker_pm.seed import seed_demo
        from zylker_pm.services import project_fields

        path = os.path.join(self.tmp, "own.db")
        init_db(path)
        app = App(Config(db_path=path), clock=lambda: self.now)
        with app.session() as ctx:  # same key as a demo field, but a different type
            project_fields.create_field(ctx, {"key": "client", "label": "Client", "type": "number"})
        tokens = seed_demo(app)
        c = Client(app, tokens["alice"])
        self.assertEqual([f["type"] for f in self.ok(c.get("/api/project-fields"))["items"]], ["number"])
        self.assertEqual(self.ok(c.get("/api/projects/WEB"))["custom_fields"], {"client": None})

    def test_seeding_refuses_clearly_when_a_required_field_exists(self):
        from zylker_pm.app import App
        from zylker_pm.seed import seed_demo
        from zylker_pm.services import project_fields

        path = os.path.join(self.tmp, "req.db")
        init_db(path)
        app = App(Config(db_path=path), clock=lambda: self.now)
        with app.session() as ctx:
            project_fields.create_field(ctx, {"key": "client", "label": "Client", "type": "text", "required": True})
        with self.assertRaisesRegex(RuntimeError, "required custom project fields"):
            seed_demo(app)
        with app.session(write=False) as ctx:
            self.assertEqual(ctx.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)  # nothing half-seeded


class MigrationTests(FieldsBase):
    def v1_database(self, path):
        """A database exactly as the previous release created it (schema v1, no custom-field tables)."""
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA.replace(db.TASK_EXTRAS_SCHEMA, "").replace(db.PROJECT_FIELDS_SCHEMA, ""))
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO users (id, username, display_name, role, token_hash, created_at) VALUES (1,'old','Old','admin','h','2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO projects (id, key, name, status, owner_id, created_at, updated_at) VALUES (1,'OLD','Legacy','active',1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        conn.commit()
        conn.close()

    @staticmethod
    def shape(path):
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        return rows, version

    def test_a_v1_database_upgrades_in_place_keeping_its_data(self):
        path = os.path.join(self.tmp, "v1.db")
        self.v1_database(path)
        init_db(path)
        fresh = os.path.join(self.tmp, "fresh.db")
        init_db(fresh)
        self.assertEqual(self.shape(path), self.shape(fresh))  # the migration and a new install end up identical
        self.assertEqual(self.shape(path)[1], db.SCHEMA_VERSION)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT name FROM projects").fetchall(), [("Legacy",)])
        conn.close()
        init_db(path)  # and running it again is a no-op
        self.assertEqual(self.shape(path), self.shape(fresh))

    def test_the_upgraded_database_works_through_the_api(self):
        from helpers import Client

        from zylker_pm.app import App
        from zylker_pm.services import users

        path = os.path.join(self.tmp, "v1b.db")
        self.v1_database(path)
        init_db(path)
        app = App(Config(db_path=path), clock=lambda: self.now)
        with app.session() as ctx:
            _, token = users.create_user(ctx, {"username": "root", "display_name": "Root", "role": "admin"})
        c = Client(app, token)
        self.ok(c.post("/api/project-fields", {"key": "client", "label": "Client", "type": "text"}), 201)
        legacy = self.ok(c.get("/api/projects/OLD"))
        self.assertEqual(legacy["custom_fields"], {"client": None})
        self.ok(c.patch("/api/projects/OLD", {"custom_fields": {"client": "Acme"}}))

    def test_a_failed_migration_leaves_the_database_untouched(self):
        path = os.path.join(self.tmp, "broken.db")
        self.v1_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE project_fields (surprise INTEGER)")  # makes step one of the migration blow up
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        rows, version = self.shape(path)
        self.assertEqual(version, 1)
        self.assertNotIn("project_field_values", [r[1] for r in rows])

    def test_a_database_from_a_newer_release_is_refused(self):
        path = os.path.join(self.tmp, "future.db")
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = %d" % (db.SCHEMA_VERSION + 1))
        conn.close()
        with self.assertRaises(RuntimeError):
            init_db(path)
