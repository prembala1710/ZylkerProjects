"""Browser end-to-end tests for baselines (Playwright + Chromium; skipped when unavailable).

They drive the real page against the real threaded server: the "Plan vs. actual" card on the Reports tab (its empty
state, setting, choosing and deleting a baseline, every number and sentence of a hand-made drift, who sees which
buttons), the ghost bars, slip chips and milestone diamonds on the timeline compared with the geometry of the bars
(on a hand-made project and on random ones), the baseline choice that follows you from tab to tab, hostile text, a
failing request, a phone-sized window, dark mode, the activity feed and the webhook form. Any console error, CSP
violation or uncaught exception fails the test. All dates are in 2031, so the real clock cannot change an answer.
"""

import datetime
import json
import os
import random
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
from zylker_pm.services import users  # noqa: E402

PROJECTS = iter(range(1, 10000))
JUNK = re.compile(r"\bnull\b|\bundefined\b|\bNaN\b|\[object")
MINUS = "\u2212"


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


def api(user, method, path, body=None):
    req = urllib.request.Request(BASE + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", "Bearer " + TOKENS[user])
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = r.read()
            return r.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def task(key, title, start=None, due=None, **extra):
    body = {"title": title, **extra}
    if start:
        body["start_date"] = start
    if due:
        body["due_date"] = due
    status, made = api("mia", "POST", "/projects/%s/tasks" % key, body)
    assert status == 201, made
    return made["key"]


def patch(key, **fields):
    status, made = api("mia", "PATCH", "/tasks/" + key, fields)
    assert status == 200, made
    return made


def baseline(project, name=None, note=None, user="mia"):
    body = {k: v for k, v in (("name", name), ("note", note)) if v is not None}
    status, made = api(user, "POST", "/projects/%s/baselines" % project, body)
    assert status == 201, made
    return made


def dd(first, second):
    return (datetime.date.fromisoformat(second) - datetime.date.fromisoformat(first)).days


def day(text):
    """How the page writes a date: without the year in the current year, with it in any other."""
    d = datetime.date.fromisoformat(text)
    return "%d %s%s" % (d.day, d.strftime("%b"), "" if d.year == datetime.date.today().year else " %d" % d.year)


def chip(days):
    return "+%dd" % days if days > 0 else "%s%dd" % (MINUS, -days)


# Reads what the timeline drew: per task its bar, its baseline strip and its slip chip; per milestone its diamonds.
READ = """() => {
  const num = (el, a) => Number(el.getAttribute(a));
  const rows = {};
  for (const g of document.querySelectorAll('svg.chart .tl-row')) {
    const bar = g.querySelector('rect.tl-bar'), ghost = g.querySelector('rect.tl-ghost'), chip = g.querySelector('text.tl-slip');
    rows[g.getAttribute('aria-label').split(' ')[0]] = { label: g.getAttribute('aria-label'), title: bar.querySelector('title').textContent,
      bar: { x: num(bar, 'x'), w: num(bar, 'width'), y: num(bar, 'y'), h: num(bar, 'height') },
      ghost: ghost ? { x: num(ghost, 'x'), w: num(ghost, 'width'), y: num(ghost, 'y'), h: num(ghost, 'height') } : null,
      chip: chip ? { text: chip.textContent, cls: chip.getAttribute('class'), x: num(chip, 'x'), y: num(chip, 'y') } : null };
  }
  const centre = (p) => Number(p.getAttribute('d').match(/M\\s*(-?[\\d.]+)/)[1]);
  const milestones = [...document.querySelectorAll('svg.chart .tl-ms')].map((p) => {
    const text = p.nextElementSibling, tspan = text.querySelector('tspan');
    return { cx: centre(p), name: text.childNodes[0].textContent, title: p.querySelector('title').textContent, chip: tspan ? { text: tspan.textContent, cls: tspan.getAttribute('class') } : null };
  });
  return { rows, milestones, ghosts: [...document.querySelectorAll('svg.chart .tl-ms-ghost')].map(centre) };
}"""

CONTRAST = """(selector) => {
  const rgb = (c) => c.match(/[\\d.]+/g).slice(0, 3).map(Number);
  const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const el = document.querySelector(selector);
  const fg = rgb(getComputedStyle(el).fill), bg = rgb(getComputedStyle(document.querySelector('.card')).backgroundColor);
  const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}"""


PALETTE = """() => {
  const probe = (v) => { const e = document.createElement('div'); e.style.color = `var(${v})`; document.body.append(e); const c = getComputedStyle(e).color; e.remove(); return c; };
  const fill = (selector) => getComputedStyle(document.querySelector(selector)).fill;
  return { late: fill('.tl-slip.late'), ahead: fill('.tl-slip.ahead'), bad: probe('--bad'), ok: probe('--ok') };
}"""


class Base(unittest.TestCase):
    viewport = {"width": 1360, "height": 1000}

    def setUp(self):
        self.problems, self.contexts = [], []
        self.key = "B%d" % next(PROJECTS)
        status, made = api("mia", "POST", "/projects", {"key": self.key, "name": "Baselines %s" % self.key, "members": ["max"]})
        assert status == 201, made

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def open(self, user="mia", key=None, tab="timeline", viewport=None, scheme=None, route=None):
        kwargs = {"color_scheme": scheme} if scheme else {}
        context = BROWSER.new_context(viewport=viewport or self.viewport, **kwargs)
        self.contexts.append(context)
        context.add_init_script("sessionStorage.setItem('zpm_token', %s)" % json.dumps(TOKENS[user]))
        page = context.new_page()

        def on_console(m):
            if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource"):
                self.problems.append("console.%s: %s" % (m.type, m.text))

        page.on("console", on_console)
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.on("response", lambda r: self.problems.append("HTTP %d from %s" % (r.status, r.url)) if r.status >= 500 else None)
        if route:
            page.route(*route)
        page.goto(BASE + "/#/p/%s/%s" % (key or self.key, tab))
        self.ready(page, tab)
        return page

    def ready(self, page, tab):
        if tab == "timeline":
            page.wait_for_selector("svg.chart[aria-label='Project timeline']")
        elif tab == "reports":
            page.wait_for_selector("#baselines")
            expect(page.locator("#baselines")).not_to_contain_text("Loading")

    def go(self, page, tab):
        page.get_by_role("navigation", name="Project sections").get_by_role("link", name=tab.capitalize(), exact=True).click()
        self.ready(page, tab)

    def read(self, page):
        return page.evaluate(READ)

    def scenario(self, name="Kickoff", note="Agreed with the client", with_baseline=True):
        """Six tasks (one without dates) and two milestones, as planned; the baseline is taken straight away."""
        k = self.key
        made = {
            "discovery": task(k, "Discovery", "2031-03-03", "2031-03-07", estimate_hours=4),
            "design": task(k, "Design", "2031-03-10", "2031-03-14", estimate_hours=8, assignee="max"),
            "build": task(k, "Build", "2031-03-17", "2031-03-24", estimate_hours=16),
            "review": task(k, "Review", "2031-03-25", "2031-03-28", estimate_hours=4),
            "launch": task(k, "Launch", None, "2031-03-31", estimate_hours=2),
            "docs": task(k, "Docs"),
        }
        for label, due in (("Beta", "2031-03-28"), ("Release", "2031-04-04")):
            status, m = api("mia", "POST", "/projects/%s/milestones" % k, {"name": label, "due_date": due})
            assert status == 201, m
            made[label.lower()] = m["id"]
        if with_baseline:
            made["baseline"] = baseline(k, name, note)
        return made

    def drift(self, made):
        """Design +5 days, Build +9, Review 2 days early, one task added, the Beta milestone 5 days later."""
        patch(made["design"], due_date="2031-03-19")
        patch(made["build"], start_date="2031-03-24", due_date="2031-04-02")
        patch(made["review"], due_date="2031-03-26")
        made["extra"] = task(self.key, "Extra", "2031-03-31", "2031-04-03", estimate_hours=6)
        status, m = api("mia", "PATCH", "/milestones/%d" % made["beta"], {"due_date": "2031-04-02"})
        assert status == 200, m

    def px(self, rows, made):
        discovery = rows[made["discovery"]]["bar"]
        return discovery["w"] / 5  # 3 to 7 March: five days, wide enough never to be held at the minimum width


HEADLINE = (
    "The project now finishes 3 Apr 2031, 3 days after the 31 Mar 2031 in the baseline. "
    "Of 6 tasks in the baseline, 2 slipped, 1 moved earlier, 2 are on plan and 1 has no dates. "
    "Since then 1 task was added. The estimated scope went from 34h to 40h."
)


class CardTests(Base):
    def test_a_project_without_a_baseline_says_so_and_only_managers_can_set_one(self):
        self.scenario(with_baseline=False)
        for user, can in (("mia", True), ("prem", True), ("max", False), ("val", False)):
            page = self.open(user, tab="reports")
            expect(page.locator("#baseline-empty")).to_contain_text("No baseline yet")
            self.assertEqual(page.locator("#baseline-set").count(), 1 if can else 0, user)
            self.assertEqual(page.locator("#baseline-pick, #baseline-kpis, #baseline-headline, #baseline-delete").count(), 0, user)

    def test_an_archived_project_cannot_be_baselined_from_the_page(self):
        self.scenario(with_baseline=False)
        self.assertEqual(api("mia", "PATCH", "/projects/" + self.key, {"status": "archived"})[0], 200)
        page = self.open(tab="reports")
        expect(page.locator("#baseline-empty")).to_be_visible()
        self.assertEqual(page.locator("#baseline-set").count(), 0)

    def test_setting_a_baseline_from_the_card(self):
        self.scenario(with_baseline=False)
        page = self.open(tab="reports")
        page.locator("#baseline-set").click()
        dialog = page.get_by_role("dialog", name="Set a baseline")
        expect(dialog).to_be_visible()
        dialog.get_by_label("Name").fill("Kickoff")
        dialog.get_by_label("Note").fill("Agreed with the client\nand the board")
        dialog.get_by_role("button", name="Set baseline", exact=True).click()
        expect(page.locator(".toast.ok")).to_have_text("Baseline \u201cKickoff\u201d set")
        expect(dialog).to_have_count(0)
        status, listing = api("mia", "GET", "/projects/%s/baselines" % self.key)
        item = listing["items"][0]
        self.assertEqual((item["name"], item["note"], item["leaf_tasks"], item["milestones"]), ("Kickoff", "Agreed with the client\nand the board", 6, 2))
        expect(page.locator("#baseline-about")).to_have_text("\u201cKickoff\u201d was set %s by Mia and holds 6 tasks (34h) and 2 milestones." % day(item["created_at"][:10]))
        self.assertEqual(page.locator("#baseline-note").inner_text(), "Agreed with the client\nand the board")
        expect(page.locator("#baseline-headline")).to_have_text("Nothing has moved since this baseline was set.")
        self.assertEqual(page.locator("#baseline-no-slips").inner_text(), "No task has slipped.")
        self.assertEqual(page.locator("#baseline-pick option").all_inner_texts(), ["Kickoff"])
        values = page.locator("#baseline-kpis .kpi .value").all_inner_texts()
        self.assertEqual(values, ["On plan", "0", "0", "5", "0", "0", "No change", "0/2"])

    def test_a_name_that_is_taken_is_refused_and_a_blank_name_gets_a_default(self):
        self.scenario(name="Kickoff")
        page = self.open(tab="reports")
        page.locator("#baseline-set").click()
        dialog = page.get_by_role("dialog", name="Set a baseline")
        dialog.get_by_label("Name").fill("KICKOFF")
        dialog.get_by_role("button", name="Set baseline", exact=True).click()
        expect(page.locator(".toast.error")).to_contain_text("already has a baseline called")
        expect(dialog).to_be_visible()  # nothing was lost
        self.assertEqual(len(api("mia", "GET", "/projects/%s/baselines" % self.key)[1]["items"]), 1)
        dialog.get_by_label("Name").fill("")
        dialog.get_by_label("Name").press("Enter")  # the Enter key saves, like the button
        expect(page.locator(".toast.ok")).to_have_text("Baseline \u201cBaseline 2\u201d set")
        expect(page.locator("#baseline-pick option")).to_have_text(["Baseline 2", "Kickoff"])  # the card is drawn again a moment after the toast
        self.assertEqual(page.locator("#baseline-pick").input_value(), str(api("mia", "GET", "/projects/%s/baselines" % self.key)[1]["items"][0]["id"]))

    def test_the_name_field_stops_at_the_length_the_server_accepts(self):
        self.scenario(with_baseline=False)
        page = self.open(tab="reports")
        page.locator("#baseline-set").click()
        dialog = page.get_by_role("dialog", name="Set a baseline")
        dialog.get_by_label("Name").fill("x" * 80)
        self.assertEqual(len(dialog.get_by_label("Name").input_value()), 60)
        dialog.get_by_label("Note").fill("y" * 600)
        self.assertEqual(len(dialog.get_by_label("Note").input_value()), 500)
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)
        self.assertEqual(api("mia", "GET", "/projects/%s/baselines" % self.key)[1]["items"], [])

    def test_every_sentence_number_and_row_of_a_drift(self):
        made = self.scenario()
        self.drift(made)
        page = self.open(tab="reports")
        k = self.key
        expect(page.locator("#baseline-headline")).to_have_text(HEADLINE)
        self.assertEqual(page.locator("#baseline-kpis .kpi .label").all_inner_texts(),
                         ["FINISH VS BASELINE", "SLIPPED", "MOVED EARLIER", "ON PLAN", "ADDED", "DROPPED", "SCOPE", "MILESTONES LATE"])
        self.assertEqual(page.locator("#baseline-kpis .kpi .value").all_inner_texts(), ["+3d", "2", "1", "2", "1", "0", "+6h", "1/2"])
        classes = [c.split() for c in page.locator("#baseline-kpis .kpi").evaluate_all("els => els.map(e => e.className)")]
        self.assertEqual([("bad" in c, "ok" in c, "warn" in c) for c in classes],
                         [(True, False, False), (True, False, False), (False, True, False), (False, False, False), (False, False, True), (False, False, False), (False, False, True), (True, False, False)])
        rows = [r.split("\t") for r in page.locator("#baseline-slips tbody tr").evaluate_all("rs => rs.map(r => [...r.cells].map(c => c.innerText).join('\\t'))")]
        self.assertEqual(rows, [
            ["%s-3 Build" % k, "\u2014", "24 Mar 2031", "2 Apr 2031", "+9d"],
            ["%s-2 Design" % k, "Max", "14 Mar 2031", "19 Mar 2031", "+5d"],
        ])
        ms = [r.split("\t") for r in page.locator("#baseline-milestones tbody tr").evaluate_all("rs => rs.map(r => [...r.cells].map(c => c.innerText).join('\\t'))")]
        self.assertEqual(ms, [["Beta", "28 Mar 2031", "2 Apr 2031", "+5d"], ["Release", "4 Apr 2031", "4 Apr 2031", "On plan"]])
        expect(page.locator("#baseline-added")).to_have_text("Tasks added since: %s-7 Extra" % k)
        self.assertEqual(page.locator("#baseline-dropped, #baseline-new-milestones").count(), 0)
        page.locator("#baseline-slips a", has_text="%s-3" % k).click()  # a task in the table opens
        expect(page.get_by_role("dialog")).to_contain_text("Build")

    def test_dropped_tasks_and_new_milestones_are_listed(self):
        made = self.scenario()
        k = self.key
        patch(made["launch"], status="cancelled")
        self.assertEqual(api("mia", "POST", "/projects/%s/milestones" % k, {"name": "Celebration", "due_date": "2031-04-10"})[0], 201)
        self.assertEqual(api("mia", "DELETE", "/tasks/" + made["docs"])[0], 204)
        page = self.open(tab="reports")
        expect(page.locator("#baseline-dropped")).to_have_text("Tasks dropped since: %s-5 Launch (cancelled); %s-6 Docs (deleted)" % (k, k))
        expect(page.locator("#baseline-new-milestones")).to_have_text("Milestones added since: Celebration (due 10 Apr 2031)")
        self.assertEqual(page.locator("#baseline-dropped a").count(), 1)  # a deleted task has nothing to open
        values = page.locator("#baseline-kpis .kpi .value").all_inner_texts()
        self.assertEqual(values[4:6], ["0", "2"])

    def test_choosing_and_deleting_baselines(self):
        made = self.scenario(name="Kickoff")
        self.drift(made)
        second = baseline(self.key, "Re-plan")
        page = self.open(tab="reports")
        self.assertEqual(page.locator("#baseline-pick option").all_inner_texts(), ["Re-plan", "Kickoff"])  # newest first
        self.assertEqual(page.locator("#baseline-pick").input_value(), str(second["id"]))  # and the newest is the one compared with
        expect(page.locator("#baseline-headline")).to_have_text("Nothing has moved since this baseline was set.")
        page.select_option("#baseline-pick", label="Kickoff")
        expect(page.locator("#baseline-headline")).to_have_text(HEADLINE)
        page.locator("#baseline-delete").click()
        page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
        self.assertEqual(len(api("mia", "GET", "/projects/%s/baselines" % self.key)[1]["items"]), 2)  # cancelled: nothing happened
        page.locator("#baseline-delete").click()
        expect(page.get_by_role("dialog")).to_contain_text("Delete the baseline \u201cKickoff\u201d?")
        page.get_by_role("dialog").get_by_role("button", name="Delete").click()
        expect(page.locator(".toast.ok")).to_have_text("Baseline deleted")
        expect(page.locator("#baseline-headline")).to_have_text("Nothing has moved since this baseline was set.")
        self.assertEqual(page.locator("#baseline-pick option").all_inner_texts(), ["Re-plan"])
        page.locator("#baseline-delete").click()
        page.get_by_role("dialog").get_by_role("button", name="Delete").click()
        expect(page.locator("#baseline-empty")).to_be_visible()
        self.assertEqual(api("mia", "GET", "/projects/%s/baselines" % self.key)[1]["items"], [])

    def test_viewers_and_members_see_the_comparison_but_no_buttons(self):
        made = self.scenario()
        self.drift(made)
        for user in ("max", "val"):
            page = self.open(user, tab="reports")
            expect(page.locator("#baseline-headline")).to_have_text(HEADLINE)
            self.assertEqual(page.locator("#baseline-set, #baseline-delete").count(), 0, user)
            self.assertEqual(page.locator("#baseline-pick").count(), 1, user)

    def test_a_task_done_long_before_its_due_date_is_early_whatever_its_date_says(self):
        k = self.key
        t = task(k, "Early bird", "2031-03-03", "2031-03-07")
        item = baseline(k, "Plan")
        patch(t, status="in_progress")
        patch(t, status="done")  # today, years before the due date
        shift = api("mia", "GET", "/projects/%s/baselines/%d" % (k, item["id"]))[1]["summary"]["finish"]["shift_days"]
        self.assertLess(shift, -1000)
        page = self.open(tab="reports")
        expect(page.locator("#baseline-headline")).to_contain_text("%d days before the 7 Mar 2031 in the baseline. Of 1 task in the baseline, 1 moved earlier." % -shift)
        self.assertEqual(page.locator("#baseline-kpis .kpi .value").all_inner_texts()[:3], ["%s%dd" % (MINUS, -shift), "0", "1"])
        self.assertEqual(page.locator("#baseline-slips").count(), 0)

    def test_no_stray_words_on_the_page(self):
        made = self.scenario()
        self.drift(made)
        page = self.open(tab="reports")
        card = page.locator("#baselines").inner_text()
        self.assertIsNone(JUNK.search(card), card)
        page = self.open()
        text = page.locator(".card").first.inner_text()
        self.assertIsNone(JUNK.search(text), text)


class TimelineTests(Base):
    def test_the_planned_dates_sit_under_the_tasks_that_moved(self):
        made = self.scenario()
        self.drift(made)
        page = self.open()
        data = self.read(page)
        rows, px = data["rows"], self.px(data["rows"], made)
        self.assertGreaterEqual(px, 9)

        def x_of(date):
            return rows[made["discovery"]]["bar"]["x"] + dd("2031-03-03", date) * px

        planned = {"design": ("2031-03-10", "2031-03-14"), "build": ("2031-03-17", "2031-03-24"), "review": ("2031-03-25", "2031-03-28")}
        for name, (start, due) in planned.items():
            row = rows[made[name]]
            self.assertIsNotNone(row["ghost"], name)
            self.assertEqual((row["ghost"]["x"], row["ghost"]["w"]), (x_of(start), px * (dd(start, due) + 1)), name)
            self.assertEqual((row["ghost"]["y"], row["ghost"]["h"]), (row["bar"]["y"] + 19, 4), "directly under the bar, " + name)
            self.assertLess(row["bar"]["y"] + row["bar"]["h"], row["ghost"]["y"], "it does not cover the bar")
        for name in ("discovery", "launch", "extra"):  # not moved, or not in the baseline: nothing drawn
            self.assertIsNone(rows[made[name]]["ghost"], name)
        self.assertEqual(sorted(rows), sorted(made[n] for n in ("discovery", "design", "build", "review", "launch", "extra")))

    def test_the_slip_is_written_beside_the_bar(self):
        made = self.scenario()
        self.drift(made)
        data = self.read(self.open())
        rows, k = data["rows"], self.key
        got = {name: rows[made[name]]["chip"] for name in ("discovery", "design", "build", "review", "launch", "extra")}
        self.assertEqual({n: c and (c["text"], c["cls"]) for n, c in got.items()},
                         {"discovery": None, "design": ("+5d", "tl-slip late"), "build": ("+9d", "tl-slip late"), "review": (MINUS + "2d", "tl-slip ahead"), "launch": None, "extra": None})
        for name in ("design", "build", "review"):
            bar = rows[made[name]]["bar"]
            self.assertEqual((got[name]["x"], got[name]["y"]), (bar["x"] + bar["w"] + 10, bar["y"] - 6 + 19), name)
        self.assertIn("5 days later than the baseline", rows[made["design"]]["label"])
        self.assertIn("2 days earlier than the baseline", rows[made["review"]]["label"])
        self.assertNotIn("baseline", rows[made["launch"]]["label"])
        self.assertTrue(rows[made["design"]]["label"].startswith("%s-2 Design" % k))

    def test_the_tooltip_of_a_bar_says_what_was_planned(self):
        made = self.scenario()
        self.drift(made)
        rows = self.read(self.open())["rows"]
        self.assertIn("\nBaseline: 10 Mar 2031 \u2192 14 Mar 2031 (5 days later)", rows[made["design"]]["title"])
        self.assertIn("\nBaseline: 25 Mar 2031 \u2192 28 Mar 2031 (2 days earlier)", rows[made["review"]]["title"])
        self.assertIn("\nBaseline: \u2014 \u2192 31 Mar 2031", rows[made["launch"]]["title"])  # unmoved: no comment on it
        self.assertNotIn("days", rows[made["launch"]]["title"].split("Baseline:")[1])
        self.assertNotIn("Baseline", rows[made["extra"]]["title"])  # it was not in the baseline

    def test_a_milestone_that_moved_has_a_hollow_diamond_where_it_was_planned(self):
        made = self.scenario()
        self.drift(made)
        data = self.read(self.open())
        px = self.px(data["rows"], made)
        by_name = {m["name"]: m for m in data["milestones"]}
        beta, release = by_name["Beta"], by_name["Release"]
        self.assertEqual(data["ghosts"], [beta["cx"] - 5 * px])  # planned five days earlier, and only that one
        self.assertEqual((beta["chip"]["text"], beta["chip"]["cls"]), ("+5d", "tl-slip late"))
        self.assertIsNone(release["chip"])
        self.assertIn("Baseline: 28 Mar 2031 (5 days later)", beta["title"])
        self.assertIn("Baseline: 4 Apr 2031", release["title"])
        self.assertNotIn("days", release["title"].split("Baseline:")[1])

    def test_the_header_note_and_legend_say_what_is_being_compared(self):
        made = self.scenario()
        self.drift(made)
        page = self.open()
        created = made["baseline"]["created_at"][:10]
        expect(page.locator("#timeline-baseline-note")).to_have_text(
            "Compared with \u201cKickoff\u201d, set %s. The project now finishes 3 Apr 2031, 3 days after the 31 Mar 2031 in the baseline." % day(created))
        legend = page.locator(".legend").inner_text()
        self.assertIn("Baseline dates", legend)
        self.assertIn("Baseline milestone", legend)
        self.assertIn("later / earlier than the baseline", legend)
        self.assertEqual(page.locator("#timeline-baseline").input_value(), str(made["baseline"]["id"]))
        self.assertEqual(page.locator("#timeline-baseline option").all_inner_texts(), ["None", "Kickoff"])
        self.assertEqual(page.get_by_label("Compare with baseline").count(), 1)

    def test_choosing_none_draws_the_timeline_as_it_was_before_baselines(self):
        made = self.scenario()
        self.drift(made)
        page = self.open()
        self.assertEqual(page.locator(".tl-ghost").count(), 3)
        page.select_option("#timeline-baseline", label="None")
        page.wait_for_selector("svg.chart[aria-label='Project timeline']")  # the timeline is drawn again: it is empty for a moment, which would pass anything
        expect(page.locator(".tl-row")).to_have_count(6)
        self.assertEqual(page.locator(".tl-ghost, .tl-slip, .tl-ms-ghost, #timeline-baseline-note, .sw-ghost").count(), 0)
        self.assertEqual(page.locator("#timeline-baseline").input_value(), "")
        self.assertNotIn("Baseline", page.locator(".legend").inner_text())
        self.assertNotIn("Baseline", self.read(page)["rows"][made["design"]]["title"])
        page.select_option("#timeline-baseline", label="Kickoff")
        expect(page.locator(".tl-ghost")).to_have_count(3)
        expect(page.locator("#timeline-baseline-note")).to_be_visible()

    def test_the_choice_follows_you_between_tabs(self):
        made = self.scenario()
        self.drift(made)
        second = baseline(self.key, "Re-plan")
        page = self.open()  # the newest baseline: nothing has moved since
        self.assertEqual(page.locator("#timeline-baseline").input_value(), str(second["id"]))
        self.assertEqual(page.locator(".tl-ghost, .tl-slip").count(), 0)
        page.select_option("#timeline-baseline", label="Kickoff")
        expect(page.locator(".tl-ghost")).to_have_count(3)
        self.go(page, "reports")
        self.assertEqual(page.locator("#baseline-pick").input_value(), str(made["baseline"]["id"]))
        expect(page.locator("#baseline-headline")).to_have_text(HEADLINE)
        page.select_option("#baseline-pick", label="Re-plan")
        self.go(page, "timeline")
        self.assertEqual(page.locator("#timeline-baseline").input_value(), str(second["id"]))
        page.select_option("#timeline-baseline", label="None")
        self.go(page, "reports")  # the card always compares with something
        self.assertEqual(page.locator("#baseline-pick").input_value(), str(second["id"]))
        self.go(page, "timeline")
        self.assertEqual(page.locator("#timeline-baseline").input_value(), "")  # and the timeline remembers that you asked for none

    def test_setting_a_baseline_on_the_reports_tab_selects_it_on_the_timeline(self):
        made = self.scenario(name="Kickoff")
        self.drift(made)
        page = self.open()
        page.select_option("#timeline-baseline", label="None")  # something was chosen before: that must not stick
        expect(page.locator(".tl-ghost")).to_have_count(0)
        self.go(page, "reports")
        page.locator("#baseline-set").click()
        page.get_by_role("dialog").get_by_label("Name").fill("After the drift")
        page.get_by_role("dialog").get_by_role("button", name="Set baseline", exact=True).click()
        expect(page.locator(".toast.ok")).to_be_visible()
        expect(page.locator("#baseline-pick option:checked")).to_have_text("After the drift")
        self.go(page, "timeline")
        expect(page.locator("#timeline-baseline option:checked")).to_have_text("After the drift")
        self.assertEqual(page.locator(".tl-ghost").count(), 0)  # nothing has moved since it was set

    def test_a_project_without_baselines_looks_as_it_did_before(self):
        self.scenario(with_baseline=False)
        page = self.open()
        self.assertEqual(page.locator("#timeline-baseline, .tl-ghost, .tl-slip, .tl-ms-ghost, .sw-ghost").count(), 0)
        expect(page.locator("#timeline-baseline-note")).to_have_text("There is no baseline yet. Set one on the Reports tab to see how far the plan drifts from it.")
        for user in ("max", "val"):  # nothing to suggest to somebody who cannot set one
            page = self.open(user)
            self.assertEqual(page.locator("#timeline-baseline-note").count(), 0, user)

    def test_viewers_and_members_see_the_comparison_too(self):
        made = self.scenario()
        self.drift(made)
        for user in ("max", "val"):
            page = self.open(user)
            self.assertEqual((page.locator(".tl-ghost").count(), page.locator(".tl-slip").count(), page.locator(".tl-ms-ghost").count()), (3, 4, 1), user)
            self.assertEqual(page.locator("#timeline-baseline").count(), 1, user)

    def test_a_dropped_task_and_a_split_task_are_not_drawn_as_moved(self):
        made = self.scenario()
        k = self.key
        patch(made["launch"], status="cancelled")
        child = task(k, "Build part", "2031-03-17", "2031-03-20", parent=made["build"])
        page = self.open()
        data = self.read(page)
        self.assertNotIn(made["launch"], data["rows"])
        self.assertNotIn(made["build"], data["rows"])  # a parent is not on the timeline, only its leaves
        self.assertIsNone(data["rows"][child]["ghost"])  # new work has no plan to compare with
        self.assertEqual(page.locator(".tl-ghost, .tl-slip").count(), 0)

    def test_the_range_grows_to_hold_a_plan_that_was_earlier_than_today_and_than_anything_now(self):
        k = self.key
        today = datetime.date.today()
        planned = (today - datetime.timedelta(days=60), today - datetime.timedelta(days=56))
        later = (today + datetime.timedelta(days=10), today + datetime.timedelta(days=14))
        t = task(k, "Early", planned[0].isoformat(), planned[1].isoformat())
        baseline(k, "Plan")
        patch(t, start_date=later[0].isoformat(), due_date=later[1].isoformat())
        page = self.open()
        row = self.read(page)["rows"][t]
        px = row["bar"]["w"] / 5
        self.assertIsNotNone(row["ghost"])
        self.assertGreaterEqual(row["ghost"]["x"], 270)  # inside the chart, right of the labels
        self.assertEqual(row["bar"]["x"] - row["ghost"]["x"], 70 * px)
        self.assertEqual(row["chip"]["text"], "+70d")

    def test_when_the_baselines_cannot_be_loaded_the_timeline_still_draws(self):
        made = self.scenario()
        self.drift(made)
        refuse = ("**/api/projects/*/baselines*", lambda r: r.fulfill(status=403, content_type="application/json", body='{"error":{"code":"x","message":"gone"}}'))
        page = self.open(route=refuse)
        self.assertEqual(page.locator(".tl-row").count(), 6)
        self.assertEqual(page.locator(".tl-ghost, .tl-slip, .tl-ms-ghost, #timeline-baseline").count(), 0)
        expect(page.locator("#timeline-baseline-note")).to_have_text("The baselines could not be loaded, so nothing is compared.")

    def test_hostile_names_and_titles_stay_text(self):
        k = self.key
        evil = '<img src=x onerror="window.pwned=1"> & "quotes"'
        a = task(k, evil, "2031-03-03", "2031-03-07")
        task(k, "<b>bold</b>", "2031-03-10", "2031-03-12")
        baseline(k, "<script>window.pwned=2</script>", "<b>note</b> <img src=x onerror=window.pwned=3>")
        patch(a, due_date="2031-03-12")
        page = self.open(tab="reports")
        self.assertEqual(page.locator("#baselines img, #baselines b, #baselines script").count(), 0)
        self.assertIn(evil, page.locator("#baseline-slips").inner_text())
        self.assertEqual(page.locator("#baseline-note").inner_text(), "<b>note</b> <img src=x onerror=window.pwned=3>")
        self.assertEqual(page.locator("#baseline-pick option").all_inner_texts(), ["<script>window.pwned=2</script>"])
        self.assertIn("\u201c<script>window.pwned=2</script>\u201d was set", page.locator("#baseline-about").inner_text())
        self.go(page, "timeline")
        self.assertIn("<script>window.pwned=2</script>", page.locator("#timeline-baseline-note").inner_text())
        self.assertIn(evil, page.locator(".tl-row", has_text="quotes").locator("rect.tl-bar title").text_content())
        self.assertIsNone(page.evaluate("window.pwned"))

    def test_dark_mode_phone_window_and_contrast(self):
        made = self.scenario()
        self.drift(made)
        for scheme in ("light", "dark"):
            page = self.open(scheme=scheme)
            for selector in (".tl-slip.late", ".tl-slip.ahead"):
                self.assertGreaterEqual(page.evaluate(CONTRAST, selector), 3.0, "%s in %s" % (selector, scheme))
            colours = page.evaluate(PALETTE)
            self.assertEqual((colours["late"], colours["ahead"]), (colours["bad"], colours["ok"]), "late is the colour of trouble and early the colour of good news in " + scheme)
            swatch = page.locator(".sw-ghost").bounding_box()
            self.assertGreaterEqual((swatch["width"], swatch["height"]), (10, 3), "the legend swatch can be seen")
            self.assertNotEqual(page.locator(".tl-ghost").first.evaluate("e => getComputedStyle(e).fill"), "none")
            self.assertEqual(page.locator(".tl-ghost").first.evaluate("e => getComputedStyle(e).pointerEvents"), "none")
        page = self.open(tab="reports", viewport={"width": 390, "height": 800}, scheme="dark")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391, "the page itself does not scroll sideways")
        for selector in ("#baseline-set", "#baseline-pick", "#baseline-delete"):
            box = page.locator(selector).bounding_box()
            self.assertGreaterEqual(box["x"], 0, selector)
            self.assertLessEqual(box["x"] + box["width"], 391, selector)
        page = self.open(viewport={"width": 390, "height": 800})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertEqual(page.locator(".tl-ghost").count(), 3)

    def test_a_failing_baselines_request_is_explained_on_the_reports_tab(self):
        made = self.scenario()
        self.drift(made)
        refuse = ("**/api/projects/*/baselines*", lambda r: r.fulfill(status=409, content_type="application/json", body='{"error":{"code":"baselines_not_initialised","message":"Restart the server."}}'))
        page = self.open(tab="reports", route=refuse)
        expect(page.locator("#baseline-error")).to_have_text("Restart the server.")
        self.assertEqual(page.locator("#baseline-kpis").count(), 0)
        expect(page.get_by_role("heading", name="Tasks by status")).to_be_visible()  # the rest of the tab is untouched


class ElsewhereTests(Base):
    def test_the_activity_tab_describes_setting_and_deleting(self):
        made = self.scenario()
        page = self.open(tab="reports")
        page.locator("#baseline-delete").click()
        page.get_by_role("dialog").get_by_role("button", name="Delete").click()
        expect(page.locator(".toast.ok")).to_have_text("Baseline deleted")
        self.go(page, "activity")
        expect(page.locator(".item", has_text="deleted the baseline")).to_contain_text("Mia deleted the baseline \u201cKickoff\u201d")
        expect(page.locator(".item", has_text="set the baseline")).to_contain_text("Mia set the baseline \u201cKickoff\u201d (6 tasks, 2 milestones)")
        self.assertEqual(made["baseline"]["name"], "Kickoff")

    def test_webhooks_can_be_subscribed_to_baseline_events(self):
        context = BROWSER.new_context(viewport=self.viewport)
        self.contexts.append(context)
        context.add_init_script("sessionStorage.setItem('zpm_token', %s)" % json.dumps(TOKENS["prem"]))
        page = context.new_page()
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.goto(BASE + "/#/webhooks")
        page.get_by_role("button", name="+ New webhook").click()
        page.get_by_label("All events").uncheck()
        box = page.get_by_label("Baselines \u2014 everything")
        expect(box).to_be_visible()
        box.check()
        expect(box).to_be_checked()
        group = page.locator(".check-group", has_text="Baselines \u2014 everything")
        self.assertEqual([t.strip() for t in group.locator(".children .check-row").all_inner_texts()], ["created", "deleted"])


def pick(rng, days):
    """A start date and, mostly, a due date on or after it."""
    start = rng.choice(days)
    if rng.random() < 0.15:
        return start, None
    return start, rng.choice([d for d in days if d >= start])


class RandomProjectTests(Base):
    """Strips and chips have to land where the dates say, whatever was planned and whatever has happened since."""

    def test_random_projects(self):
        for seed in range(1, 5):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                key = "R%d" % next(PROJECTS)
                self.assertEqual(api("mia", "POST", "/projects", {"key": key, "name": "Random %d" % seed, "members": []})[0], 201)
                days = ["2031-03-%02d" % d for d in range(3, 29)] + ["2031-04-%02d" % d for d in range(1, 12)]

                plan, keys = {}, []
                for n in range(14):
                    start, due = pick(rng, days) if rng.random() < 0.9 else (None, None)
                    k = task(key, "Task %d" % (n + 1), start, due)
                    keys.append(k)
                    plan[k] = (start, due)
                self.assertEqual(api("mia", "POST", "/projects/%s/baselines" % key, {"name": "Plan"})[0], 201)
                now, cancelled = dict(plan), set()
                for k in keys:
                    roll = rng.random()
                    if roll < 0.2:
                        continue
                    if roll < 0.3:
                        patch(k, status="cancelled")
                        cancelled.add(k)
                        continue
                    start, due = pick(rng, days) if roll < 0.9 else (None, None)
                    fields = {"start_date": start, "due_date": due}
                    status, made = api("mia", "PATCH", "/tasks/" + k, fields)
                    self.assertEqual(status, 200, made)
                    now[k] = (start, due)
                for n in range(3):
                    start, due = pick(rng, days)
                    k = task(key, "Added %d" % n, start, due)
                    now[k] = (start, due)
                page = self.open(key=key)
                data = self.read(page)
                rows = data["rows"]
                shown = {k for k, (s, e) in now.items() if (s or e) and k not in cancelled}
                self.assertEqual(set(rows), shown)
                anchor = next(k for k in rows if rows[k]["bar"]["w"] >= 9)
                a_start, a_end = (now[anchor][0] or now[anchor][1]), (now[anchor][1] or now[anchor][0])
                px = rows[anchor]["bar"]["w"] / (dd(a_start, a_end) + 1)
                self.assertEqual(px, int(px))
                for k, row in rows.items():
                    s, e = now[k][0] or now[k][1], now[k][1] or now[k][0]
                    self.assertEqual(row["bar"]["x"], rows[anchor]["bar"]["x"] + dd(a_start, s) * px, k)
                    before = plan.get(k)
                    gs = (before[0] or before[1]) if before else None
                    ge = (before[1] or before[0]) if before else None
                    if gs and (gs, ge) != (s, e):
                        self.assertIsNotNone(row["ghost"], (seed, k, before, now[k]))
                        self.assertEqual((row["ghost"]["x"], row["ghost"]["w"]), (rows[anchor]["bar"]["x"] + dd(a_start, gs) * px, max(px * (dd(gs, ge) + 1), 8)), (seed, k))
                    else:
                        self.assertIsNone(row["ghost"], (seed, k, before, now[k]))
                    slip = dd(before[1], now[k][1]) if before and before[1] and now[k][1] else 0
                    if slip:
                        self.assertEqual((row["chip"]["text"], row["chip"]["cls"]), (chip(slip), "tl-slip late" if slip > 0 else "tl-slip ahead"), (seed, k))
                    else:
                        self.assertIsNone(row["chip"], (seed, k, before, now[k]))
                total = sum(1 for r in rows.values() if r["ghost"]), sum(1 for r in rows.values() if r["chip"])
                self.assertGreater(total[0], 0, "the random project has something moved")
                self.assertGreater(total[1], 0)


if __name__ == "__main__":
    unittest.main()
