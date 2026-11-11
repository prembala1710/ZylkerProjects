"""Browser end-to-end tests for MSP clients and the client portal (Playwright + Chromium; skipped when unavailable).

They drive the real page against the real threaded server, on the demo organisation (two clients, each with a project and a
contact) plus whatever a test builds for itself: the overview and the page of a client, linking, sharing and unlinking a
project, the message for the client, contacts and their tokens, suspending a client, the preview of the portal, and - from the
other side - a contact who signs in, sees exactly its own project, and finds that every other address leads back to the
portal. The pages are compared with what the API says rather than with numbers that depend on today's date. Any console
error, CSP violation or uncaught exception fails the test.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

from zylker_pm.app import App  # noqa: E402
from zylker_pm.cli import make_threaded_server  # noqa: E402
from zylker_pm.config import Config  # noqa: E402
from zylker_pm.db import init_db  # noqa: E402
from zylker_pm.seed import seed_demo  # noqa: E402
from zylker_pm.services import users  # noqa: E402

COUNTER = iter(range(1, 10000))
JUNK = re.compile(r"\bnull\b|\bundefined\b|\bNaN\b|\[object")
EVIL = '<img src=x onerror="window.pwned=1"> & "quotes" <b>bold</b>'


def setUpModule():
    global PW, BROWSER, SERVER, BASE, TOKENS, TMP
    if sync_playwright is None:
        raise unittest.SkipTest("playwright is not installed")
    PW = sync_playwright().start()
    try:
        BROWSER = PW.chromium.launch()
    except Exception as exc:  # no browser binary
        PW.stop()
        raise unittest.SkipTest("chromium unavailable: %s" % str(exc)[:80]) from None
    import logging

    logging.getLogger("zylker.access").setLevel(logging.ERROR)
    TMP = tempfile.mkdtemp()
    cfg = Config(db_path=os.path.join(TMP, "e2e.db"))
    init_db(cfg.db_path)
    app = App(cfg)
    TOKENS = {}
    with app.session() as ctx:
        for username, role in (("prem", "admin"), ("mia", "manager"), ("max", "member"), ("val", "viewer")):
            _, TOKENS[username] = users.create_user(ctx, {"username": username, "display_name": username.capitalize(), "role": role})
    TOKENS.update(seed_demo(app))  # alice, bob, ... and the contacts rita (Zylker Retail, WEB, full) and milo (Zylker Mobile, APP, summary)
    SERVER = make_threaded_server(app, "127.0.0.1", 0)
    BASE = "http://127.0.0.1:%d" % SERVER.server_port
    threading.Thread(target=SERVER.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()


def tearDownModule():
    if sync_playwright is None or "SERVER" not in globals():
        return
    SERVER.shutdown()
    SERVER.server_close()
    BROWSER.close()
    PW.stop()
    shutil.rmtree(TMP, True)


def api(user, method, path, body=None, query=""):
    req = urllib.request.Request(BASE + "/api" + path + query, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + TOKENS[user])
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = r.read()
            return r.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def must(result, status=200):
    code, payload = result
    assert code == status, (code, payload)
    return payload


def eventually(read, want, timeout=8.0):
    """The server's answer after a click is not instant: wait for ``read()`` to say ``want`` (and fail with what it last said)."""
    import time

    deadline, seen = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        seen = read()
        if seen == want:
            return
        time.sleep(0.05)
    raise AssertionError("%r != %r after %.0fs" % (seen, want, timeout))


def client_named(name):
    return next(c for c in must(api("mia", "GET", "/clients"))["items"] if c["name"] == name)


class Base(unittest.TestCase):
    viewport = {"width": 1360, "height": 1000}

    def setUp(self):
        self.problems, self.contexts = [], []

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def open(self, user=None, route="", token=None, viewport=None, scheme=None, wait="h1"):
        kwargs = {"color_scheme": scheme} if scheme else {}
        context = BROWSER.new_context(viewport=viewport or self.viewport, **kwargs)
        self.contexts.append(context)
        if user or token:
            context.add_init_script("sessionStorage.setItem('zpm_token', %s)" % json.dumps(token or TOKENS[user]))
        page = context.new_page()
        page.set_default_timeout(10000)  # a broken page fails in seconds, not in half a minute per step

        def on_console(m):
            if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource"):
                self.problems.append("console.%s: %s" % (m.type, m.text))

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.on("response", lambda r: self.problems.append("HTTP %d from %s" % (r.status, r.url)) if r.status >= 500 else None)
        page.goto(BASE + "/#/" + route)
        if wait:
            page.wait_for_selector(wait)
        return page

    def world(self, share="full", name=None, text=None):
        """A client of its own, a project of its own that is linked to it, and a contact who can sign in."""
        n = next(COUNTER)
        key = "E%d" % n
        must(api("mia", "POST", "/projects", {"key": key, "name": text or "Project %s" % key, "members": ["max"]}), 201)
        for title, extra in (("Overdue job", {"due_date": "2020-01-01"}), ("Quiet job", {}), ("Finished job", {"status": "done"})):
            must(api("mia", "POST", "/projects/%s/tasks" % key, {"title": title, **extra}), 201)
        client = must(api("mia", "POST", "/clients", {"name": name or "Client %d" % n}), 201)
        must(api("mia", "PUT", "/clients/%d/projects/%s" % (client["id"], key), {"share": share, "update": "Hello from %s" % key}))
        user = "contact%d" % n
        made = must(api("prem", "POST", "/clients/%d/contacts" % client["id"], {"username": user, "display_name": "Contact %d" % n}), 201)
        TOKENS[user] = made["token"]
        return {"key": key, "client": client, "user": user, "n": n}


class OverviewTests(Base):
    def test_the_overview_says_what_the_api_says_the_worst_client_first(self):
        page = self.open("alice", "clients", wait="#clients-table")
        data = must(api("alice", "GET", "/clients"))
        rows = page.locator("#clients-table tbody tr")
        names = [c["name"] for c in data["items"]]
        self.assertEqual(rows.count(), len(names))
        label = {"off_track": "Off track", "at_risk": "At risk", "on_track": "On track", "complete": "Complete", "on_hold": "On hold", "not_started": "Not started"}
        for i, c in enumerate(data["items"]):
            text = rows.nth(i).inner_text()
            self.assertIn(c["name"], text)
            self.assertIn("%d (%d shared)" % (c["projects"]["total"], c["projects"]["shared"]), text)
            if c["health"]:
                self.assertIn(label[c["health"]], text)
            f = c["forecast"]
            if f and f["verdict"] == "will_miss":
                self.assertIn("%d day%s late" % (f["late_by_days"], "" if f["late_by_days"] == 1 else "s"), text)
            elif f and f["verdict"] == "on_track":
                self.assertIn("On time", text)
            cells = rows.nth(i).locator("td").all_inner_texts()
            self.assertEqual((cells[4], cells[5], cells[6]), (str(c["projects"]["open_tasks"]), str(c["projects"]["overdue_tasks"]), str(c["contacts"])), text)
        kpis = page.locator("#clients-kpis .kpi .value").all_inner_texts()
        self.assertEqual(kpis[0], str(len(data["items"])))
        self.assertEqual(kpis[1], str(sum(1 for c in data["items"] if c["active"] and c["health"] in ("off_track", "at_risk"))))
        self.assertEqual(kpis[2], str(sum(c["projects"]["shared"] for c in data["items"])))
        self.assertEqual(kpis[3], str(sum(c["contacts"] for c in data["items"])))
        self.assertIsNone(JUNK.search(page.locator("main").inner_text()))
        rank = {"off_track": 0, "at_risk": 1, "on_track": 2}
        ranks = [rank.get(c["health"], 9) for c in data["items"] if c["active"]]
        self.assertEqual(ranks, sorted(ranks), "the clients that need attention come first")

    def test_a_row_opens_its_client(self):
        page = self.open("alice", "clients", wait="#clients-table")
        first = must(api("alice", "GET", "/clients"))["items"][0]
        page.locator("#clients-table tbody tr").first.locator("td").nth(3).click()  # not on the name
        expect(page.locator("#client-name")).to_have_text(first["name"])

    def test_who_sees_which_buttons(self):
        for user, manage in (("alice", True), ("prem", True), ("max", False), ("val", False)):
            page = self.open(user, "clients", wait="#clients-table")
            self.assertEqual(page.locator("#client-new").count(), 1 if manage else 0, user)
        page = self.open("val", "clients/%d" % client_named("Zylker Retail")["id"], wait="#client-projects")
        self.assertEqual(page.locator("#client-rename, #client-suspend, #client-delete, #contact-new, #client-add-go, select.share, #client-projects button").count(), 0)
        self.assertEqual(page.locator("#client-preview").count(), 1, "anybody on the staff may look at what the client sees")
        page = self.open("alice", "clients/%d" % client_named("Zylker Retail")["id"], wait="#client-projects")
        self.assertEqual(page.locator("#contact-new").count(), 0, "contacts are for admins")
        self.assertEqual(page.locator("#client-contacts button").count(), 0)
        self.assertEqual(page.locator("#client-rename, #client-suspend, select.share, #client-add-go").count(), 4)
        page = self.open("prem", "clients/%d" % client_named("Zylker Retail")["id"], wait="#client-projects")
        self.assertEqual(page.locator("#contact-new, #client-delete").count(), 2)
        self.assertEqual(page.locator("#client-contacts tbody button").all_inner_texts(), ["New token", "Deactivate"])

    def test_creating_a_client_and_the_name_has_to_be_new(self):
        n = next(COUNTER)
        page = self.open("mia", "clients", wait="#clients-table")
        page.locator("#client-new").click()
        dialog = page.get_by_role("dialog", name="New client")
        dialog.get_by_label("Name").fill("zylker retail")  # taken, whatever the case
        dialog.get_by_role("button", name="Create client").click()
        expect(page.locator(".toast.error")).to_contain_text("already a client called")
        expect(dialog).to_be_visible()
        dialog.get_by_label("Name").fill("Fresh Client %d" % n)
        dialog.get_by_label("Name").press("Enter")
        expect(page.locator("#client-name")).to_have_text("Fresh Client %d" % n)
        self.assertEqual(client_named("Fresh Client %d" % n)["projects"]["total"], 0)
        expect(page.locator("#client-projects")).to_contain_text("No project is linked to this client yet.")
        expect(page.locator("#client-contacts")).to_contain_text("nobody can sign in for this client")

    def test_the_name_field_stops_at_the_length_the_server_accepts(self):
        page = self.open("mia", "clients", wait="#clients-table")
        page.locator("#client-new").click()
        field = page.get_by_role("dialog").get_by_label("Name")
        field.fill("x" * 120)
        self.assertEqual(len(field.input_value()), 80)


class ClientPageTests(Base):
    def test_linking_sharing_and_unlinking_a_project(self):
        n = next(COUNTER)
        key = "L%d" % n
        must(api("mia", "POST", "/projects", {"key": key, "name": "Linked %d" % n, "members": []}), 201)
        client = must(api("mia", "POST", "/clients", {"name": "Linker %d" % n}), 201)
        page = self.open("mia", "clients/%d" % client["id"], wait="#client-projects")
        page.locator("#client-add-project").select_option(key)
        page.locator("#client-add-share").select_option("summary")
        page.locator("#client-add-go").click()
        row = page.locator("#client-projects tr[data-project='%s']" % key)
        expect(row).to_be_visible()
        self.assertEqual(row.locator("select.share").input_value(), "summary")
        self.assertEqual(must(api("mia", "GET", "/clients/%d" % client["id"]))["projects"][0]["share"], "summary")
        self.assertNotIn(key, page.locator("#client-add-project").inner_text(), "it is no longer on offer")
        shared = lambda: must(api("mia", "GET", "/clients/%d" % client["id"]))["projects"][0]["share"]  # noqa: E731
        row.locator("select.share").select_option("full")
        eventually(shared, "full")
        expect(page.locator("#client-projects tr[data-project='%s'] select.share" % key)).to_have_value("full")
        page.locator("#client-projects tr[data-project='%s'] select.share" % key).select_option("none")
        eventually(shared, "none")  # and back to nothing
        expect(page.locator("#client-projects tr[data-project='%s'] select.share" % key)).to_have_value("none")
        row.get_by_role("button", name="Remove").click()
        page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
        self.assertEqual(len(must(api("mia", "GET", "/clients/%d" % client["id"]))["projects"]), 1, "cancelled: nothing happened")
        row.get_by_role("button", name="Remove").click()
        page.get_by_role("dialog").get_by_role("button", name="Remove").click()
        expect(page.locator("#client-projects")).to_contain_text("No project is linked to this client yet.")
        self.assertIn(key, page.locator("#client-add-project").inner_text(), "and it can be offered again")

    def test_a_project_that_belongs_to_another_client_is_not_offered(self):
        w = self.world()
        other = must(api("mia", "POST", "/clients", {"name": "Other %d" % w["n"]}), 201)
        page = self.open("mia", "clients/%d" % other["id"], wait="#client-projects")
        self.assertNotIn(w["key"], page.locator("#client-add-project").inner_text())

    def test_the_message_for_the_client(self):
        w = self.world()
        page = self.open("mia", "clients/%d" % w["client"]["id"], wait="#client-projects")
        row = page.locator("#client-projects tr[data-project='%s']" % w["key"])
        expect(row.locator(".update-text")).to_have_text("Hello from %s" % w["key"])
        row.get_by_role("button", name="Edit").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Message").fill("Beta goes out on Friday.\nThanks for your patience.")
        dialog.get_by_role("button", name="Save message").click()
        expect(page.locator("#client-projects tr[data-project='%s'] .update-text" % w["key"])).to_contain_text("Beta goes out on Friday.")
        message = must(api("mia", "GET", "/clients/%d" % w["client"]["id"]))["projects"][0]["update"]
        self.assertEqual((message["text"], message["by"]["username"]), ("Beta goes out on Friday.\nThanks for your patience.", "mia"))
        row.get_by_role("button", name="Edit").click()
        page.get_by_role("dialog").get_by_role("button", name="Take it down").click()
        expect(page.locator("#client-projects tr[data-project='%s']" % w["key"])).to_contain_text("None")
        self.assertIsNone(must(api("mia", "GET", "/clients/%d" % w["client"]["id"]))["projects"][0]["update"])
        contact = self.open(token=TOKENS[w["user"]], route="portal/%s" % w["key"], wait="#portal-headline")
        self.assertEqual(contact.locator("#portal-update").count(), 0, "gone from the portal as well")

    def test_a_contact_is_created_with_a_token_that_is_shown_once_and_works(self):
        w = self.world()
        page = self.open("prem", "clients/%d" % w["client"]["id"], wait="#client-contacts")
        page.locator("#contact-new").click()
        dialog = page.get_by_role("dialog", name="New contact at %s" % w["client"]["name"])
        n = next(COUNTER)
        dialog.get_by_label("Username").fill("newbie%d" % n)
        dialog.get_by_label("Display name").fill("Newbie %d" % n)
        dialog.get_by_role("button", name="Create contact").click()
        token_box = page.locator(".token-box")
        expect(token_box).to_be_visible()
        token = token_box.inner_text()
        self.assertTrue(token.startswith("zpm_"))
        page.get_by_role("button", name="Done").click()
        expect(page.locator("#client-contacts tr[data-contact='newbie%d']" % n)).to_contain_text("Newbie %d" % n)
        self.assertNotIn(token, page.locator("body").inner_text(), "shown once")
        signed_in = self.open(token=token, route="portal", wait="#portal-projects")
        self.assertEqual(signed_in.locator(".portal-card").count(), 1)
        self.assertIn("newbie%d" % n, [c["username"] for c in must(api("prem", "GET", "/clients/%d" % w["client"]["id"]))["contacts"]])

    def test_deactivating_a_contact_signs_it_out_and_reactivating_lets_it_back_in(self):
        w = self.world()
        contact_page = self.open(token=TOKENS[w["user"]], route="portal", wait="#portal-projects")
        page = self.open("prem", "clients/%d" % w["client"]["id"], wait="#client-contacts")
        row = page.locator("#client-contacts tr[data-contact='%s']" % w["user"])
        row.get_by_role("button", name="Deactivate").click()
        expect(page.locator("#client-contacts tr[data-contact='%s']" % w["user"])).to_contain_text("Deactivated")
        contact_page.reload()
        expect(contact_page.locator("#token-input")).to_be_visible()
        expect(contact_page.locator(".error-text")).to_contain_text("no longer valid")
        page.locator("#client-contacts tr[data-contact='%s']" % w["user"]).get_by_role("button", name="Reactivate").click()
        expect(page.locator("#client-contacts tr[data-contact='%s']" % w["user"])).to_contain_text("Active")
        back = self.open(token=TOKENS[w["user"]], route="portal", wait="#portal-projects")
        self.assertEqual(back.locator(".portal-card").count(), 1)

    def test_a_new_token_for_a_contact_replaces_the_old_one(self):
        w = self.world()
        page = self.open("prem", "clients/%d" % w["client"]["id"], wait="#client-contacts")
        page.locator("#client-contacts tr[data-contact='%s']" % w["user"]).get_by_role("button", name="New token").click()
        page.get_by_role("dialog").get_by_role("button", name="Issue new token").click()
        token = page.locator(".token-box").inner_text()
        self.assertNotEqual(token, TOKENS[w["user"]])
        self.assertEqual(api(w["user"], "GET", "/portal/projects")[0], 401, "the old one is dead")
        TOKENS[w["user"]] = token
        self.assertEqual(api(w["user"], "GET", "/portal/projects")[0], 200)

    def test_rename_suspend_reactivate_and_delete(self):
        n = next(COUNTER)
        client = must(api("mia", "POST", "/clients", {"name": "Temp %d" % n}), 201)
        page = self.open("prem", "clients/%d" % client["id"], wait="#client-name")
        page.locator("#client-rename").click()
        dialog = page.get_by_role("dialog", name="Rename client")
        dialog.get_by_label("Name").fill("Renamed %d" % n)
        dialog.get_by_role("button", name="Save").click()
        expect(page.locator("#client-name")).to_have_text("Renamed %d" % n)
        page.locator("#client-suspend").click()
        page.get_by_role("dialog").get_by_role("button", name="Suspend").click()
        expect(page.locator("main .badge", has_text="Suspended")).to_be_visible()
        self.assertFalse(must(api("mia", "GET", "/clients/%d" % client["id"]))["active"])
        page.locator("#client-suspend").click()  # now it says Reactivate and needs no confirmation
        expect(page.locator("main .badge", has_text="Active")).to_be_visible()
        page.locator("#client-delete").click()
        page.get_by_role("dialog").get_by_role("button", name="Delete").click()
        expect(page.locator("#clients-table")).to_be_visible()
        self.assertEqual(api("mia", "GET", "/clients/%d" % client["id"])[0], 404)

    def test_a_client_with_contacts_cannot_be_deleted(self):
        w = self.world()
        page = self.open("prem", "clients/%d" % w["client"]["id"], wait="#client-name")
        expect(page.locator("#client-delete")).to_be_disabled()

    def test_the_project_history_and_the_webhook_form_know_about_clients(self):
        w = self.world()
        must(api("mia", "PUT", "/clients/%d/projects/%s" % (w["client"]["id"], w["key"]), {"share": "summary", "update": "A different message"}))
        page = self.open("mia", "p/%s/activity" % w["key"], wait=".item")
        expect(page.locator(".item", has_text="linked the project to the client")).to_contain_text("Client %d" % w["n"])
        expect(page.locator(".item", has_text="what the client")).to_contain_text("message for the client changed")
        hooks = self.open("prem", "webhooks", wait="h1")
        hooks.get_by_role("button", name="+ New webhook").click()
        hooks.get_by_label("All events").uncheck()
        expect(hooks.get_by_label("Clients \u2014 everything")).to_be_visible()


class PreviewTests(Base):
    def test_staff_see_the_portal_exactly_as_the_client_does(self):
        client = client_named("Zylker Retail")
        page = self.open("alice", "clients/%d/portal" % client["id"], wait="#portal-projects")
        expect(page.locator("#portal-banner")).to_contain_text("This is what Zylker Retail")
        self.assertEqual(page.locator(".portal-card").count(), 1)
        self.assertEqual(page.locator(".sidebar").count(), 1, "the preview lives inside the staff pages")
        page.locator(".portal-card").click()
        expect(page.locator("#portal-headline")).to_be_visible()
        want = must(api("alice", "GET", "/portal/projects/WEB", query="?client=%d" % client["id"]))
        self.assertEqual(page.locator("#portal-headline").inner_text(), want["headline"])
        expect(page.locator("#portal-banner")).to_be_visible()
        page.get_by_role("link", name="Back to Zylker Retail").click()
        expect(page.locator("#client-name")).to_have_text("Zylker Retail")

    def test_the_preview_of_a_summary_shows_no_detail(self):
        page = self.open("alice", "clients/%d/portal/APP" % client_named("Zylker Mobile")["id"], wait="#portal-headline")
        self.assertEqual(page.locator("#portal-forecast, #portal-completed, #portal-changes, #portal-overdue").count(), 0)
        expect(page.locator("#portal-summary-note")).to_be_visible()


class PortalTests(Base):
    def test_signing_in_with_a_contacts_token_lands_in_the_portal_and_only_there(self):
        page = self.open(route="", wait="#token-input")
        page.locator("#token-input").fill(TOKENS["rita"])
        page.get_by_role("button", name="Sign in").click()
        expect(page.locator("#portal-title")).to_have_text("Your projects")
        self.assertEqual(page.url.split("#")[1], "/portal")
        self.assertEqual(page.locator(".sidebar, .topbar, nav[aria-label='Primary'], input[type=search], #project-nav").count(), 0)
        body = page.locator("body").inner_text()
        for staff_only in ("Dashboard", "My tasks", "Team workload", "Clients", "Users", "Webhooks", "New project", "Mobile App", "Cliq Integrations", "Zylker Mobile"):
            self.assertNotIn(staff_only, body, staff_only)
        self.assertEqual(page.locator(".portal-card").count(), 1)
        expect(page.locator(".portal-card")).to_contain_text("Website Redesign")
        expect(page.locator(".portal-top")).to_contain_text("Rita Raman")
        expect(page.locator(".portal-top")).to_contain_text("Zylker Retail")
        page.get_by_role("button", name="Sign out").click()
        expect(page.locator("#token-input")).to_be_visible()

    def test_the_project_page_says_what_the_api_says(self):
        page = self.open("rita", "portal/WEB", wait="#portal-forecast")
        want = must(api("rita", "GET", "/portal/projects/WEB"))
        self.assertEqual(page.locator("#portal-headline").inner_text(), want["headline"])
        self.assertEqual(page.locator("#portal-title").inner_text(), "Website Redesign")
        self.assertEqual(page.locator("#portal-reasons li").all_inner_texts(), want["rating"]["reasons"])
        self.assertEqual(page.locator("#portal-update").inner_text().splitlines()[-1], want["update"]["text"])
        kpis = page.locator("#portal-kpis .kpi .value").all_inner_texts()
        self.assertEqual(kpis[:4], ["%d%%" % round(want["progress"]["tasks"]["percent"]), "%d/%d" % (want["progress"]["tasks"]["done"], want["progress"]["tasks"]["total"]), str(want["progress"]["period"]["completed"]), str(want["progress"]["period"]["added"])])
        self.assertEqual(kpis[4:], [str(want["attention"]["overdue"]["count"]), str(want["attention"]["blocked"]["count"])])
        self.assertEqual(page.locator("#portal-milestones tbody tr").count(), len(want["milestones"]["items"]))
        self.assertIn(want["forecast"]["headline"], page.locator("#portal-forecast").inner_text())
        for section, items in (("#portal-overdue", want["attention"]["overdue"]["items"]), ("#portal-blocked", want["attention"]["blocked"]["items"]),
                               ("#portal-completed", want["completed"]["items"]), ("#portal-upcoming", want["upcoming"]["items"])):
            self.assertEqual(page.locator(section + " li").count(), len(items), section)
            for item in items:
                self.assertIn(item["key"], page.locator(section).inner_text(), section)
        moved = want["changes"]["rescheduled"]
        self.assertEqual(page.locator("#portal-changes ul").first.locator("li").count(), len(moved["items"]))
        self.assertIn("Dates moved (%d" % moved["count"], page.locator("#portal-changes").inner_text())
        self.assertIsNone(JUNK.search(page.locator("main").inner_text()), page.locator("main").inner_text())

    def test_what_is_not_the_clients_to_see_is_not_on_the_page(self):
        page = self.open("rita", "portal/WEB", wait="#portal-forecast")
        text = page.locator("body").inner_text()
        for hidden in ("Bob", "Carol", "Dave", "Okafor", "hours", "estimate", "budget", "Budget", "120000", "Signed-off plan", "busy with", "queue", "Offline sync", "Mobile App", "APP-"):
            self.assertNotIn(hidden, text, hidden)
        self.assertEqual(page.locator("a[href^='#/p/'], a[href^='#/t/']").count(), 0, "nothing on this page leads into the staff pages")

    def test_the_lists_say_how_many_more_there_are(self):
        w = self.world()
        for n in range(12):
            must(api("mia", "POST", "/projects/%s/tasks" % w["key"], {"title": "Late %d" % n, "due_date": "2020-02-%02d" % (n + 1)}), 201)
        page = self.open(token=TOKENS[w["user"]], route="portal/%s" % w["key"], wait="#portal-forecast")
        want = must(api(w["user"], "GET", "/portal/projects/%s" % w["key"]))["attention"]["overdue"]
        self.assertEqual((want["count"], len(want["items"]), want["more"]), (13, 10, 3))
        expect(page.locator("#portal-overdue h2")).to_have_text("Overdue (13)")
        self.assertEqual(page.locator("#portal-overdue li").count(), 10)
        expect(page.locator("#portal-overdue")).to_contain_text("and 3 more")

    def test_the_milestone_table_has_a_forecast_only_at_the_full_level(self):
        full = self.open("rita", "portal/WEB", wait="#portal-forecast")
        self.assertEqual(full.locator("#portal-milestones th").all_inner_texts(), ["Milestone", "Due", "Progress", "Status", "Forecast"])
        want = must(api("rita", "GET", "/portal/projects/WEB"))["milestones"]["items"]
        for i, m in enumerate(want):
            cells = full.locator("#portal-milestones tbody tr").nth(i).locator("td").all_inner_texts()
            self.assertEqual(len(cells), 5)
            self.assertEqual((cells[0], cells[2]), (m["name"], "%d/%d tasks" % (m["tasks_done"], m["tasks_total"])))
        summary = self.open("milo", "portal/APP", wait="#portal-summary-note")
        self.assertEqual(summary.locator("#portal-milestones th").all_inner_texts(), ["Milestone", "Due", "Progress", "Status"])

    def test_the_period_choice(self):
        page = self.open("rita", "portal/WEB", wait="#portal-forecast")
        self.assertEqual(page.locator("#portal-days").input_value(), "7", "a week unless asked otherwise")
        self.assertEqual(must(api("rita", "GET", "/portal/projects/WEB"))["period"]["days"], 7)
        want = must(api("rita", "GET", "/portal/projects/WEB", query="?days=30"))
        page.locator("#portal-days").select_option("30")
        expect(page.locator("#portal-completed h2")).to_contain_text("last 30 days")
        self.assertEqual(page.locator("#portal-headline").inner_text(), want["headline"])
        self.assertEqual(page.locator("#portal-changes h3").first.inner_text().split(" (")[0], "Dates moved")
        self.assertEqual(page.locator("#portal-days").input_value(), "30")
        page.locator("a", has_text="All projects").click()
        page.locator(".portal-card").click()
        self.assertEqual(page.locator("#portal-days").input_value(), "30", "the choice is remembered while the page is open")

    def test_a_project_shared_as_a_summary_shows_a_summary(self):
        page = self.open("milo", "portal/APP", wait="#portal-summary-note")
        want = must(api("milo", "GET", "/portal/projects/APP"))
        self.assertEqual(page.locator("#portal-headline").inner_text(), want["headline"])
        self.assertEqual(page.locator("#portal-forecast, #portal-completed, #portal-upcoming, #portal-changes, #portal-overdue, #portal-blocked").count(), 0)
        self.assertEqual(page.locator("#portal-kpis .kpi").count(), 4)
        self.assertNotIn("Push notifications", page.locator("body").inner_text(), "not a task title in sight")

    def test_every_address_that_belongs_to_the_staff_leads_back_to_the_portal(self):
        page = self.open("rita", "portal", wait="#portal-projects")
        for address in ("dashboard", "mine", "team", "clients", "clients/1", "clients/1/portal", "users", "webhooks", "fields", "p/WEB/board", "p/APP/reports", "t/APP-1", "nonsense"):
            page.goto(BASE + "/#/" + address)
            expect(page.locator("#portal-title")).to_have_text("Your projects")
            self.assertEqual(page.url.split("#")[1], "/portal", address)
            self.assertEqual(page.locator(".portal-card").count(), 1, address)
        self.assertNotIn("Mobile App", page.locator("body").inner_text())

    def test_another_clients_project_is_just_not_found(self):
        page = self.open("rita", "portal/APP", wait=None)
        expect(page.get_by_role("heading", name="Not found")).to_be_visible()
        expect(page.locator("main")).to_contain_text("Project APP not found")
        self.assertNotIn("Mobile App", page.locator("body").inner_text())

    def test_a_client_that_is_suspended_while_somebody_is_reading_signs_them_out(self):
        w = self.world()
        page = self.open(token=TOKENS[w["user"]], route="portal", wait="#portal-projects")
        must(api("mia", "PATCH", "/clients/%d" % w["client"]["id"], {"active": False}))
        page.locator(".portal-card").click()
        expect(page.locator("#token-input")).to_be_visible()
        expect(page.locator(".error-text")).to_contain_text("suspended")
        page.locator("#token-input").fill(TOKENS[w["user"]])
        page.get_by_role("button", name="Sign in").click()
        expect(page.locator(".error-text")).to_contain_text("suspended")
        must(api("mia", "PATCH", "/clients/%d" % w["client"]["id"], {"active": True}))
        page.get_by_role("button", name="Sign in").click()
        expect(page.locator("#portal-title")).to_have_text("Your projects")

    def test_a_contact_with_nothing_shared_is_told_so(self):
        w = self.world(share="none")
        page = self.open(token=TOKENS[w["user"]], route="portal", wait="#portal-empty")
        expect(page.locator("#portal-empty")).to_contain_text("Nothing has been shared with you yet")
        self.assertEqual(page.locator(".portal-card").count(), 0)

    def test_hostile_text_stays_text_everywhere_in_the_portal_and_in_the_staff_pages(self):
        w = self.world(name=EVIL, text=EVIL)
        must(api("mia", "PUT", "/clients/%d/projects/%s" % (w["client"]["id"], w["key"]), {"update": EVIL}))
        must(api("mia", "POST", "/projects/%s/tasks" % w["key"], {"title": EVIL, "due_date": "2020-01-02"}), 201)
        for user, route in ((w["user"], "portal"), (w["user"], "portal/%s" % w["key"]), ("mia", "clients"), ("mia", "clients/%d" % w["client"]["id"]),
                            ("mia", "clients/%d/portal/%s" % (w["client"]["id"], w["key"]))):
            page = self.open(token=TOKENS[user], route=route, wait="h1")
            page.wait_for_timeout(300)
            self.assertEqual(page.locator("main img, main b, main script").count(), 0, route)
            self.assertIsNone(page.evaluate("window.pwned"), route)
        page = self.open(token=TOKENS[w["user"]], route="portal/%s" % w["key"], wait="#portal-forecast")
        self.assertIn(EVIL, page.locator("#portal-title").inner_text())
        self.assertIn(EVIL, page.locator("#portal-overdue").inner_text())
        self.assertIn(EVIL, page.locator("#portal-update").inner_text())

    def test_printing_leaves_only_the_report(self):
        page = self.open("rita", "portal/WEB", wait="#portal-forecast")
        page.emulate_media(media="print")
        for hidden in (".portal-top", "#portal-print", "#portal-days"):
            self.assertFalse(page.locator(hidden).first.is_visible(), hidden)
        self.assertTrue(page.locator("#portal-headline").is_visible())
        self.assertTrue(page.locator("#portal-forecast").is_visible())

    def test_dark_mode_and_a_phone_sized_window(self):
        for scheme in ("light", "dark"):
            page = self.open("rita", "portal/WEB", scheme=scheme, wait="#portal-forecast")
            for sel in ("#portal-headline", "#portal-title"):
                self.assertTrue(page.locator(sel).is_visible(), sel)
            bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
            self.assertEqual(bg == "rgb(17, 20, 27)", scheme == "dark", scheme)
        page = self.open("rita", "portal/WEB", viewport={"width": 390, "height": 800}, scheme="dark", wait="#portal-forecast")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391, "the page itself does not scroll sideways")
        page = self.open("rita", "portal", viewport={"width": 390, "height": 800}, wait="#portal-projects")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        page = self.open("alice", "clients/%d" % client_named("Zylker Retail")["id"], viewport={"width": 390, "height": 800}, wait="#client-projects")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391, "a wide table scrolls inside its card, not the page")

    def test_a_failing_request_is_explained_not_hidden(self):
        page = self.open("rita", "portal", wait="#portal-projects")
        page.route("**/api/portal/projects/WEB*", lambda r: r.fulfill(status=409, content_type="application/json", body='{"error":{"code":"x","message":"Try again in a minute."}}'))
        page.locator(".portal-card").click()
        expect(page.locator("main")).to_contain_text("Try again in a minute.")
        expect(page.get_by_role("button", name="Try again")).to_be_visible()


if __name__ == "__main__":
    unittest.main()
