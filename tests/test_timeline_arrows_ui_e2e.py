"""Browser end-to-end tests for the dependency arrows on the project timeline (Playwright + Chromium; skipped when
unavailable).

They drive the real page against the real threaded server: where every arrow starts and ends compared with the bars
it joins (on a hand-made project and on random ones), the three colours, the switch, the highlight when a row is
hovered or focused, the tooltips, the list of dates that break a dependency, links that cannot be drawn, who sees
what, hostile titles, a failing request and a phone-sized window. Any console error, CSP violation or uncaught
exception fails the test.
"""

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


def link(blocked, blocker, **extra):
    status, made = api("mia", "POST", "/tasks/%s/dependencies" % blocked, {"blocked_by": blocker, **extra})
    return status


# Reads every arrow and every bar off the page: the arrow as the points of its path, the bars as rectangles.
READ = """() => {
  const num = (v) => Number(v);
  const bars = {};
  for (const g of document.querySelectorAll('svg.chart .tl-row')) {
    const r = g.querySelector('rect.tl-bar');
    bars[g.getAttribute('aria-label').split(' ')[0]] = { l: num(r.getAttribute('x')), r: num(r.getAttribute('x')) + num(r.getAttribute('width')),
      cy: num(r.getAttribute('y')) + num(r.getAttribute('height')) / 2 };
  }
  const arrows = [...document.querySelectorAll('svg.chart .tl-dep')].map((p) => {
    const pts = []; let x = 0, y = 0;
    for (const m of p.getAttribute('d').matchAll(/([MHV])\\s*(-?[\\d.]+)(?:\\s+(-?[\\d.]+))?/g)) {
      if (m[1] === 'M') { x = num(m[2]); y = num(m[3]); } else if (m[1] === 'H') x = num(m[2]); else y = num(m[2]);
      pts.push([x, y]);
    }
    return { from: p.dataset.from, to: p.dataset.to, type: p.dataset.type, state: p.dataset.state, cls: p.getAttribute('class'), marker: p.getAttribute('marker-end'), pts };
  });
  return { bars, arrows };
}"""


class Base(unittest.TestCase):
    viewport = {"width": 1360, "height": 1000}

    def setUp(self):
        self.problems, self.contexts = [], []
        self.key = "T%d" % next(PROJECTS)
        status, made = api("mia", "POST", "/projects", {"key": self.key, "name": "Timeline %s" % self.key, "members": ["max"]})
        assert status == 201, made

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def open(self, user="mia", key=None, viewport=None, scheme=None, route=None):
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
        page.goto(BASE + "/#/p/%s/timeline" % (key or self.key))
        page.wait_for_selector("svg.chart")
        return page

    def read(self, page):
        return page.evaluate(READ)

    def hand_made(self):
        """Five tasks in a row of dates, four arrows that can be drawn and three links that cannot."""
        k = self.key
        t1 = task(k, "Discovery", "2026-10-05", "2026-10-09")
        t2 = task(k, "Design", "2026-10-12", "2026-10-23")
        t3 = task(k, "Build", "2026-10-14", "2026-11-06")  # starts before Design ends, and before its 2 days of lag
        t4 = task(k, "Review", "2026-10-26", "2026-10-30")
        t5 = task(k, "Launch", "2026-11-02", "2026-11-04")  # due before Build is
        t6 = task(k, "Docs")  # no dates: a link to it cannot be drawn
        t7 = task(k, "Dropped", "2026-10-20", "2026-10-22")
        t8 = task(k, "Epic", type="epic")
        task(k, "Child", "2026-10-21", "2026-10-22", parent=t8)
        self.assertEqual(api("mia", "PATCH", "/tasks/" + t1, {"status": "done"})[0], 200)
        self.assertEqual(api("mia", "PATCH", "/tasks/" + t7, {"status": "cancelled"})[0], 200)
        for blocked, blocker, extra in (
            (t2, t1, {}),
            (t3, t2, {"lag_days": 2}),
            (t4, t3, {"type": "start_to_start"}),
            (t5, t3, {"type": "finish_to_finish"}),
            (t5, t4, {}),
            (t6, t1, {}),
            (t7, t2, {}),
            (t8, t1, {}),
        ):
            self.assertEqual(link(blocked, blocker, **extra), 200)
        return [t1, t2, t3, t4, t5, t6, t7, t8]

    def expected_arrows(self):
        k = self.key
        return {
            ("%s-1" % k, "%s-2" % k): ("finish_to_start", "ok"),
            ("%s-2" % k, "%s-3" % k): ("finish_to_start", "conflict"),
            ("%s-3" % k, "%s-4" % k): ("start_to_start", "holding"),
            ("%s-3" % k, "%s-5" % k): ("finish_to_finish", "conflict"),
            ("%s-4" % k, "%s-5" % k): ("finish_to_start", "holding"),
        }


def edge_points(arrow, bars):
    """Where an arrow of this type should start and end, taken from the bars it joins."""
    a, b = bars[arrow["from"]], bars[arrow["to"]]
    start = (a["l"] if arrow["type"] == "start_to_start" else a["r"], a["cy"])
    end = (b["r"] if arrow["type"] == "finish_to_finish" else b["l"], b["cy"])
    return start, end


def day(text):
    """How the page writes a date of 2026: without the year in 2026, with it in any other year."""
    import datetime

    d = datetime.date.fromisoformat(text)
    return "%d %s%s" % (d.day, d.strftime("%b"), "" if d.year == datetime.date.today().year else " %d" % d.year)


class ArrowTests(Base):
    def test_every_arrow_joins_the_right_edges_of_the_right_bars(self):
        self.hand_made()
        page = self.open()
        data = self.read(page)
        got = {(a["from"], a["to"]): (a["type"], a["state"]) for a in data["arrows"]}
        self.assertEqual(got, self.expected_arrows())
        for arrow in data["arrows"]:
            start, end = edge_points(arrow, data["bars"])
            self.assertEqual((tuple(arrow["pts"][0]), tuple(arrow["pts"][-1])), (start, end), arrow["from"] + " to " + arrow["to"])
            self.assertEqual(arrow["marker"], "url(#tl-head-%s)" % arrow["state"])
            self.assertEqual(page.locator("#tl-head-%s" % arrow["state"]).count(), 1)

    def test_arrows_only_run_straight_and_end_pointing_into_the_bar(self):
        self.hand_made()
        data = self.read(self.open())
        for arrow in data["arrows"]:
            pts = arrow["pts"]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
                self.assertTrue(x1 == x2 or y1 == y2, "a slanted piece in %s" % arrow["from"])
            top, bottom = sorted((data["bars"][arrow["from"]]["cy"], data["bars"][arrow["to"]]["cy"]))
            self.assertTrue(all(top <= y <= bottom for _, y in pts), "an arrow stays between the two rows it joins: " + arrow["from"])
            (xa, ya), (xb, yb) = pts[-2], pts[-1]
            self.assertEqual(ya, yb, "the last piece is horizontal")
            self.assertTrue(xb < xa if arrow["type"] == "finish_to_finish" else xb > xa, "points into the bar: " + arrow["type"])

    def test_three_looks_for_three_meanings(self):
        self.hand_made()
        page = self.open()
        style = lambda state: page.locator(".tl-dep.%s" % state).first.evaluate("e => { const s = getComputedStyle(e); return [s.stroke, s.strokeDasharray, s.pointerEvents] }")  # noqa: E731
        ok, holding, conflict = style("ok"), style("holding"), style("conflict")
        self.assertEqual(len({ok[0], holding[0], conflict[0]}), 3, "three colours")
        self.assertEqual([ok[1], holding[1]], ["none", "none"])
        self.assertNotEqual(conflict[1], "none", "a break in the line, not only a colour")
        self.assertEqual({ok[2], holding[2], conflict[2]}, {"none"})
        for state in ("ok", "holding", "conflict"):  # the arrowheads wear the same colour as their line
            line = page.locator(".tl-dep.%s" % state).first.evaluate("e => getComputedStyle(e).stroke")
            head = page.locator("#tl-head-%s path" % state).evaluate("e => getComputedStyle(e).fill")
            self.assertEqual(head, line, state)

    def test_arrows_never_take_a_click_away_from_the_row_beneath(self):
        self.hand_made()
        page = self.open()
        for index in range(page.locator(".tl-dep").count()):
            top = page.locator(".tl-dep").nth(index).evaluate(
                """e => { const p = e.getPointAtLength(e.getTotalLength() / 3), m = e.getScreenCTM();
                         const hit = document.elementFromPoint(m.a * p.x + m.e, m.d * p.y + m.f); return hit ? hit.getAttribute('class') || hit.tagName : null }"""
            )
            self.assertIsNotNone(top, "the point is on the screen")
            self.assertNotIn("tl-dep", top, top)
        page.locator(".tl-row", has_text="Design").locator("rect.hit").click()
        expect(page.locator(".modal")).to_contain_text("%s-2" % self.key)

    def test_the_switch_hides_and_shows_every_arrow(self):
        self.hand_made()
        page = self.open()
        box = page.get_by_label("Dependency arrows")
        expect(box).to_be_checked()
        expect(page.locator(".tl-dep").first).to_be_visible()
        box.uncheck()
        self.assertEqual([page.locator(".tl-dep").nth(i).is_visible() for i in range(5)], [False] * 5)
        box.check()
        self.assertEqual([page.locator(".tl-dep").nth(i).is_visible() for i in range(5)], [True] * 5)

    def test_hovering_or_tabbing_to_a_row_picks_out_its_arrows(self):
        self.hand_made()
        page = self.open()
        k = self.key
        lit = lambda: sorted(page.eval_on_selector_all(".tl-dep.hl", "els => els.map(e => [e.dataset.from, e.dataset.to])"))  # noqa: E731
        self.assertEqual(lit(), [])
        page.locator(".tl-row", has_text="Launch").locator("rect.hit").hover()
        self.assertEqual(lit(), [["%s-3" % k, "%s-5" % k], ["%s-4" % k, "%s-5" % k]])
        self.assertIn("dep-focus", page.locator("svg.chart").get_attribute("class"))
        page.mouse.move(5, 5)
        self.assertEqual(lit(), [])
        self.assertNotIn("dep-focus", page.locator("svg.chart").get_attribute("class"))
        page.locator(".tl-row", has_text="Review").focus()
        self.assertEqual(lit(), [["%s-3" % k, "%s-4" % k], ["%s-4" % k, "%s-5" % k]])
        page.locator(".tl-row", has_text="Review").blur()
        self.assertEqual(lit(), [])

    def test_a_task_without_links_picks_out_nothing(self):
        self.hand_made()
        loner = task(self.key, "Alone", "2026-11-09", "2026-11-10")
        page = self.open()
        page.locator(".tl-row", has_text=loner).locator("rect.hit").hover()
        self.assertEqual(page.locator(".tl-dep.hl").count(), 0)
        self.assertNotIn("dep-focus", page.locator("svg.chart").get_attribute("class"))

    def test_the_tooltip_of_a_bar_names_what_it_waits_for_and_what_it_holds_up(self):
        self.hand_made()
        page = self.open()
        k = self.key
        title = lambda name: page.locator(".tl-row", has_text=name).locator("rect.tl-bar title").text_content()  # noqa: E731
        self.assertIn("Waits for: %s-3 (FF), %s-4 (FS)" % (k, k), title("Launch"))
        self.assertIn("Waits for: %s-2 (FS \u00b7 2d lag)" % k, title("Build"))
        self.assertIn("Holds up: %s-4 (SS), %s-5 (FF)" % (k, k), title("Build"))
        self.assertIn("Holds up: %s-2 (FS), %s-6 (FS), %s-8 (FS)" % (k, k, k), title("Discovery"))  # links that are not drawn are told here
        self.assertNotIn("Waits for", title("Discovery"))

    def test_the_card_says_what_was_not_drawn_and_why(self):
        self.hand_made()
        page = self.open()
        note = page.locator("#timeline-dep-note")
        expect(note).to_have_text("5 dependencies drawn; 3 dependencies not drawn because a task is not on the timeline.")
        self.assertEqual(page.locator("#timeline-dep-note ~ p", has_text="without dates").count(), 1)

    def test_one_link_is_spoken_of_in_the_singular(self):
        a, b = task(self.key, "A", "2026-10-05", "2026-10-06"), task(self.key, "B", "2026-10-07", "2026-10-08")
        c = task(self.key, "C")
        link(b, a)
        link(c, a)
        page = self.open()
        expect(page.locator("#timeline-dep-note")).to_have_text("1 dependency drawn; 1 dependency not drawn because a task is not on the timeline.")

    def test_dates_that_break_a_dependency_are_listed_and_open_the_task(self):
        self.hand_made()
        page = self.open()
        k = self.key
        rows = page.locator("#timeline-conflicts .item")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            [rows.nth(i).inner_text() for i in range(2)],
            [
                "%s-3 Build is planned to start on %s, but %s-2 Design (Finish \u2192 Start, 2d lag) allows it no earlier than %s." % (k, day("2026-10-14"), k, day("2026-10-25")),
                "%s-5 Launch is planned to finish on %s, but %s-3 Build (Finish \u2192 Finish) allows it no earlier than %s." % (k, day("2026-11-04"), k, day("2026-11-06")),
            ],
        )
        rows.nth(0).get_by_role("link").nth(1).click()  # the blocker's name
        expect(page.locator(".modal")).to_contain_text("%s-2" % k)

    def test_the_list_only_has_dates_that_matter(self):
        k = self.key
        a, b = task(k, "First", "2026-10-05", "2026-10-20"), task(k, "Second", "2026-10-12", "2026-10-30")
        link(b, a)  # Second starts before First is due: a conflict
        page = self.open()
        expect(page.locator("#timeline-conflicts .item")).to_have_count(1)
        for finished in (a, b):  # both done: the dates are history now
            self.assertEqual(api("mia", "PATCH", "/tasks/" + finished, {"status": "done"})[0], 200)
        page.reload()
        page.wait_for_selector("svg.chart")
        self.assertEqual(page.locator("#timeline-conflicts").count(), 0)
        self.assertEqual(page.locator(".tl-dep").count(), 1)
        self.assertEqual(page.locator(".tl-dep").get_attribute("data-state"), "ok")

    def test_a_project_without_links_looks_as_it_did_before(self):
        task(self.key, "Solo", "2026-10-05", "2026-10-06")
        page = self.open()
        self.assertEqual(page.locator(".tl-dep").count(), 0)
        self.assertEqual(page.get_by_label("Dependency arrows").count(), 0)
        self.assertEqual(page.locator("#timeline-dep-note, #timeline-conflicts, .sw-line").count(), 0)

    def test_viewers_and_members_see_the_arrows_too(self):
        self.hand_made()
        for user in ("val", "max"):
            page = self.open(user)
            self.assertEqual(page.locator(".tl-dep").count(), 5, user)
            self.assertEqual(page.locator("#timeline-conflicts .item").count(), 2, user)

    def test_hostile_titles_stay_text(self):
        k = self.key
        evil = '<img src=x onerror="window.pwned=1"> & "quotes"'
        a, b = task(k, evil, "2026-10-05", "2026-10-20"), task(k, "<b>bold</b>", "2026-10-12", "2026-10-30")
        link(b, a)
        page = self.open()
        self.assertEqual(page.locator("#timeline-conflicts img, #timeline-conflicts b").count(), 0)
        self.assertIn(evil, page.locator("#timeline-conflicts").inner_text())
        self.assertIn("<b>bold</b>", page.locator("#timeline-conflicts").inner_text())
        self.assertIsNone(page.evaluate("window.pwned"))
        self.assertIn(evil, page.locator(".tl-row", has_text="quotes").locator("rect.tl-bar title").text_content())

    def test_dark_mode_and_a_phone_window_work(self):
        self.hand_made()
        page = self.open(scheme="dark")
        self.assertEqual(page.locator(".tl-dep").count(), 5)
        stroke = page.locator(".tl-dep.conflict").first.evaluate("e => getComputedStyle(e).stroke")
        self.assertNotEqual(stroke, "none")
        page = self.open(viewport={"width": 390, "height": 800})
        self.assertEqual(page.locator(".tl-dep").count(), 5)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391, "the page itself does not scroll sideways")

    def test_when_the_dependencies_cannot_be_loaded_the_timeline_still_draws(self):
        self.hand_made()
        refuse = ("**/api/projects/*/dependencies", lambda r: r.fulfill(status=404, content_type="application/json", body='{"error":{"code":"x","message":"gone"}}'))
        page = self.open(route=refuse)
        self.assertEqual(page.locator(".tl-row").count(), 6)  # leaf tasks with dates that are not cancelled
        self.assertEqual(page.locator(".tl-dep").count(), 0)
        expect(page.locator("#timeline-dep-note")).to_have_text("The dependencies could not be loaded, so no arrows are drawn.")
        self.assertEqual(page.get_by_label("Dependency arrows").count(), 0)

    def test_no_stray_words_on_the_page(self):
        self.hand_made()
        page = self.open()
        self.assertIsNone(JUNK.search(page.locator(".card").inner_text()), page.locator(".card").inner_text())


class RandomProjectTests(Base):
    """Arrows have to land on the bars whatever the dates, directions and kinds of the links."""

    def test_random_projects(self):
        for seed in range(1, 5):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                key = "R%d" % next(PROJECTS)
                self.assertEqual(api("mia", "POST", "/projects", {"key": key, "name": "Random %d" % seed, "members": []})[0], 201)
                days = ["2026-10-%02d" % d for d in range(1, 29)] + ["2026-11-0%d" % d for d in range(1, 9)]
                keys = []
                for n in range(14):
                    start = rng.choice(days)
                    due = rng.choice([d for d in days if d >= start])
                    keys.append(task(key, "Task %d" % (n + 1), start, due))
                drawn = 0
                while drawn < 20:
                    a, b = rng.sample(keys, 2)
                    drawn += link(b, a, type=rng.choice(["finish_to_start", "start_to_start", "finish_to_finish"]), lag_days=rng.randint(-3, 5)) == 200
                page = self.open(key=key)
                data = self.read(page)
                self.assertEqual(len(data["arrows"]), 20)
                kinds = set()
                for arrow in data["arrows"]:
                    start, end = edge_points(arrow, data["bars"])
                    pts = arrow["pts"]
                    self.assertEqual((tuple(pts[0]), tuple(pts[-1])), (start, end), (seed, arrow["from"], arrow["to"], arrow["type"]))
                    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
                        self.assertTrue(x1 == x2 or y1 == y2)
                    top, bottom = sorted((data["bars"][arrow["from"]]["cy"], data["bars"][arrow["to"]]["cy"]))
                    self.assertTrue(all(top <= y <= bottom for _, y in pts), (seed, "left the rows it joins", arrow["from"], arrow["to"]))
                    (xa, _), (xb, _) = pts[-2], pts[-1]
                    self.assertTrue(xb < xa if arrow["type"] == "finish_to_finish" else xb > xa, (seed, arrow))
                    kinds.add(arrow["type"])
                self.assertEqual(kinds, {"finish_to_start", "start_to_start", "finish_to_finish"}, "the random links cover every kind")
