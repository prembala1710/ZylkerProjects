"""Browser end-to-end tests for the Webhooks admin page (Playwright + Chromium; skipped when unavailable).

They run the real threaded server *and* a real delivery worker against a local receiver, so a click on
"Send test" goes UI -> API -> queue -> worker -> signed HTTP request -> receiver and back into the delivery log.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

from webhook_helpers import Receiver  # noqa: E402

from zylker_pm.app import App  # noqa: E402
from zylker_pm.cli import make_threaded_server  # noqa: E402
from zylker_pm.config import Config  # noqa: E402
from zylker_pm.db import connect, init_db  # noqa: E402
from zylker_pm.seed import seed_demo  # noqa: E402
from zylker_pm.services import users, webhooks  # noqa: E402
from zylker_pm.webhook_worker import WebhookWorker  # noqa: E402

JUNK = re.compile(r"\bnull\b|\bundefined\b|\bNaN\b|\[object")


def setUpModule():
    global PW, BROWSER, SERVER, BASE, TOKENS, TMP, WORKER
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
    cfg = Config(db_path=os.path.join(TMP, "e2e.db"), webhook_allow_private=True)
    init_db(cfg.db_path)
    app = App(cfg)
    with app.session() as ctx:
        _, admin = users.create_user(ctx, {"username": "prem", "display_name": "Prem B", "role": "admin"})
    TOKENS = {"prem": admin, **seed_demo(app)}
    SERVER = make_threaded_server(app, "127.0.0.1", 0)
    BASE = "http://127.0.0.1:%d" % SERVER.server_port
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    WORKER = WebhookWorker(cfg, concurrency=2, poll_interval=0.1)
    WORKER.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:  # the page warns when no worker has checked in, so wait for the first heartbeat
        conn = connect(cfg.db_path)
        beat = conn.execute("SELECT 1 FROM webhook_state WHERE key = 'worker_heartbeat'").fetchone()
        conn.close()
        if beat:
            break
        time.sleep(0.05)


def tearDownModule():
    if sync_playwright is None or "SERVER" not in globals():
        return
    WORKER.stop()
    SERVER.shutdown()
    SERVER.server_close()
    BROWSER.close()
    PW.stop()
    shutil.rmtree(TMP, True)


def api(token, method, path, body=None, base=None):
    req = urllib.request.Request((base or BASE) + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    return predicate()


class WebhookUi(unittest.TestCase):
    viewport = {"width": 1360, "height": 900}

    def setUp(self):
        self.problems, self.contexts = [], []

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def receiver(self, **kwargs):
        r = Receiver(**kwargs)
        self.addCleanup(r.close)
        return r

    def make_hook(self, receiver, name, **fields):
        status, body = api(TOKENS["prem"], "POST", "/webhooks", {"name": name, "url": receiver.url, "events": ["*"], **fields})
        self.assertEqual(status, 201, body)
        return body

    def hooks(self):
        return {w["name"]: w for w in api(TOKENS["prem"], "GET", "/webhooks")[1]["items"]}

    def seen(self, receiver, event):
        return wait_for(lambda: next((r for r in receiver.requests if r["headers"].get("X-Zylker-Event") == event), None))

    def open(self, user="prem", hash_="#/webhooks", viewport=None, base=None, token=None):
        context = BROWSER.new_context(viewport=viewport or self.viewport)
        self.contexts.append(context)
        context.add_init_script("sessionStorage.setItem('zpm_token', %s)" % json.dumps(token or TOKENS[user]))
        page = context.new_page()

        def on_console(m):
            if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource"):
                self.problems.append("console.%s: %s" % (m.type, m.text))

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.on("response", lambda r: self.problems.append("HTTP %d from %s" % (r.status, r.url)) if r.status >= 500 else None)
        page.goto((base or BASE) + "/" + hash_)
        page.wait_for_selector(".content")
        return page

    def row(self, page, name):
        return page.locator("#view table.table tbody tr", has_text=name)

    def no_junk(self, page, where):
        match = JUNK.search(page.inner_text("#view"))
        self.assertIsNone(match, "stray %r on %s" % (match and match.group(), where))


class WebhookLifecycleTests(WebhookUi):
    def test_create_test_and_inspect_deliveries(self):
        receiver = self.receiver()
        page = self.open()
        expect(page.locator("#view h1")).to_have_text("Webhooks")
        expect(page.locator(".notice")).to_have_count(0)  # a worker is running, so no warning
        page.get_by_role("button", name="+ New webhook").click()
        page.get_by_label("Name", exact=True).fill("Build server")
        page.get_by_label("Payload URL").fill(receiver.url)
        page.get_by_role("button", name="Create webhook").click()
        secret = page.locator(".modal .token-box").inner_text()
        self.assertTrue(secret.startswith("whsec_"), secret)
        page.get_by_role("button", name="Done").click()
        row = self.row(page, "Build server")
        expect(row).to_contain_text("All events")
        expect(row).to_contain_text("No deliveries yet")
        expect(row).to_contain_text("127.0.0.1:%d/hooks/zylker" % receiver.port)  # host and path only...
        self.assertNotIn("key=abc", page.inner_text("body"))  # ...never the query string, which often carries a key
        self.no_junk(page, "webhooks list")

        row.get_by_role("button", name="Send test").click()
        expect(page.locator(".toast.ok", has_text="Test delivered: HTTP 200")).to_be_visible(timeout=10000)
        ping = self.seen(receiver, "webhook.ping")
        self.assertTrue(webhooks.verify(secret, ping["headers"]["X-Zylker-Timestamp"], ping["raw"], ping["headers"]["X-Zylker-Signature"]))

        status, _ = api(TOKENS["alice"], "POST", "/projects/WEB/tasks", {"title": "Webhook e2e task"})
        self.assertEqual(status, 201)
        created = self.seen(receiver, "task.created")
        self.assertEqual(json.loads(created["raw"])["task"]["title"], "Webhook e2e task")
        page.reload()
        row = self.row(page, "Build server")
        expect(row).to_contain_text("Healthy")
        expect(row).to_contain_text("just now")

        row.get_by_role("button", name="Deliveries").click()
        dialog = page.locator(".modal")
        expect(dialog).to_contain_text("Deliveries \u2014 Build server")
        rows = dialog.locator("tbody tr")
        expect(rows).to_have_count(2)
        expect(rows.filter(has_text="webhook.ping")).to_contain_text("Delivered")
        task_row = rows.filter(has_text="task.created")
        expect(task_row).to_contain_text("HTTP 200")
        expect(task_row).to_contain_text("1/6")
        task_row.get_by_role("button", name="Details").click()
        detail = page.locator(".modal").last
        expect(detail).to_contain_text("Delivery #")
        expect(detail.locator("pre.code")).to_contain_text('"event": "task.created"')
        expect(detail.locator("pre.code")).to_contain_text("Webhook e2e task")
        page.keyboard.press("Escape")  # closes the detail first...
        expect(page.locator(".modal")).to_have_count(1)
        page.keyboard.press("Escape")  # ...then the list
        expect(page.locator(".modal")).to_have_count(0)

    def test_event_and_project_choices_are_saved_and_shown_again(self):
        receiver = self.receiver()
        page = self.open()
        page.get_by_role("button", name="+ New webhook").click()
        page.get_by_label("Name", exact=True).fill("Comment watcher")
        page.get_by_label("Payload URL").fill(receiver.url)
        page.get_by_label("All events").uncheck()
        page.get_by_role("button", name="Create webhook").click()
        expect(page.locator(".toast.error", has_text="Choose at least one event")).to_be_visible()
        page.get_by_label("Comments \u2014 everything").check()
        page.get_by_label("All projects").uncheck()
        page.get_by_role("button", name="Create webhook").click()
        expect(page.locator(".toast.error", has_text="Choose at least one project")).to_be_visible()
        page.locator(".check-row", has_text="Website Redesign").locator("input").check()
        page.get_by_role("button", name="Create webhook").click()
        expect(page.locator(".modal .token-box")).to_be_visible()
        page.get_by_role("button", name="Done").click()
        saved = self.hooks()["Comment watcher"]
        self.assertEqual((saved["events"], saved["projects"]), (["comment.*"], ["WEB"]))

        row = self.row(page, "Comment watcher")
        expect(row).to_contain_text("1 subscription")
        expect(row).to_contain_text("WEB")
        row.get_by_role("button", name="Edit").click()
        expect(page.locator(".modal")).to_contain_text("Edit Comment watcher")
        expect(page.get_by_label("All events")).not_to_be_checked()
        expect(page.get_by_label("Comments \u2014 everything")).to_be_checked()
        expect(page.get_by_label("All projects")).not_to_be_checked()
        expect(page.locator(".check-row", has_text="Website Redesign").locator("input")).to_be_checked()
        expect(page.locator(".check-row", has_text="Mobile App v2").locator("input")).not_to_be_checked()
        page.get_by_label("Name", exact=True).fill("Everything watcher")
        page.get_by_label("All events").check()
        page.get_by_role("button", name="Save changes").click()
        expect(page.locator(".toast.ok", has_text="Webhook saved")).to_be_visible()
        saved = self.hooks()["Everything watcher"]
        self.assertEqual((saved["events"], saved["projects"]), (["*"], ["WEB"]))
        expect(self.row(page, "Everything watcher")).to_contain_text("All events")

    def test_the_server_refuses_a_bad_url_and_the_dialog_says_why(self):
        page = self.open()
        page.get_by_role("button", name="+ New webhook").click()
        page.get_by_label("Name", exact=True).fill("Broken")
        page.get_by_label("Payload URL").fill("ftp://example.com/hook")
        page.get_by_role("button", name="Create webhook").click()
        expect(page.locator(".toast.error", has_text="must start with http")).to_be_visible()
        expect(page.locator(".modal")).to_be_visible()  # still open, nothing was lost
        page.get_by_label("Payload URL").fill("http://169.254.169.254/latest")
        page.get_by_role("button", name="Create webhook").click()
        # this server allows private targets (the receivers below are on loopback), so only the scheme was refused above
        expect(page.locator(".modal .token-box")).to_be_visible()
        page.get_by_role("button", name="Done").click()
        self.assertIn("Broken", self.hooks())

    def test_pause_resume_rotate_and_delete(self):
        receiver = self.receiver()
        hook = self.make_hook(receiver, "Lifecycle")
        page = self.open()
        row = self.row(page, "Lifecycle")
        row.get_by_role("button", name="Pause").click()
        expect(row).to_contain_text("Paused")
        self.assertFalse(self.hooks()["Lifecycle"]["active"])
        row.get_by_role("button", name="Resume").click()
        expect(row).not_to_contain_text("Paused")
        self.assertTrue(self.hooks()["Lifecycle"]["active"])

        row.get_by_role("button", name="Edit").click()
        page.get_by_role("button", name="Rotate\u2026").click()
        page.locator(".modal", has_text="Please confirm").get_by_role("button", name="Rotate secret").click()
        new_secret = page.locator(".modal .token-box").inner_text()
        self.assertTrue(new_secret.startswith("whsec_") and new_secret != hook["secret"])
        page.get_by_role("button", name="Done").click()
        self.assertEqual(self.hooks()["Lifecycle"]["secret_hint"], "\u2026" + new_secret[-4:])

        row = self.row(page, "Lifecycle")
        row.get_by_role("button", name="Edit").click()
        page.get_by_role("button", name="Delete\u2026").click()
        page.locator(".modal", has_text="Please confirm").get_by_role("button", name="Delete webhook").click()
        expect(page.locator(".toast.ok", has_text="Webhook deleted")).to_be_visible()
        expect(self.row(page, "Lifecycle")).to_have_count(0)
        self.assertNotIn("Lifecycle", self.hooks())

    def test_failures_are_visible_and_a_failed_delivery_can_be_sent_again(self):
        receiver = self.receiver(status=500, body=b"<script>window.__pwned=1</script> upstream is down")
        self.make_hook(receiver, "Flaky")
        page = self.open()
        row = self.row(page, "Flaky")
        row.get_by_role("button", name="Send test").click()
        expect(page.locator(".toast.error", has_text="Test failed: HTTP 500")).to_be_visible(timeout=10000)
        for title in ("one", "two"):
            self.assertEqual(api(TOKENS["alice"], "POST", "/projects/WEB/tasks", {"title": "Flaky " + title})[0], 201)
        wait_for(lambda: self.hooks()["Flaky"]["health"]["consecutive_failures"] >= 3)
        page.reload()
        row = self.row(page, "Flaky")
        expect(row).to_contain_text("Failing \u00d73")
        self.no_junk(page, "failing webhook")

        row.get_by_role("button", name="Deliveries").click()
        dialog = page.locator(".modal")
        ping = dialog.locator("tbody tr").filter(has_text="webhook.ping")
        expect(ping).to_contain_text("Failed")
        expect(ping).to_contain_text("HTTP 500")
        expect(dialog.locator("tbody tr").filter(has_text="task.created").first).to_contain_text("Queued")  # waiting for its retry
        ping.get_by_role("button", name="Details").click()
        detail = page.locator(".modal").last
        expect(detail).to_contain_text("<script>window.__pwned=1</script> upstream is down")  # shown as text...
        self.assertIsNone(page.evaluate("window.__pwned"))  # ...never executed
        page.keyboard.press("Escape")

        receiver.status, receiver.body = 200, b"ok"  # the receiver recovers
        ping.get_by_role("button", name="Redeliver").click()
        expect(page.locator(".toast.ok", has_text="Queued again")).to_be_visible()
        for _ in range(20):
            dialog.get_by_role("button", name="Refresh").click()
            if dialog.locator("tbody tr").filter(has_text="redelivery of").filter(has_text="Delivered").count():
                break
            time.sleep(0.25)
        expect(dialog.locator("tbody tr").filter(has_text="redelivery of").filter(has_text="Delivered")).to_have_count(1)

    def test_names_are_text_not_markup(self):
        receiver = self.receiver()
        nasty = '<img src=x onerror="window.__pwned=1">'
        self.make_hook(receiver, nasty)
        page = self.open()
        expect(page.locator("#view table.table tbody")).to_contain_text("<img src=x")
        self.assertIsNone(page.evaluate("window.__pwned"))
        expect(page.locator("#view table.table img")).to_have_count(0)


class WebhookAccessTests(WebhookUi):
    def test_only_admins_see_the_page(self):
        page = self.open("alice", "#/dashboard")
        expect(page.locator("a.nav-link", has_text="Webhooks")).to_have_count(0)
        page.evaluate("location.hash = '#/webhooks'")
        expect(page.locator("#view h1")).to_have_text("Dashboard")
        expect(page.get_by_role("button", name="+ New webhook")).to_have_count(0)
        admin = self.open("prem", "#/dashboard")
        expect(admin.locator("a.nav-link", has_text="Webhooks")).to_have_count(1)
        admin.locator("a.nav-link", has_text="Webhooks").click()
        expect(admin.locator("#view h1")).to_have_text("Webhooks")
        expect(admin.locator("a.nav-link.active")).to_have_text("Webhooks")

    def test_a_warning_explains_a_missing_worker(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = Config(db_path=os.path.join(tmp, "noworker.db"))
        init_db(cfg.db_path)
        app = App(cfg)
        with app.session() as ctx:
            _, token = users.create_user(ctx, {"username": "root", "display_name": "Root", "role": "admin"})
        server = make_threaded_server(app, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        page = self.open(token=token, base="http://127.0.0.1:%d" % server.server_port)
        notice = page.locator(".notice")
        expect(notice).to_contain_text("The delivery worker is not running")
        expect(notice).to_contain_text("zylker-pm webhooks-worker")
        expect(page.locator("#view .empty")).to_contain_text("No webhooks yet")

    def test_the_page_fits_a_phone(self):
        receiver = self.receiver()
        self.make_hook(receiver, "Phone check")
        page = self.open(viewport={"width": 390, "height": 800})
        expect(self.row(page, "Phone check")).to_be_visible()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)


if __name__ == "__main__":
    unittest.main()
