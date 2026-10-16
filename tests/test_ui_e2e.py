"""Browser end-to-end tests (Playwright + Chromium). Skipped automatically when unavailable.

They run the real threaded server against the demo dataset and drive the actual UI, failing on any
console error / CSP violation / uncaught exception, and on stray "null" / "undefined" / "NaN" text.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.request

sys_path_fix = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(sys_path_fix))

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
    logging_off()
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


def logging_off():
    import logging

    logging.getLogger("zylker.access").setLevel(logging.ERROR)


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


class UiCase(unittest.TestCase):
    viewport = {"width": 1360, "height": 900}

    def setUp(self):
        self.problems = []
        self.contexts = []

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def open(self, user=None, hash_="#/dashboard", viewport=None, **ctx_args):
        context = BROWSER.new_context(viewport=viewport or self.viewport, **ctx_args)
        self.contexts.append(context)
        if user:
            context.add_init_script("sessionStorage.setItem('zpm_token', %s)" % json.dumps(TOKENS[user]))
        page = context.new_page()
        # Chromium logs "Failed to load resource" for every non-2xx reply, including the 401/409 answers that
        # negative tests provoke on purpose. Those are asserted through the UI; real problems are CSP
        # violations, script errors, uncaught exceptions and any 5xx.
        def on_console(m):
            if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource"):
                self.problems.append("console.%s: %s" % (m.type, m.text))

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.on("response", lambda r: self.problems.append("HTTP %d from %s" % (r.status, r.url)) if r.status >= 500 else None)
        page.goto(BASE + "/" + hash_)
        if user:
            page.wait_for_selector(".content")
        return page

    def settle(self, page):
        page.wait_for_load_state("networkidle")

    def assert_clean_text(self, page, where):
        text = page.inner_text("body")
        match = JUNK.search(text)
        self.assertIsNone(match, "stray %r on %s" % (match and match.group(), where))


class LoginTests(UiCase):
    def test_wrong_token_then_right_token_then_sign_out(self):
        page = self.open()
        page.fill("#token-input", "zpm_definitely-wrong")
        page.click("button[type=submit]")
        expect(page.locator(".error-text")).to_have_text("That token was not recognised.")
        page.fill("#token-input", TOKENS["alice"])
        page.click("button[type=submit]")
        expect(page.locator(".kpi").first).to_be_visible()
        expect(page.locator(".topbar")).to_contain_text("Alice Morgan")
        page.reload()  # the session survives a reload (sessionStorage)
        expect(page.locator(".kpi").first).to_be_visible()
        page.click("text=Sign out")
        expect(page.locator("#token-input")).to_be_visible()
        page.reload()
        expect(page.locator("#token-input")).to_be_visible()  # and signing out really forgot the token

    def test_a_revoked_token_returns_to_the_login_screen_with_an_explanation(self):
        status, created = api(TOKENS["prem"], "POST", "/users", {"username": "temp1", "display_name": "Temp One"})
        self.assertEqual(status, 201)
        page = self.open()
        page.evaluate("t => sessionStorage.setItem('zpm_token', t)", created["token"])
        page.reload()
        expect(page.locator(".kpi").first).to_be_visible()
        uid = created["user"]["id"]
        api(TOKENS["prem"], "PATCH", "/users/%d" % uid, {"active": False})
        page.evaluate("location.hash = '#/mine'")
        expect(page.locator(".error-text")).to_contain_text("session has ended")


class PageTests(UiCase):
    def test_every_page_renders_cleanly_for_an_admin(self):
        page = self.open("prem")
        routes = ["#/dashboard", "#/mine", "#/team", "#/users"]
        for key in ("WEB", "APP", "INT"):
            routes += ["#/p/%s/%s" % (key, tab) for tab in ("board", "list", "timeline", "milestones", "reports", "activity", "settings")]
        for r in routes:
            page.evaluate("h => location.hash = h", r)
            self.settle(page)
            expect(page.locator("#view h1").first).to_be_visible()
            self.assert_clean_text(page, r)

    def test_manager_pages_hide_admin_features_and_show_no_stray_text(self):
        # Regression: Element.append(null) used to print the word "null" on pages with optional blocks.
        page = self.open("alice", "#/p/WEB/settings")
        self.settle(page)
        self.assert_clean_text(page, "settings (manager)")
        expect(page.get_by_text("Danger zone")).to_have_count(0)
        expect(page.get_by_text("Users & access")).to_have_count(0)
        expect(page.get_by_text("Import tasks from CSV")).to_be_visible()
        page.evaluate("location.hash = '#/p/WEB/reports'")
        expect(page.locator("svg.chart").first).to_be_visible()
        self.assert_clean_text(page, "reports (manager)")
        page.evaluate("location.hash = '#/p/WEB/board'")
        page.get_by_text("Homepage layout").click()
        expect(page.locator(".modal")).to_be_visible()
        self.settle(page)
        self.assert_clean_text(page, "task dialog")

    def test_dashboard_tells_the_demo_story(self):
        page = self.open("alice")
        rows = page.locator("table.table tbody tr")
        expect(rows).to_have_count(3)
        expect(rows.nth(0)).to_contain_text("Mobile App v2")
        expect(rows.nth(0)).to_contain_text("Off track")
        expect(rows.nth(1)).to_contain_text("At risk")
        expect(rows.nth(2)).to_contain_text("On track")
        expect(page.get_by_text("Needs attention")).to_be_visible()
        expect(page.locator(".util-cell .overdue-text")).to_contain_text("43h / 40h")  # carol is overallocated

    def test_deep_link_to_a_task_opens_its_dialog_over_the_project(self):
        page = self.open("alice", "#/t/WEB-5")
        expect(page.locator(".modal")).to_contain_text("WEB-5")
        expect(page.locator(".modal .title-input")).to_have_value("Homepage layout")
        expect(page.locator("#view h1")).to_have_text("Website Redesign")
        page.keyboard.press("Escape")
        expect(page.locator(".modal")).to_have_count(0)

    def test_global_search_finds_and_opens_a_task(self):
        page = self.open("bob")
        page.fill("input[aria-label='Search tasks']", "accessib")
        page.locator(".search-results button", has_text="WEB-7").click()
        expect(page.locator(".modal")).to_contain_text("Accessibility pass")

    def test_layout_stays_inside_a_phone_sized_viewport(self):
        page = self.open("alice", viewport={"width": 390, "height": 800})
        for r in ("#/dashboard", "#/p/WEB/board", "#/p/WEB/list", "#/p/WEB/reports"):
            page.evaluate("h => location.hash = h", r)
            self.settle(page)
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            self.assertLessEqual(overflow, 1, "horizontal page overflow on %s" % r)

    def test_dark_mode_renders(self):
        page = self.open("alice", color_scheme="dark")
        expect(page.locator(".kpi").first).to_be_visible()
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        self.assertNotEqual(bg, "rgb(244, 245, 248)")


class RoleTests(UiCase):
    def test_a_viewer_gets_a_read_only_interface(self):
        page = self.open("erin", "#/p/WEB/board")
        self.settle(page)
        expect(page.get_by_text("+ New task")).to_have_count(0)
        expect(page.get_by_text("+ New project")).to_have_count(0)
        expect(page.locator(".task-card").first).not_to_have_attribute("draggable", "true")
        page.get_by_text("Homepage layout").click()
        expect(page.locator(".modal")).to_contain_text("read-only access")
        for sel in page.locator(".modal select").all():
            expect(sel).to_be_disabled()
        expect(page.locator(".modal textarea[aria-label='New comment']")).to_have_count(0)

    def test_a_member_cannot_edit_a_project_they_do_not_belong_to(self):
        page = self.open("bob", "#/p/APP/board")  # bob is on WEB and INT but not APP
        self.settle(page)
        expect(page.get_by_text("+ New task")).to_have_count(0)
        page.get_by_text("Offline sync engine").click()
        expect(page.locator(".modal")).to_contain_text("read-only access")


class WorkflowTests(UiCase):
    def test_drag_and_drop_moves_a_task_and_the_server_enforces_the_rules(self):
        page = self.open("alice", "#/p/WEB/board")
        self.settle(page)
        card = page.locator(".task-card", has_text="Performance budget")
        card.drag_to(page.locator("section.column[data-status=in_progress]"))
        expect(page.locator(".toast.ok")).to_contain_text("WEB-9 moved to In progress")
        self.assertEqual(api(TOKENS["alice"], "GET", "/tasks/WEB-9")[1]["status"], "in_progress")
        expect(page.locator("section.column[data-status=in_progress] .task-card", has_text="Performance budget")).to_have_count(1)
        # WEB-6 waits on WEB-4, which is still open: the server must refuse and the UI must say why
        page.locator(".task-card", has_text="Responsive navigation").drag_to(page.locator("section.column[data-status=in_progress]"))
        expect(page.locator(".toast.error")).to_contain_text("blocked by unfinished task(s): WEB-4")
        self.assertEqual(api(TOKENS["alice"], "GET", "/tasks/WEB-6")[1]["status"], "todo")
        expect(page.locator("section.column[data-status=todo] .task-card", has_text="Responsive navigation")).to_have_count(1)

    def test_create_edit_comment_log_time_and_link_a_task_through_the_dialogs(self):
        page = self.open("alice", "#/p/INT/board")
        self.settle(page)
        page.get_by_role("button", name="+ New task").click()
        create = page.locator(".modal")
        create.get_by_label("Title").fill("Write the E2E story")
        create.get_by_label("Priority").select_option("high")
        create.get_by_label("Assignee").select_option("bob")
        create.get_by_label("Estimate (hours)").fill("3")
        create.get_by_role("button", name="Create task").click()
        expect(page.locator(".toast.ok")).to_contain_text("INT-5 created")
        card = page.locator(".task-card", has_text="Write the E2E story")
        expect(card).to_be_visible()
        card.focus()
        page.keyboard.press("Enter")  # keyboard users can open cards too
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("INT-5")
        dialog.get_by_label("Priority").select_option("critical")
        expect(dialog.get_by_label("Priority")).to_have_value("critical")
        dialog.get_by_label("New comment").fill("Heads up @bob, this is ready")
        dialog.get_by_role("button", name="Comment").click()
        expect(dialog.locator(".comment .body")).to_have_text("Heads up @bob, this is ready")
        dialog.get_by_label("Hours", exact=True).fill("1.5")
        dialog.get_by_role("button", name="Log time").click()
        expect(dialog).to_contain_text("1.5h logged of 3h estimated")
        dialog.get_by_label("Related task key").fill("INT-1")
        dialog.get_by_role("button", name="Link").click()
        expect(dialog.locator(".section", has_text="Dependencies").locator(".item", has_text="INT-1")).to_be_visible()
        dialog.get_by_label("Status").select_option("blocked")  # allowed from todo, regardless of links
        expect(dialog.get_by_label("Status")).to_have_value("blocked")
        page.keyboard.press("Escape")
        status, task = api(TOKENS["alice"], "GET", "/tasks/INT-5")
        self.assertEqual((task["priority"], task["assignee"]["username"], task["logged_hours"], task["status"]), ("critical", "bob", 1.5, "blocked"))
        self.assertEqual([b["key"] for b in task["blocked_by"]], ["INT-1"])
        self.assertEqual(api(TOKENS["alice"], "GET", "/tasks/INT-5/comments")[1]["items"][0]["body"], "Heads up @bob, this is ready")

    def test_rejected_edits_explain_themselves_and_leave_the_task_unchanged(self):
        page = self.open("alice", "#/p/WEB/board")
        page.locator(".task-card", has_text="Design system").click()
        dialog = page.locator(".modal")
        # an epic with open subtasks cannot be completed - 'done' is offered, the server says no
        dialog.get_by_label("Status").select_option("done")
        expect(page.locator(".toast.error")).to_contain_text("open subtask")
        expect(dialog.get_by_label("Status")).to_have_value("todo")

    def test_csv_export_downloads_a_real_file(self):
        page = self.open("alice", "#/p/WEB/list")
        self.settle(page)
        with page.expect_download() as info:
            page.get_by_role("button", name="Export CSV").click()
        path = info.value.path()
        content = open(path, encoding="utf-8-sig").read()
        self.assertTrue(content.startswith("key,title,type,status"))
        self.assertIn("Homepage layout", content)
        self.assertEqual(info.value.suggested_filename, "WEB-tasks.csv")

    def test_csv_import_dry_run_then_real_import_from_the_settings_tab(self):
        page = self.open("alice", "#/p/INT/settings")
        csv_bytes = b"Issue key,Summary,Priority,Due Date\nEXT-1,Imported from the UI,Highest,30-Dec-26\nEXT-2,,Low,\n"
        page.set_input_files("input[type=file]", {"name": "tasks.csv", "mimeType": "text/csv", "buffer": csv_bytes})
        page.get_by_role("button", name="Run import").click()
        expect(page.get_by_text("Dry run \u2014 nothing was saved.")).to_be_visible()
        expect(page.get_by_text("1 would be created")).to_be_visible()
        expect(page.get_by_text("Row 3:")).to_be_visible()  # the row with an empty title is reported
        self.assertEqual(api(TOKENS["alice"], "GET", "/tasks?external_ref=EXT-1")[1]["total"], 0)
        page.get_by_label("Dry run first").uncheck()
        page.get_by_role("button", name="Run import").click()
        expect(page.get_by_text("Import finished.")).to_be_visible()
        [task] = api(TOKENS["alice"], "GET", "/tasks?external_ref=EXT-1")[1]["items"]
        self.assertEqual((task["title"], task["priority"], task["due_date"]), ("Imported from the UI", "critical", "2026-12-30"))

    def test_admin_creates_a_user_and_sees_the_token_exactly_once(self):
        page = self.open("prem", "#/users")
        page.get_by_role("button", name="+ New user").click()
        dialog = page.locator(".modal")
        dialog.get_by_label("Username").fill("e2e-dana")
        dialog.get_by_label("Display name").fill("Dana E2E")
        dialog.get_by_label("Role").select_option("manager")
        dialog.get_by_role("button", name="Create user").click()
        token_box = page.locator(".token-box")
        expect(token_box).to_contain_text("zpm_")
        token = token_box.inner_text().strip()
        page.get_by_role("button", name="Done").click()
        status, me = api(token, "GET", "/me")
        self.assertEqual((status, me["role"], me["username"]), (200, "manager", "e2e-dana"))
        expect(page.locator("table.table")).to_contain_text("Dana E2E")
        self.assertEqual(page.locator(".token-box").count(), 0)  # gone, and nowhere on the page


class SafetyTests(UiCase):
    def test_hostile_content_is_displayed_as_text_never_executed(self):
        payload_title = '<img src=x onerror="window.__pwned=1">'
        status, task = api(TOKENS["prem"], "POST", "/projects/WEB/tasks", {"title": payload_title, "description": "<script>window.__pwned=2</script>", "labels": ["x"]})
        self.assertEqual(status, 201, task)
        key = task["key"]
        api(TOKENS["prem"], "POST", "/tasks/%s/comments" % key, {"body": "<svg onload=window.__pwned=3> <a href='javascript:window.__pwned=4'>x</a>"})
        status, proj = api(TOKENS["prem"], "POST", "/projects", {"key": "XSS", "name": "<b onmouseover=window.__pwned=5>Evil</b>", "description": "<iframe src=javascript:window.__pwned=6>"})
        self.assertEqual(status, 201, proj)
        page = self.open("prem")
        for r in ("#/dashboard", "#/p/WEB/board", "#/p/WEB/list", "#/p/WEB/activity", "#/p/XSS/board", "#/mine", "#/p/WEB/timeline"):
            page.evaluate("h => location.hash = h", r)
            self.settle(page)
        page.evaluate("h => location.hash = h", "#/t/" + key)
        expect(page.locator(".modal")).to_contain_text(key)
        self.settle(page)
        expect(page.locator(".modal .comment .body")).to_contain_text("<svg onload=window.__pwned=3>")  # literal text
        expect(page.locator(".modal textarea[aria-label='Description']")).to_have_value("<script>window.__pwned=2</script>")
        self.assertEqual(page.evaluate("typeof window.__pwned"), "undefined")
        self.assertEqual(page.locator(".modal img, .modal svg[onload], .modal iframe, #view iframe").count(), 0)
        page.evaluate("h => location.hash = h", "#/p/WEB/board")
        self.settle(page)
        expect(page.locator(".task-card", has_text="<img src=x")).to_be_visible()
        self.assertEqual(page.evaluate("typeof window.__pwned"), "undefined")
        api(TOKENS["prem"], "DELETE", "/tasks/%s" % key)
        api(TOKENS["prem"], "DELETE", "/projects/XSS?confirm=XSS")
