"""Browser end-to-end tests for the Microsoft Project card in the project settings (Playwright + Chromium; skipped when
unavailable).

They drive the real page against the real threaded server: the export download, a dry run, the real import, the lists of
errors and warnings, files that are not plans, hostile names inside a file, who sees what, a phone-sized window and the
places the import shows up afterwards (activity feed, webhook form). Any console error, CSP violation or uncaught exception
fails the test.
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
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

from msproject_helpers import assignment, doc, resource, task  # noqa: E402

from zylker_pm.app import App  # noqa: E402
from zylker_pm.cli import make_threaded_server  # noqa: E402
from zylker_pm.config import Config  # noqa: E402
from zylker_pm.db import init_db  # noqa: E402
from zylker_pm.services import users  # noqa: E402

JUNK = re.compile(r"\bnull\b|\bundefined\b|\bNaN\b|\[object")
NS = "{http://schemas.microsoft.com/project}"
PROJECTS = iter(range(1, 10000))


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


def api(user, method, path, body=None, raw=None, content_type=None, query=""):
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + "/api" + path + query, method=method, data=data)
    req.add_header("Authorization", "Bearer " + TOKENS[user])
    if data is not None:
        req.add_header("Content-Type", content_type or "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = r.read()
            return r.status, json.loads(payload) if payload and r.headers.get_content_type() == "application/json" else payload
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def plan(**project):
    """A guide with two subtasks (the second one first in the order of work) and a milestone the guide leads into."""
    return doc(
        task(1, "Release 2", 1),
        task(2, "Write the guide", 2, Duration="PT16H0M0S", Work="PT16H0M0S", links=[3]),
        task(3, "Collect screenshots", 2, Duration="PT8H0M0S"),
        task(4, "Guide ready", 1, Milestone=1, Finish="2026-11-20T17:00:00", links=[2]),
        **project,
    )


def titles(key):
    status, listing = api("mia", "GET", "/tasks", query="?project=%s&limit=100&sort=number" % key)
    assert status == 200, listing
    return listing["items"]


class MspUi(unittest.TestCase):
    viewport = {"width": 1360, "height": 1000}

    def setUp(self):
        self.problems, self.contexts = [], []
        self.key = "U%d" % next(PROJECTS)
        status, made = api("mia", "POST", "/projects", {"key": self.key, "name": "Guide %s" % self.key, "members": ["max"]})
        assert status == 201, made

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def open(self, user, hash_, viewport=None):
        context = BROWSER.new_context(viewport=viewport or self.viewport, accept_downloads=True)
        self.contexts.append(context)
        context.add_init_script("sessionStorage.setItem('zpm_token', %s)" % json.dumps(TOKENS[user]))
        page = context.new_page()

        def on_console(m):
            if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource"):
                self.problems.append("console.%s: %s" % (m.type, m.text))

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.on("response", lambda r: self.problems.append("HTTP %d from %s" % (r.status, r.url)) if r.status >= 500 else None)
        page.goto(BASE + "/" + hash_)
        page.wait_for_selector(".content")
        return page

    def settings(self, user="mia", key=None, **kw):
        page = self.open(user, "#/p/%s/settings" % (key or self.key), **kw)
        expect(page.get_by_role("heading", name="Microsoft Project")).to_be_visible()
        return page

    def card(self, page):
        return page.locator(".card", has=page.get_by_role("heading", name="Microsoft Project"))

    def choose(self, page, data, name="plan.xml"):
        page.get_by_label("Microsoft Project XML file").set_input_files(files=[{"name": name, "mimeType": "text/xml", "buffer": data}])

    def run_import(self, page, data, dry_run=False, name="plan.xml"):
        self.choose(page, data, name)
        if not dry_run:
            self.card(page).get_by_label("Dry run (check only, save nothing)").uncheck()
        self.card(page).get_by_role("button", name="Run Microsoft Project import").click()

    def no_junk(self, page):
        match = JUNK.search(self.card(page).inner_text())
        self.assertIsNone(match, "stray %r in the card" % (match and match.group()))


class ExportTests(MspUi):
    def test_the_button_downloads_the_project_as_a_microsoft_project_file(self):
        api("mia", "POST", "/projects/%s/tasks" % self.key, {"title": "Gather the screenshots", "estimate_hours": 6, "type": "story"})
        status, second = api("mia", "POST", "/projects/%s/tasks" % self.key, {"title": "Write the words", "assignee": "max"})
        self.assertEqual(status, 201)
        page = self.settings()
        with page.expect_download() as download:
            page.get_by_role("button", name="Export for Microsoft Project").click()
        self.assertEqual(download.value.suggested_filename, f"{self.key}-msproject.xml")
        root = ET.parse(download.value.path()).getroot()
        self.assertEqual(root.tag, NS + "Project")
        self.assertEqual(root.findtext(NS + "Author"), "Zylker Projects")
        names = [t.findtext(NS + "Name") for t in root.find(NS + "Tasks")]
        self.assertEqual(names, ["Guide %s" % self.key, "Gather the screenshots", "Write the words"])  # the project row comes first
        self.assertEqual(second["key"], "%s-2" % self.key)
        self.no_junk(page)

    def test_a_viewer_and_a_member_can_export_and_cannot_import(self):
        api("mia", "POST", "/projects/%s/tasks" % self.key, {"title": "Seen by everybody"})
        for user in ("val", "max"):
            page = self.settings(user)
            card = self.card(page)
            expect(card.get_by_role("button", name="Export for Microsoft Project")).to_be_visible()
            expect(card.get_by_text("Import from Microsoft Project")).to_have_count(0)
            expect(card.locator("input[type=file]")).to_have_count(0)
            expect(card.get_by_role("button", name="Run Microsoft Project import")).to_have_count(0)
            with page.expect_download() as download:
                card.get_by_role("button", name="Export for Microsoft Project").click()
            with open(download.value.path(), "rb") as saved:
                self.assertIn(b"Seen by everybody", saved.read())

    def test_an_export_that_cannot_be_made_says_so_instead_of_downloading_nothing(self):
        page = self.settings()
        api("prem", "DELETE", "/projects/%s" % self.key, query="?confirm=%s" % self.key)
        page.get_by_role("button", name="Export for Microsoft Project").click()
        expect(page.locator(".toast.error")).to_contain_text("not found")


class ImportTests(MspUi):
    def test_a_dry_run_reports_and_saves_nothing_and_the_real_import_then_creates_everything(self):
        page = self.settings()
        card = self.card(page)
        expect(card.get_by_label("Dry run (check only, save nothing)")).to_be_checked()  # the careful way round is the default
        self.choose(page, plan())
        self.card(page).get_by_role("button", name="Run Microsoft Project import").click()
        expect(card).to_contain_text("Dry run \u2014 nothing was saved. 4 tasks read: 3 would be created, 0 already here, 1 milestone, 1 dependency, 0 errors, 0 warnings.")
        self.assertEqual(titles(self.key), [])
        self.assertEqual(api("mia", "GET", "/projects/%s/milestones" % self.key)[1]["items"], [])
        card.get_by_label("Dry run (check only, save nothing)").uncheck()
        self.card(page).get_by_role("button", name="Run Microsoft Project import").click()
        expect(card).to_contain_text("Import finished. 4 tasks read: 3 created, 0 already here, 1 milestone, 1 dependency, 0 errors, 0 warnings.")
        expect(page.locator(".toast.ok")).to_have_text("3 tasks imported")
        rows = {t["title"]: t for t in titles(self.key)}
        self.assertEqual(sorted(rows), ["Collect screenshots", "Release 2", "Write the guide"])
        self.assertEqual((rows["Write the guide"]["parent"], rows["Write the guide"]["estimate_hours"]), (rows["Release 2"]["key"], 16.0))
        self.assertEqual(rows["Write the guide"]["milestone"]["name"], "Guide ready")
        status, detail = api("mia", "GET", "/tasks/" + rows["Write the guide"]["key"])
        self.assertEqual([d["title"] for d in detail["blocked_by"]], ["Collect screenshots"])
        self.no_junk(page)

    def test_the_same_file_again_adds_nothing_and_says_so(self):
        page = self.settings()
        self.run_import(page, plan())
        expect(self.card(page)).to_contain_text("3 created")
        self.run_import(page, plan())
        expect(self.card(page)).to_contain_text("Import finished. 4 tasks read: 0 created, 3 already here, 0 milestones, 0 dependencies, 0 errors, 0 warnings.")
        self.assertEqual(len(titles(self.key)), 3)

    def test_errors_and_warnings_are_listed_with_the_task_they_belong_to(self):
        data = doc(
            task(1, "Fine"), task(2, "Loop A", links=[4]), task(3, "Loop B", links=[2]), task(4, "Loop C", links=[3]),
            resources=[resource(1, "Nobody Known")], assignments=[assignment(1, 1, "PT8H0M0S")],
        )  # fmt: skip
        page = self.settings()
        self.run_import(page, data)
        card = self.card(page)
        expect(card).to_contain_text("4 tasks read: 4 created, 0 already here, 0 milestones, 2 dependencies, 1 error, 1 warning.")
        errors = card.locator("div:has(> strong.error-text)")
        expect(errors).to_have_count(1)
        expect(errors).to_contain_text("Errors")
        expect(errors).to_contain_text("UID 4: the dependency on UID 3 was not imported:")
        expect(errors).to_contain_text("circular wait")
        warnings = card.locator("div:has(> strong:text-is('Warnings'))")
        expect(warnings).to_contain_text("Nobody Known: 1 task left unassigned because there is no active user with that name or e-mail address")
        self.assertEqual(len(titles(self.key)), 4)  # what could be imported was

    def test_a_long_list_is_cut_and_says_how_many_more_there_are(self):
        many = [task(i, "Task %d" % i, links=[(1000, 1, 0, 7, 1)]) for i in range(1, 31)]  # 30 links into other project files
        page = self.settings()
        self.run_import(page, doc(*many))
        card = self.card(page)
        expect(card).to_contain_text("30 warnings.")
        expect(card.locator("div.muted", has_text="a dependency on a task in another project file was not imported")).to_have_count(20)
        expect(card).to_contain_text("\u2026and 10 more")

    def test_a_file_that_is_not_a_plan_is_refused_with_the_reason_and_nothing_is_written(self):
        page = self.settings()
        for data, name, reason in (
            (b"<notes>hello</notes>", "notes.xml", "this is not a Microsoft Project file"),
            (b"<Project><Tasks></Project>", "broken.xml", "not well-formed XML"),
            (b"", "empty.xml", "the file is empty"),
            (b'<!DOCTYPE x [<!ENTITY a "b">]><Project/>', "evil.xml", "DOCTYPE"),
        ):
            self.run_import(page, data, name=name)
            expect(page.locator(".toast.error").last).to_contain_text(reason)
        self.assertEqual(titles(self.key), [])

    def test_pressing_the_button_without_a_file_asks_for_one(self):
        page = self.settings()
        self.card(page).get_by_role("button", name="Run Microsoft Project import").click()
        expect(page.locator(".toast.error")).to_have_text("Choose a Microsoft Project XML file first")
        expect(self.card(page).get_by_role("button", name="Run Microsoft Project import")).to_be_enabled()

    def test_people_who_are_not_members_are_added_when_the_box_is_ticked(self):
        status, _ = api("prem", "POST", "/users", {"username": "outsider", "display_name": "Olive Outsider", "role": "member", "email": "olive@example.com"})
        self.assertEqual(status, 201)
        data = doc(task(1, "Needs Olive"), resources=[resource(1, "Olive Outsider", EmailAddress="olive@example.com")], assignments=[assignment(1, 1, "PT8H0M0S")])
        page = self.settings()
        self.run_import(page, data, dry_run=True)
        expect(self.card(page)).to_contain_text("outsider: 1 task left unassigned because they are not a member of the project")
        self.card(page).get_by_label("Add missing people as members").check()
        self.card(page).get_by_label("Dry run (check only, save nothing)").uncheck()
        self.card(page).get_by_role("button", name="Run Microsoft Project import").click()
        expect(self.card(page)).to_contain_text("1 created")
        self.assertEqual(titles(self.key)[0]["assignee"]["username"], "outsider")
        self.assertIn("outsider", [m["username"] for m in api("mia", "GET", "/projects/" + self.key)[1]["members"]])

    def test_what_is_exported_from_one_project_comes_into_another_through_the_browser(self):
        first = self.key
        _, epic = api("mia", "POST", "/projects/%s/tasks" % first, {"title": "Epic", "type": "epic"})
        _, child = api("mia", "POST", "/projects/%s/tasks" % first, {"title": "Child", "parent": epic["key"], "estimate_hours": 12.5, "assignee": "max", "due_date": "2026-12-04"})
        _, later = api("mia", "POST", "/projects/%s/tasks" % first, {"title": "Later", "priority": "critical"})
        self.assertEqual(api("mia", "POST", "/tasks/%s/dependencies" % later["key"], {"blocked_by": child["key"], "type": "start_to_start", "lag_days": 2})[0], 200)
        page = self.settings()
        with page.expect_download() as download:
            page.get_by_role("button", name="Export for Microsoft Project").click()
        path = download.value.path()
        self.key = "U%d" % next(PROJECTS)
        self.assertEqual(api("mia", "POST", "/projects", {"key": self.key, "name": "Copy", "members": ["max"]})[0], 201)
        page = self.settings()
        page.get_by_label("Microsoft Project XML file").set_input_files(path)
        self.card(page).get_by_label("Dry run (check only, save nothing)").uncheck()
        self.card(page).get_by_role("button", name="Run Microsoft Project import").click()
        expect(self.card(page)).to_contain_text("3 tasks read: 3 created")  # the project's own row is not a task
        rows = {t["title"]: t for t in titles(self.key)}
        self.assertEqual((rows["Child"]["parent"], rows["Child"]["estimate_hours"], rows["Child"]["due_date"], rows["Child"]["assignee"]["username"]), (rows["Epic"]["key"], 12.5, "2026-12-04", "max"))
        self.assertEqual((rows["Epic"]["type"], rows["Later"]["priority"]), ("epic", "critical"))
        _, detail = api("mia", "GET", "/tasks/" + rows["Later"]["key"])
        self.assertEqual([(d["title"], d["type"], d["lag_days"]) for d in detail["blocked_by"]], [("Child", "start_to_start", 2)])

    def test_names_in_the_file_are_text_never_markup(self):
        hostile = '<img src=x onerror="window.pwned=1">'
        data = doc(
            task(1, "<script>window.pwned=2</script>"),
            resources=[resource(1, hostile)], assignments=[assignment(1, 1, "PT8H0M0S")],
        )  # fmt: skip
        page = self.settings()
        self.run_import(page, data)
        card = self.card(page)
        expect(card).to_contain_text(hostile + ": 1 task left unassigned")  # shown as the characters they are
        self.assertEqual(card.locator("img").count(), 0)
        listing = self.open("mia", "#/p/%s/list" % self.key)
        expect(listing.locator(".content")).to_contain_text("<script>window.pwned=2</script>")
        for each in (page, listing):
            self.assertIsNone(each.evaluate("window.pwned"))


class AfterwardsTests(MspUi):
    def test_the_activity_feed_names_the_source_of_an_import(self):
        page = self.settings()
        self.run_import(page, plan())
        expect(self.card(page)).to_contain_text("3 created")
        csv = b"title,priority\nFrom a spreadsheet,High\n"
        self.assertEqual(api("mia", "POST", "/projects/%s/import" % self.key, raw=csv, content_type="text/csv")[0], 200)
        feed = self.open("mia", "#/p/%s/activity" % self.key)
        expect(feed.locator(".card")).to_contain_text("imported tasks from Microsoft Project: 3 created, 0 skipped, 0 errors")
        expect(feed.locator(".card")).to_contain_text("imported tasks from CSV: 1 created, 0 skipped, 0 errors")

    def test_the_webhook_form_calls_the_import_events_imports(self):
        page = self.open("prem", "#/webhooks")
        page.get_by_role("button", name="+ New webhook").click()
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Imports \u2014 everything")
        expect(dialog).not_to_contain_text("CSV import")


class PhoneTests(MspUi):
    def test_the_card_fits_a_phone_and_every_control_can_be_reached(self):
        page = self.settings(viewport={"width": 390, "height": 800})
        card = self.card(page)
        card.scroll_into_view_if_needed()
        for control in (card.get_by_role("button", name="Export for Microsoft Project"), card.get_by_label("Microsoft Project XML file"), card.get_by_label("Dry run (check only, save nothing)"),
                        card.get_by_label("Add missing people as members"), card.get_by_role("button", name="Run Microsoft Project import")):
            control.scroll_into_view_if_needed()
            box = control.bounding_box()
            self.assertGreaterEqual(box["x"], 0)
            self.assertLessEqual(box["x"] + box["width"], 390.5, control)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        self.run_import(page, plan())
        expect(card).to_contain_text("3 created")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)


if __name__ == "__main__":
    unittest.main()
