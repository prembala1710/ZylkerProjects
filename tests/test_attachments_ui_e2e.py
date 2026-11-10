"""Browser end-to-end tests for attachments (Playwright + Chromium; skipped when unavailable).

They drive the real dialog and the Files tab against the real threaded server: picking files, dropping them, pasting a
screenshot, previews, downloads, deletion, the read-only experience and the places where hostile names or files could
do harm. Any console error, CSP violation or uncaught exception fails the test.
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
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

from attachment_helpers import HTML, SVG, make_png  # noqa: E402

from zylker_pm.app import App  # noqa: E402
from zylker_pm.cli import make_threaded_server  # noqa: E402
from zylker_pm.config import Config  # noqa: E402
from zylker_pm.db import init_db  # noqa: E402
from zylker_pm.seed import seed_demo  # noqa: E402
from zylker_pm.services import users  # noqa: E402

JUNK = re.compile(r"\bnull\b|\bundefined\b|\bNaN\b|\[object")
MAX_BYTES = 2048  # small on purpose: the "too big" case needs no big file


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
    cfg = Config(db_path=os.path.join(TMP, "e2e.db"), attachment_max_bytes=MAX_BYTES)
    init_db(cfg.db_path)
    app = App(cfg)
    with app.session() as ctx:
        _, admin = users.create_user(ctx, {"username": "prem", "display_name": "Prem B", "role": "admin"})
    TOKENS = {"prem": admin, **seed_demo(app)}
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


def call(request):
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def api(token, method, path, body=None):
    req = urllib.request.Request(BASE + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return call(req)


def upload(user, key, name, data, content_type="application/octet-stream"):
    url = "%s/api/tasks/%s/attachments?filename=%s" % (BASE, key, urllib.parse.quote(name))
    req = urllib.request.Request(url, method="POST", data=data)
    req.add_header("Authorization", "Bearer " + TOKENS[user])
    req.add_header("Content-Type", content_type)
    return call(req)


PROJECTS = iter(range(1, 1000))


class AttachmentUi(unittest.TestCase):
    viewport = {"width": 1360, "height": 1000}

    def setUp(self):
        self.problems, self.contexts = [], []

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def new_project(self, name, members=("bob", "carol")):
        """A project of its own (keys are unique per call, so tests never see each other's files)."""
        key = "P%d" % next(PROJECTS)
        status, project = api(TOKENS["alice"], "POST", "/projects", {"key": key, "name": name, "members": list(members)})
        self.assertEqual(status, 201, project)
        return key

    def new_task(self, title, project="WEB"):
        status, task = api(TOKENS["alice"], "POST", "/projects/%s/tasks" % project, {"title": title})
        self.assertEqual(status, 201, task)
        return task["key"]

    def open(self, user, hash_, viewport=None, **context_args):
        context = BROWSER.new_context(viewport=viewport or self.viewport, accept_downloads=True, **context_args)
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

    def open_task(self, user, key):
        page = self.open(user, "#/t/" + key)
        expect(page.locator("#attachments")).to_be_visible()
        return page

    def files(self, page):
        return page.locator("#attachments .attachment")

    def fake_drop(self, page, name, data, content_type, events=("dragenter", "dragover", "drop"), target=".modal"):
        """Dispatch drag events carrying a file at ``target``; returns whether each one was claimed (default prevented)."""
        return page.evaluate(
            """({name, bytes, type, events, target}) => {
                const dt = new DataTransfer();
                dt.items.add(new File([new Uint8Array(bytes)], name, { type }));
                const element = document.querySelector(target);
                return events.map((kind) => { const e = new DragEvent(kind, { dataTransfer: dt, bubbles: true, cancelable: true }); element.dispatchEvent(e); return e.defaultPrevented; });
            }""",
            {"name": name, "bytes": list(data), "type": content_type, "events": list(events), "target": target},
        )

    def confirm(self, page, button="Delete"):
        page.locator(".modal", has_text="Please confirm").get_by_role("button", name=button, exact=True).click()


class DialogTests(AttachmentUi):
    def test_choose_files_download_and_delete(self):
        key = self.new_task("Attach things")
        page = self.open_task("alice", key)
        expect(page.locator("#attachments")).to_contain_text("No files yet")
        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name="Attach files").click()
        chooser.value.set_files([
            {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"line one\nline two\n"},
            {"name": "screenshot.png", "mimeType": "image/png", "buffer": make_png(40, 30)},
        ])  # fmt: skip
        expect(self.files(page)).to_have_count(2)
        expect(page.locator(".toast.ok", has_text="2 files attached")).to_be_visible()
        expect(page.locator("#attachments h3")).to_contain_text("Attachments (2 \u00b7")
        first = self.files(page).filter(has_text="notes.txt")
        expect(first).to_contain_text("TXT")
        expect(first).to_contain_text("18 B")
        expect(first).to_contain_text("Alice Morgan")
        expect(page.locator(".section", has_text="History")).to_contain_text("attached \u201cnotes.txt\u201d (18 B) to %s" % key)
        self.no_junk(page)

        with page.expect_download() as download:
            page.get_by_role("button", name="Download notes.txt").click()
        self.assertEqual(download.value.suggested_filename, "notes.txt")
        with open(download.value.path(), "rb") as saved:
            self.assertEqual(saved.read(), b"line one\nline two\n")

        page.get_by_role("button", name="Delete notes.txt").click()
        self.confirm(page)
        expect(self.files(page)).to_have_count(1)
        expect(page.locator(".toast.ok", has_text="Attachment deleted")).to_be_visible()
        expect(page.locator(".section", has_text="History")).to_contain_text("removed the attachment \u201cnotes.txt\u201d")
        status, listing = api(TOKENS["alice"], "GET", "/tasks/%s/attachments" % key)
        self.assertEqual([a["filename"] for a in listing["items"]], ["screenshot.png"])

    def test_only_real_images_get_a_preview_button_and_the_preview_is_an_image(self):
        key = self.new_task("Previews")
        for name, data, ctype in (("real.png", make_png(40, 30), "image/png"), ("fake.png", HTML, "image/png"), ("logo.svg", SVG, "image/svg+xml")):
            self.assertEqual(upload("bob", key, name, data, ctype)[0], 201)
        page = self.open_task("bob", key)
        expect(self.files(page)).to_have_count(3)
        expect(page.get_by_role("button", name="Preview real.png")).to_have_count(1)
        expect(page.get_by_role("button", name="Preview fake.png")).to_have_count(0)  # it only claims to be a picture
        expect(page.get_by_role("button", name="Preview logo.svg")).to_have_count(0)  # svg can carry script
        page.get_by_role("button", name="Preview real.png").click()
        image = page.locator(".preview-img")
        expect(image).to_be_visible()
        width, source = image.evaluate("el => [el.naturalWidth, el.src.slice(0, 22)]")
        self.assertEqual((width, source), (40, "data:image/png;base64,"))
        expect(page.locator(".modal").last).to_contain_text("real.png")
        page.keyboard.press("Escape")
        expect(page.locator(".preview-img")).to_have_count(0)
        expect(page.locator("#attachments")).to_be_visible()  # the task dialog is still there

    def test_files_can_be_dropped_anywhere_in_the_window_while_the_dialog_is_open(self):
        key = self.new_task("Drop target")
        page = self.open_task("bob", key)
        self.assertEqual(self.fake_drop(page, "dropped.txt", b"from the desktop", "text/plain", events=("dragenter", "dragover")), [True, True])
        expect(page.locator("body.dropping")).to_have_count(1)  # the whole window says where the files will go
        self.assertEqual(self.fake_drop(page, "dropped.txt", b"from the desktop", "text/plain", events=("drop",)), [True])
        self.assertFalse(page.evaluate("document.body.classList.contains('dropping')"))  # at once, not when the idle timer would have fired
        expect(self.files(page).filter(has_text="dropped.txt")).to_have_count(1)
        status, listing = api(TOKENS["bob"], "GET", "/tasks/%s/attachments" % key)
        self.assertEqual([(a["filename"], a["size"]) for a in listing["items"]], [("dropped.txt", 16)])

    def test_the_drop_overlay_does_not_get_stuck_when_a_drag_just_stops(self):
        key = self.new_task("Changed my mind")
        page = self.open_task("bob", key)
        self.fake_drop(page, "never-dropped.txt", b"x", "text/plain", events=("dragenter", "dragover"))
        expect(page.locator("body.dropping")).to_have_count(1)
        expect(page.locator("body.dropping")).to_have_count(0, timeout=4000)  # no more dragover events: the drag is over (a dragleave was missed)
        expect(self.files(page)).to_have_count(0)

    def test_the_dimmed_area_around_the_dialog_accepts_files_too(self):
        key = self.new_task("Outside the box")
        page = self.open_task("bob", key)
        self.assertEqual(self.fake_drop(page, "outside.txt", b"dropped next to the dialog", "text/plain", events=("drop",), target=".backdrop"), [True])
        expect(self.files(page).filter(has_text="outside.txt")).to_have_count(1)  # not opened by the browser, not lost
        self.assertEqual(len(page.context.pages), 1)

    def test_a_file_dropped_on_a_dialog_above_the_task_is_neither_attached_nor_opened(self):
        key = self.new_task("Busy screen")
        upload("bob", key, "keep.txt", b"keep me", "text/plain")
        page = self.open_task("bob", key)
        page.get_by_role("button", name="Delete keep.txt").click()  # a confirmation now sits on top of the task dialog
        expect(page.locator(".modal", has_text="Please confirm")).to_be_visible()
        claimed = self.fake_drop(page, "stray.txt", b"not for the task", "text/plain", target=".backdrop:last-of-type")
        self.assertEqual(claimed, [True, True, True])  # swallowed: the browser must not navigate away
        expect(page.locator("body.dropping")).to_have_count(0)
        self.confirm(page, "Cancel")
        expect(self.files(page)).to_have_count(1)
        self.assertEqual([a["filename"] for a in api(TOKENS["bob"], "GET", "/tasks/%s/attachments" % key)[1]["items"]], ["keep.txt"])

    def test_once_the_dialog_is_closed_the_page_stops_claiming_file_drags(self):
        key = self.new_task("Closed again")
        page = self.open_task("bob", key)
        page.keyboard.press("Escape")
        expect(page.locator(".modal")).to_have_count(0)
        self.assertEqual(self.fake_drop(page, "late.txt", b"x", "text/plain", target="body"), [False, False, False])
        expect(page.locator("body.dropping")).to_have_count(0)

    def test_dragging_something_that_is_not_a_file_does_nothing(self):
        key = self.new_task("Not a file")
        page = self.open_task("bob", key)
        page.evaluate(
            """() => {
                const dt = new DataTransfer();
                dt.setData('text/plain', 'WEB-1');
                const target = document.querySelector('.modal');
                window.__dropPrevented = ['dragenter', 'dragover', 'drop'].map((k) => { const e = new DragEvent(k, { dataTransfer: dt, bubbles: true, cancelable: true }); target.dispatchEvent(e); return e.defaultPrevented; });
            }"""
        )
        self.assertEqual(page.evaluate("window.__dropPrevented"), [False, False, False])
        expect(page.locator("body.dropping")).to_have_count(0)
        expect(self.files(page)).to_have_count(0)

    def test_a_pasted_screenshot_is_attached_and_pasted_text_is_left_alone(self):
        key = self.new_task("Paste a screenshot")
        page = self.open_task("bob", key)
        comment = page.get_by_label("New comment")
        comment.focus()
        prevented_for_text = page.evaluate(
            """() => {
                const dt = new DataTransfer();
                dt.setData('text/plain', 'just words');
                const e = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
                document.activeElement.dispatchEvent(e);
                return e.defaultPrevented;
            }"""
        )
        self.assertFalse(prevented_for_text)
        expect(self.files(page)).to_have_count(0)
        page.evaluate(
            """(bytes) => {
                const dt = new DataTransfer();
                dt.items.add(new File([new Uint8Array(bytes)], 'image.png', { type: 'image/png' }));
                document.activeElement.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
            }""",
            list(make_png(40, 30)),
        )
        expect(self.files(page)).to_have_count(1)
        expect(self.files(page).first).to_contain_text("PNG")
        name = self.files(page).first.locator(".file-name").inner_text()
        self.assertRegex(name, r"^pasted-\d{8}-\d{6}\.png$")  # "image.png" would be the same every time
        expect(page.get_by_role("button", name="Preview " + name)).to_have_count(1)

    def test_problems_are_explained_in_words(self):
        key = self.new_task("Troubles")
        page = self.open_task("bob", key)
        chooser = page.locator("#attachments input[type=file]")
        chooser.set_input_files({"name": "huge.bin", "mimeType": "application/octet-stream", "buffer": b"x" * 5000})
        expect(page.locator(".toast.error", has_text="huge.bin is 4.9 KB; files can be at most 2 KB.")).to_be_visible()
        chooser.set_input_files({"name": "empty.txt", "mimeType": "text/plain", "buffer": b""})
        expect(page.locator(".toast.error", has_text="empty.txt is empty")).to_be_visible()
        self.assertEqual(api(TOKENS["bob"], "GET", "/tasks/%s/attachments" % key)[1]["items"], [])
        for i in range(25):
            self.assertEqual(upload("carol", key, "f%02d.txt" % i, b"x")[0], 201)
        page.reload()
        page.evaluate("k => { location.hash = '#/t/' + k; }", key)  # a reload closes the dialog; open it again
        expect(self.files(page)).to_have_count(25)
        page.locator("#attachments input[type=file]").set_input_files({"name": "26th.txt", "mimeType": "text/plain", "buffer": b"x"})
        expect(page.locator(".toast.error", has_text="26th.txt: %s already has 25 attachments" % key)).to_be_visible()

    def test_progress_is_shown_and_a_second_upload_waits_for_the_first(self):
        key = self.new_task("Slow link")
        page = self.open_task("bob", key)
        held = []
        page.route("**/api/tasks/*/attachments?*", lambda route: held.append(route))  # park the upload until the test lets it go
        page.locator("#attachments input[type=file]").set_input_files({"name": "slow.txt", "mimeType": "text/plain", "buffer": b"slow"})
        status = page.locator("#attachments [role=status]")
        expect(status).to_have_text("Uploading 1 of 1: slow.txt\u2026")
        expect(page.get_by_role("button", name="Attach files")).to_be_disabled()
        self.fake_drop(page, "impatient.txt", b"too soon", "text/plain")
        expect(page.locator(".toast.error", has_text="Wait for the current upload to finish")).to_be_visible()
        self.assertEqual(len(held), 1)  # the second file was never sent
        held[0].continue_()
        expect(self.files(page)).to_have_count(1)
        expect(page.get_by_role("button", name="Attach files")).to_be_enabled()
        expect(status).to_have_text("")
        self.assertEqual([a["filename"] for a in api(TOKENS["bob"], "GET", "/tasks/%s/attachments" % key)[1]["items"]], ["slow.txt"])

    def test_several_files_where_one_fails_still_attach_the_others(self):
        key = self.new_task("Mixed bag")
        page = self.open_task("bob", key)
        page.locator("#attachments input[type=file]").set_input_files([
            {"name": "ok-1.txt", "mimeType": "text/plain", "buffer": b"1"},
            {"name": "too-big.bin", "mimeType": "application/octet-stream", "buffer": b"x" * 3000},
            {"name": "ok-2.txt", "mimeType": "text/plain", "buffer": b"2"},
        ])  # fmt: skip
        expect(self.files(page)).to_have_count(2)
        expect(page.locator(".toast.error", has_text="too-big.bin")).to_be_visible()
        expect(page.locator(".toast.ok", has_text="2 files attached")).to_be_visible()

    def test_names_are_text_and_html_files_are_downloaded_never_rendered(self):
        key = self.new_task("Hostile files")
        nasty = '<img src=x onerror="window.__pwned=1">.html'
        self.assertEqual(upload("bob", key, nasty, HTML, "text/html")[0], 201)
        page = self.open_task("bob", key)
        expect(self.files(page).first.locator(".file-name")).to_have_text(nasty)  # shown as text, character for character
        self.assertIsNone(page.evaluate("window.__pwned"))
        expect(page.locator("#attachments img")).to_have_count(0)
        before = page.url
        page.evaluate(
            """() => { window.__blobTypes = []; const make = URL.createObjectURL; URL.createObjectURL = (blob) => { window.__blobTypes.push(blob.type); return make.call(URL, blob); }; }"""
        )
        with page.expect_download() as download:
            page.get_by_role("button", name="Download " + nasty).click()
        with open(download.value.path(), "rb") as saved:
            self.assertEqual(saved.read(), HTML)
        self.assertEqual(page.evaluate("window.__blobTypes"), ["application/octet-stream"])  # even if a browser ignored "download", it would not render this
        self.assertEqual(page.url, before)  # the app did not navigate to the file
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertEqual(len(page.context.pages), 1)  # nor did it open a tab

    def test_a_long_name_does_not_break_the_layout_on_a_phone(self):
        key = self.new_task("Long names")
        long_name = ("a-very-long-file-name-" * 8) + ".pdf"
        self.assertEqual(upload("bob", key, long_name, b"%PDF-1.7", "application/pdf")[0], 201)
        page = self.open("bob", "#/t/" + key, viewport={"width": 390, "height": 800})
        expect(self.files(page)).to_have_count(1)
        self.assertLessEqual(page.evaluate("document.querySelector('.modal').scrollWidth - document.querySelector('.modal').clientWidth"), 1)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)

    def no_junk(self, page):
        match = JUNK.search(page.inner_text(".modal"))
        self.assertIsNone(match, "stray %r in the task dialog" % (match and match.group()))


class ReadOnlyTests(AttachmentUi):
    def test_a_viewer_can_read_and_download_but_not_change_anything(self):
        key = self.new_task("Read only")
        upload("bob", key, "spec.txt", b"the spec", "text/plain")
        page = self.open_task("erin", key)
        expect(self.files(page)).to_have_count(1)
        expect(page.get_by_role("button", name="Attach files")).to_have_count(0)
        expect(page.get_by_role("button", name="Delete spec.txt")).to_have_count(0)
        expect(page.locator("#attachments")).not_to_contain_text("Drop them")
        with page.expect_download() as download:
            page.get_by_role("button", name="Download spec.txt").click()
        with open(download.value.path(), "rb") as saved:
            self.assertEqual(saved.read(), b"the spec")
        before = page.url
        self.fake_drop(page, "sneaky.txt", b"nope", "text/plain")
        expect(page.locator(".toast.error", has_text="read-only access")).to_be_visible()
        self.assertEqual((page.url, len(self.files(page).all())), (before, 1))

    def test_a_member_of_another_project_sees_the_same_read_only_view(self):
        status, task = api(TOKENS["alice"], "POST", "/projects/APP/tasks", {"title": "Someone else's task"})
        key = task["key"]
        upload("alice", key, "plan.txt", b"p", "text/plain")
        page = self.open_task("bob", key)  # bob is not a member of APP
        expect(self.files(page)).to_have_count(1)
        expect(page.get_by_role("button", name="Attach files")).to_have_count(0)
        expect(page.get_by_role("button", name="Delete plan.txt")).to_have_count(0)

    def test_members_delete_only_their_own_files_and_managers_any(self):
        key = self.new_task("Who deleted what")
        upload("bob", key, "bobs.txt", b"1", "text/plain")
        upload("carol", key, "carols.txt", b"2", "text/plain")
        page = self.open_task("bob", key)
        expect(page.get_by_role("button", name="Delete bobs.txt")).to_have_count(1)
        expect(page.get_by_role("button", name="Delete carols.txt")).to_have_count(0)
        manager = self.open_task("alice", key)
        expect(manager.get_by_role("button", name="Delete bobs.txt")).to_have_count(1)
        expect(manager.get_by_role("button", name="Delete carols.txt")).to_have_count(1)

    def test_an_archived_project_keeps_its_files_but_allows_no_changes(self):
        project = self.new_project("Old news", members=("bob",))
        key = self.new_task("Closed chapter", project=project)
        upload("bob", key, "final.txt", b"done", "text/plain")
        self.assertEqual(api(TOKENS["alice"], "PATCH", "/projects/" + project, {"status": "archived"})[0], 200)
        page = self.open_task("alice", key)
        expect(self.files(page)).to_have_count(1)
        expect(page.get_by_role("button", name="Attach files")).to_have_count(0)
        expect(page.get_by_role("button", name="Delete final.txt")).to_have_count(0)
        expect(page.get_by_role("button", name="Download final.txt")).to_have_count(1)


class FilesTabTests(AttachmentUi):
    def make_project_with_files(self):
        project = self.new_project("Documents")
        first = self.new_task("Contract", project=project)
        second = self.new_task("Brand book", project=project)
        upload("bob", first, "contract-v1.pdf", b"%PDF-1.7 one", "application/pdf")
        upload("carol", first, "signatures.png", make_png(40, 30), "image/png")
        upload("bob", second, "Brand book.pdf", b"%PDF-1.7 two", "application/pdf")
        return project, first, second

    def test_the_tab_lists_every_file_with_its_task_and_searches_by_name(self):
        project, first, second = self.make_project_with_files()
        page = self.open("erin", "#/p/%s/files" % project)
        rows = page.locator("table.table tbody tr")
        expect(rows).to_have_count(3)
        expect(page.locator(".toolbar")).to_contain_text("3 files \u00b7")
        expect(rows.first).to_contain_text("Brand book.pdf")  # newest first
        expect(rows.first).to_contain_text(second)
        expect(rows.first).to_contain_text("Bob Chen")
        expect(page.get_by_role("button", name="Preview signatures.png")).to_have_count(1)
        expect(page.get_by_role("button", name="Preview contract-v1.pdf")).to_have_count(0)
        expect(page.get_by_role("button", name="Delete contract-v1.pdf")).to_have_count(0)  # a viewer cannot delete
        page.get_by_label("Search file names").fill("CONTRACT")
        expect(rows).to_have_count(1)
        expect(page.locator(".toolbar")).to_contain_text("1 file \u00b7")
        expect(page.locator(".toolbar")).to_contain_text("matching the search")
        page.get_by_label("Search file names").fill("zzz")
        expect(page.locator(".card .empty")).to_have_text("No file names match.")
        page.get_by_label("Search file names").fill("")
        expect(rows).to_have_count(3)
        with page.expect_download() as download:
            page.get_by_role("button", name="Download contract-v1.pdf").click()
        with open(download.value.path(), "rb") as saved:
            self.assertEqual(saved.read(), b"%PDF-1.7 one")
        rows.filter(has_text="contract-v1.pdf").get_by_role("link").click()
        expect(page.locator(".modal")).to_contain_text(first)
        expect(self.files(page)).to_have_count(2)

    def test_a_manager_can_delete_from_the_tab(self):
        project, _, _ = self.make_project_with_files()
        page = self.open("alice", "#/p/%s/files" % project)
        rows = page.locator("table.table tbody tr")
        expect(rows).to_have_count(3)
        page.get_by_role("button", name="Delete signatures.png").click()
        self.confirm(page)
        expect(rows).to_have_count(2)
        expect(page.locator(".toolbar")).to_contain_text("2 files \u00b7")
        expect(page.locator("table.table")).not_to_contain_text("signatures.png")

    def test_a_project_without_files_says_how_to_add_some(self):
        project = self.new_project("Empty", members=("bob",))
        page = self.open("bob", "#/p/%s/files" % project)
        expect(page.locator(".card .empty")).to_contain_text("No files yet")
        expect(page.locator(".toolbar")).to_contain_text("0 files")

    def test_long_lists_are_paged(self):
        project = self.new_project("Many files", members=("bob",))
        key = self.new_task("Everything", project=project)
        second = self.new_task("More", project=project)
        for i in range(14):
            upload("bob", key, "a-%02d.txt" % i, b"1", "text/plain")
            upload("bob", second, "b-%02d.txt" % i, b"1", "text/plain")
        page = self.open("bob", "#/p/%s/files" % project)
        rows = page.locator("table.table tbody tr")
        expect(rows).to_have_count(25)
        expect(page.get_by_text("1\u201325 of 28")).to_be_visible()
        page.get_by_role("button", name="Next \u203A").click()
        expect(rows).to_have_count(3)
        expect(page.get_by_text("26\u201328 of 28")).to_be_visible()
        page.get_by_role("button", name="\u2039 Prev").click()
        expect(rows).to_have_count(25)

    def test_the_tab_fits_a_phone_and_names_stay_text(self):
        project = self.new_project("Phone", members=("bob",))
        key = self.new_task("Narrow", project=project)
        nasty = '<b onmouseover="window.__pwned=2">bold<b> ' + "x" * 150 + ".txt"  # no slash: a slash would be taken for a directory
        self.assertEqual(upload("bob", key, nasty, b"1", "text/plain")[0], 201)
        page = self.open("bob", "#/p/%s/files" % project, viewport={"width": 390, "height": 800})
        expect(page.locator("table.table tbody tr")).to_have_count(1)
        expect(page.locator("table.table .file-name")).to_contain_text("<b onmouseover")
        self.assertIsNone(page.evaluate("window.__pwned"))
        expect(page.locator("table.table b")).to_have_count(0)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)

    def test_the_tab_works_in_dark_mode(self):
        project, _, _ = self.make_project_with_files()
        page = self.open("alice", "#/p/%s/files" % project, color_scheme="dark")
        expect(page.locator("table.table tbody tr")).to_have_count(3)
        self.assertNotEqual(page.evaluate("getComputedStyle(document.body).backgroundColor"), "rgb(244, 245, 248)")


if __name__ == "__main__":
    unittest.main()
