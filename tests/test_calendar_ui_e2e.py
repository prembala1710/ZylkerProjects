"""Browser tests for the Calendar page (Playwright + Chromium). Skipped automatically when unavailable.

They run the real threaded server against the demo dataset, drive the actual UI, and fail on any console error / CSP
violation / uncaught exception and on stray "null" / "undefined" / "NaN" text. Every address that is created in the page is
then fetched over HTTP, like a calendar app would, so the UI is checked against what the feed really contains.
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

JUNK = re.compile(r"\bnull\b|\bundefined\b|\bNaN\b|\[object")


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
    with app.session() as ctx:
        _, admin = users.create_user(ctx, {"username": "prem", "display_name": "Prem B", "role": "admin"})
    TOKENS = {"prem": admin, **seed_demo(app)}
    SERVER = make_threaded_server(app, "127.0.0.1", 0)
    BASE = "http://127.0.0.1:%d" % SERVER.server_port
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    if sync_playwright is None or "SERVER" not in globals():
        return
    SERVER.shutdown()
    SERVER.server_close()
    BROWSER.close()
    PW.stop()
    shutil.rmtree(TMP, True)


def api(token, method, path, body=None):
    req = urllib.request.Request(BASE + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def fetch_feed(link):
    """What a calendar app does: a plain GET of the address, no credentials. Returns (status, text)."""
    try:
        with urllib.request.urlopen(link, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def clear_feeds(user):
    status, listing = api(TOKENS[user], "GET", "/calendar/feeds")
    assert status == 200, listing
    for feed in listing["items"]:
        assert api(TOKENS[user], "DELETE", "/calendar/feeds/%d" % feed["id"])[0] == 204


class UiCase(unittest.TestCase):
    viewport = {"width": 1360, "height": 900}

    def setUp(self):
        self.problems = []
        self.contexts = []
        for user in ("prem", "alice", "bob", "carol", "erin"):
            clear_feeds(user)

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def open(self, user, hash_="#/calendar", viewport=None, **ctx_args):
        context = BROWSER.new_context(viewport=viewport or self.viewport, **ctx_args)
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

    def assert_clean_text(self, page, where):
        match = JUNK.search(page.inner_text("body"))
        self.assertIsNone(match, "stray %r on %s" % (match and match.group(), where))

    def create_feed(self, page, scope="My tasks", project=None, name=None, done=False, milestones=True):
        """Drive the dialog and return the address it shows."""
        page.click("#new-feed")
        dialog = page.get_by_role("dialog", name="New calendar feed")
        expect(dialog).to_be_visible()
        if scope != "My tasks":
            dialog.locator("select[aria-label='What to show']").select_option(label=scope)
            if project:
                dialog.locator("select[aria-label='Project']").select_option(value=project)
            if not milestones:
                dialog.locator("#feed-milestones").uncheck()
        if name:
            dialog.locator("input[type=text]").fill(name)
        if done:
            dialog.locator("#feed-done").check()
        dialog.get_by_role("button", name="Create feed").click()
        expect(page.locator("#feed-link")).to_be_visible()
        link = page.inner_text("#feed-link")
        page.get_by_role("button", name="Done").click()
        expect(page.locator(".modal")).to_have_count(0)
        return link


class CalendarPageTests(UiCase):
    def test_the_page_is_in_the_sidebar_for_every_role_and_starts_empty(self):
        for user in ("erin", "bob", "alice", "prem"):
            page = self.open(user, "#/dashboard")
            link = page.locator("a.nav-link[data-route=calendar]")
            expect(link).to_have_text("Calendar")
            link.click()
            expect(page.locator("h1")).to_have_text("Calendar feeds")
            expect(link).to_have_class(re.compile(r"\bactive\b"))
            expect(page.locator(".card.empty h2")).to_have_text("No calendar feeds yet")
            expect(page.locator("#new-feed")).to_be_enabled()
            self.assert_clean_text(page, "calendar page of " + user)

    def test_a_new_feed_shows_its_link_once_and_the_link_works(self):
        page = self.open("bob")
        link = self.create_feed(page)
        self.assertRegex(link, r"^%s/api/calendar/zcf_[A-Za-z0-9_-]{43}\.ics$" % re.escape(BASE))
        row = page.locator("tr[data-feed]")
        expect(row).to_have_count(1)
        expect(row).to_contain_text("My tasks")
        expect(row).to_contain_text("Open tasks assigned to you")
        expect(row).to_contain_text("Never fetched")
        body = page.inner_text("body")
        self.assertNotIn("zcf_", body)  # the link is not on the page any more
        stored = page.evaluate("JSON.stringify(sessionStorage) + JSON.stringify(localStorage)")
        self.assertNotIn("zcf_", stored)  # nor kept anywhere in the browser
        self.assertNotIn("zcf_", page.content())
        status, text = fetch_feed(link)
        self.assertEqual(status, 200)
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("WEB-5 Homepage layout", text)  # Bob's in-review task from the demo data
        page.reload()
        page.wait_for_selector("tr[data-feed]")
        expect(page.locator("tr[data-feed] td").nth(1)).to_have_text("just now \u00b7 1 fetch")  # the whole cell: one fetch, singular
        self.assertEqual(fetch_feed(link)[0], 200)
        self.assertEqual(fetch_feed(link)[0], 200)
        page.reload()
        page.wait_for_selector("tr[data-feed]")
        expect(page.locator("tr[data-feed] td").nth(1)).to_have_text("just now \u00b7 3 fetches")
        self.assert_clean_text(page, "feed list")

    def test_the_page_says_what_a_feed_covers(self):
        page = self.open("bob")
        expect(page.locator(".page-head")).to_contain_text("from 60 days back to 365 days ahead")
        expect(page.locator(".page-head")).to_contain_text("anyone who has it can see the titles and dates")

    def test_without_any_project_only_my_tasks_can_be_chosen(self):
        page = self.open("bob", "#/dashboard")
        page.route("**/api/projects", lambda route: route.fulfill(status=200, content_type="application/json", body='{"items": []}'))
        page.goto(BASE + "/#/calendar")
        page.reload()
        page.wait_for_selector("#new-feed")
        page.click("#new-feed")
        dialog = page.get_by_role("dialog", name="New calendar feed")
        self.assertEqual(dialog.locator("select[aria-label='What to show'] option").all_inner_texts(), ["My tasks"])
        dialog.get_by_role("button", name="Create feed").click()
        expect(page.locator("#feed-link")).to_be_visible()

    def test_the_dialog_warns_that_a_local_address_cannot_be_reached_by_hosted_calendars(self):
        page = self.open("bob")
        page.click("#new-feed")
        page.get_by_role("button", name="Create feed").click()
        expect(page.locator("#feed-link")).to_be_visible()
        expect(page.locator("#feed-local")).to_contain_text("only works on this computer")
        expect(page.locator(".howto")).to_contain_text("Google Calendar")
        expect(page.locator(".howto")).to_contain_text("Outlook")
        expect(page.locator(".howto")).to_contain_text("Apple Calendar")

    def test_the_copy_button_copies_the_link_or_says_what_to_do(self):
        page = self.open("bob")
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
        page.click("#new-feed")
        page.get_by_role("button", name="Create feed").click()
        link = page.inner_text("#feed-link")
        page.get_by_role("button", name="Copy to clipboard").click()
        expect(page.get_by_role("button", name="Copied")).to_be_visible()
        self.assertEqual(page.evaluate("navigator.clipboard.readText()"), link)
        page.get_by_role("button", name="Done").click()
        page.click("#new-feed")
        page.get_by_role("button", name="Create feed").click()
        expect(page.locator("#feed-link")).to_be_visible()
        page.evaluate("() => { navigator.clipboard.writeText = () => Promise.reject(new Error('denied')); }")
        page.get_by_role("button", name="Copy to clipboard").click()
        expect(page.locator(".toast.error")).to_have_text("Select the link and copy it manually")

    def test_a_project_feed_with_milestones_and_finished_tasks(self):
        page = self.open("alice")
        dialog_scope = "The tasks of a project"
        link = self.create_feed(page, scope=dialog_scope, project="WEB", name="Web plan", done=True)
        row = page.locator("tr[data-feed]")
        expect(row).to_contain_text("Web plan")
        expect(row).to_contain_text("Project WEB: tasks and milestones, finished tasks included")
        status, text = fetch_feed(link)
        self.assertEqual(status, 200)
        self.assertIn("X-WR-CALNAME:Web plan", text)
        self.assertIn("SUMMARY:Milestone: Beta launch", text)
        self.assertIn("SUMMARY:WEB-5 Homepage layout", text)
        self.assertNotIn("INT-1", text)  # another project
        plain = self.create_feed(page, scope=dialog_scope, project="APP", milestones=False)
        expect(page.locator("tr[data-feed]").nth(1)).to_contain_text("Project APP: tasks")
        expect(page.locator("tr[data-feed]").nth(1)).not_to_contain_text("milestones")
        _, app_text = fetch_feed(plain)
        self.assertNotIn("Milestone:", app_text)
        self.assertIn("APP-1 Offline sync engine", app_text)

    def test_the_project_choices_appear_only_for_a_project_feed(self):
        page = self.open("alice")
        page.click("#new-feed")
        dialog = page.get_by_role("dialog", name="New calendar feed")
        expect(dialog.locator("select[aria-label='Project']")).to_be_hidden()
        expect(dialog.locator("#feed-milestones")).to_be_hidden()
        dialog.locator("select[aria-label='What to show']").select_option(label="The tasks of a project")
        expect(dialog.locator("select[aria-label='Project']")).to_be_visible()
        expect(dialog.locator("#feed-milestones")).to_be_checked()
        options = dialog.locator("select[aria-label='Project'] option").all_inner_texts()
        self.assertEqual(sorted(o.split(" ")[0] for o in options), ["APP", "INT", "WEB"])
        dialog.locator("select[aria-label='What to show']").select_option(label="My tasks")
        expect(dialog.locator("select[aria-label='Project']")).to_be_hidden()

    def test_replacing_the_link_needs_a_confirmation_and_kills_the_old_one(self):
        page = self.open("bob")
        old = self.create_feed(page)
        self.assertEqual(fetch_feed(old)[0], 200)
        page.reload()
        page.wait_for_selector("tr[data-feed]")
        expect(page.locator("tr[data-feed]")).to_contain_text("1 fetch")
        page.get_by_role("button", name="New link").click()
        confirm = page.get_by_role("dialog", name="Please confirm")
        expect(confirm).to_contain_text("The old link stops working immediately")
        confirm.get_by_role("button", name="Cancel").click()
        self.assertEqual(fetch_feed(old)[0], 200)  # cancelled: nothing changed
        page.get_by_role("button", name="New link").click()
        page.get_by_role("dialog", name="Please confirm").get_by_role("button", name="Replace link").click()
        expect(page.get_by_role("dialog", name="New link for My tasks")).to_be_visible()
        new = page.inner_text("#feed-link")
        self.assertNotEqual(new, old)
        self.assertEqual(fetch_feed(old)[0], 404)
        self.assertEqual(fetch_feed(new)[0], 200)
        page.get_by_role("button", name="Done").click()
        expect(page.locator("tr[data-feed]")).to_contain_text("Never fetched")  # a new link starts with a clean record, and the list shows it
        page.reload()
        page.wait_for_selector("tr[data-feed]")
        expect(page.locator("tr[data-feed]")).to_contain_text("1 fetch")  # only the new link's fetch is on record

    def test_deleting_a_feed(self):
        page = self.open("bob")
        link = self.create_feed(page)
        page.get_by_role("button", name="Delete").click()
        confirm = page.get_by_role("dialog", name="Please confirm")
        confirm.get_by_role("button", name="Cancel").click()
        expect(page.locator("tr[data-feed]")).to_have_count(1)
        page.get_by_role("button", name="Delete").click()
        page.get_by_role("dialog", name="Please confirm").get_by_role("button", name="Delete").click()
        expect(page.locator(".toast.ok")).to_have_text("Feed deleted")
        expect(page.locator(".card.empty h2")).to_have_text("No calendar feeds yet")
        self.assertEqual(fetch_feed(link)[0], 404)

    def test_each_person_sees_only_their_own_feeds(self):
        bob_page = self.open("bob")
        self.create_feed(bob_page, name="Bob only")
        alice_page = self.open("alice")
        expect(alice_page.locator(".card.empty h2")).to_have_text("No calendar feeds yet")
        carol_page = self.open("carol")
        self.create_feed(carol_page, name="Carol only")
        expect(carol_page.locator("tr[data-feed]")).to_have_count(1)
        expect(carol_page.locator("tr[data-feed]")).not_to_contain_text("Bob only")

    def test_at_the_limit_the_button_is_off_and_deleting_one_turns_it_on(self):
        for i in range(10):
            self.assertEqual(api(TOKENS["carol"], "POST", "/calendar/feeds", {"scope": "mine", "name": "Feed %d" % i})[0], 201)
        page = self.open("carol")
        expect(page.locator("tr[data-feed]")).to_have_count(10)
        expect(page.locator("#new-feed")).to_be_disabled()
        expect(page.locator("#new-feed")).to_have_attribute("title", "You already have 10 feeds")
        page.locator("tr[data-feed]").first.get_by_role("button", name="Delete").click()
        page.get_by_role("dialog", name="Please confirm").get_by_role("button", name="Delete").click()
        expect(page.locator("tr[data-feed]")).to_have_count(9)
        expect(page.locator("#new-feed")).to_be_enabled()

    def test_a_refusal_from_the_server_is_shown_not_swallowed(self):
        for i in range(9):
            api(TOKENS["carol"], "POST", "/calendar/feeds", {"scope": "mine", "name": "Feed %d" % i})
        page = self.open("carol")
        expect(page.locator("#new-feed")).to_be_enabled()  # nine is still allowed...
        self.assertEqual(api(TOKENS["carol"], "POST", "/calendar/feeds", {"scope": "mine"})[0], 201)  # ...until one is made elsewhere
        page.click("#new-feed")
        page.get_by_role("button", name="Create feed").click()
        expect(page.locator(".toast.error")).to_contain_text("You already have 10 calendar feeds")
        expect(page.get_by_role("dialog", name="New calendar feed")).to_be_visible()  # the dialog stays so nothing typed is lost

    def test_a_feed_name_with_markup_is_shown_as_text(self):
        hostile = '<img src=x onerror="window.__pwned=1"> <b>bold</b>'
        self.assertEqual(api(TOKENS["bob"], "POST", "/calendar/feeds", {"scope": "mine", "name": hostile})[0], 201)
        page = self.open("bob")
        expect(page.locator("tr[data-feed] strong")).to_have_text(hostile)
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertEqual(page.locator("tr[data-feed] img, tr[data-feed] b").count(), 0)

    def test_the_dialogs_work_from_the_keyboard(self):
        page = self.open("bob")
        page.focus("#new-feed")
        page.keyboard.press("Enter")
        expect(page.get_by_role("dialog", name="New calendar feed")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator(".modal")).to_have_count(0)
        expect(page.locator("#new-feed")).to_be_focused()  # focus comes back to where it was

    def test_dark_mode_and_a_phone_screen_do_not_overflow(self):
        self.assertEqual(api(TOKENS["bob"], "POST", "/calendar/feeds", {"scope": "mine", "name": "A fairly long feed name to check a narrow screen"})[0], 201)
        page = self.open("bob", viewport={"width": 390, "height": 800}, color_scheme="dark")
        expect(page.locator("tr[data-feed]")).to_have_count(1)
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
        page.click("#new-feed")
        page.get_by_role("button", name="Create feed").click()
        expect(page.locator("#feed-link")).to_be_visible()
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
        box = page.locator(".modal").bounding_box()
        self.assertLessEqual(box["x"] + box["width"], 390.5)


if __name__ == "__main__":
    unittest.main()
