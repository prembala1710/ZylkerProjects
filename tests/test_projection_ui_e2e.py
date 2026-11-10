"""Browser end-to-end tests for the delivery projection (Playwright + Chromium; skipped when unavailable).

They drive the real dashboard, board, list, task dialog and Reports tab against the real threaded server. Every test builds
its own small team (so that nobody else's work competes for the same people) and works out the expected dates with a plain
loop over the calendar, independently of the scheduling engine. Any console error, CSP violation or uncaught exception fails
the test.
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
from datetime import date, timedelta

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

JUNK = re.compile(r"\bnull\b|\bundefined\b|\bNaN\b|\[object")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def setUpModule():
    global PW, BROWSER, SERVER, BASE, ADMIN, TMP, TODAY
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
        _, ADMIN = users.create_user(ctx, {"username": "prem", "display_name": "Prem B", "role": "admin"})
    SERVER = make_threaded_server(app, "127.0.0.1", 0)
    BASE = "http://127.0.0.1:%d" % SERVER.server_port
    threading.Thread(target=SERVER.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    TODAY = date.fromisoformat(api(ADMIN, "GET", "/meta")[1]["today"])


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


# -- the calendar, the slow and obvious way


def first_workday(day):
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def workday(base, n):
    """The date of working day ``n`` counted from ``base`` (0 = base itself)."""
    day = base
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def long_date(day):  # "Thu 26 Nov", as the server writes it in sentences
    return "%s %d %s%s" % (WEEKDAYS[day.weekday()], day.day, MONTHS[day.month - 1], "" if day.year == TODAY.year else " %d" % day.year)


def short_date(day):  # "26 Nov", as the interface writes it in tables
    return "%d %s%s" % (day.day, MONTHS[day.month - 1], "" if day.year == TODAY.year else " %d" % day.year)


def days(n):
    return "%d day%s" % (n, "" if n == 1 else "s")


TEAMS = iter(range(1, 10000))


class Team:
    """A manager and two members with their own accounts, and one project whose work they share."""

    def __init__(self, bob_hours=40, carol_hours=20, project_status="active", due=None):
        n = self.n = next(TEAMS)
        self.base = first_workday(TODAY)
        self.names = {"manager": "mgr%d" % n, "bob": "bob%d" % n, "carol": "carol%d" % n}
        self.tokens = {}
        for key, role, hours in (("manager", "manager", 40), ("bob", "member", bob_hours), ("carol", "member", carol_hours)):
            status, made = api(ADMIN, "POST", "/users", {"username": self.names[key], "display_name": "%s %d" % (key.capitalize(), n), "role": role, "weekly_capacity_hours": hours})
            assert status == 201, made
            self.tokens[key] = made["token"]
        self.key = "Q%d" % n
        self.name = "Projection %s" % self.key
        body = {"key": self.key, "name": self.name, "members": [self.names["bob"], self.names["carol"]]}
        if due:
            body["due_date"] = due.isoformat()
        status, project = api(self.tokens["manager"], "POST", "/projects", body)
        assert status == 201, project
        self.project_status = project_status

    def person(self, who):
        return self.names[who]

    def add(self, title, who, hours, due=None, **fields):
        body = {"title": title, "estimate_hours": hours, "assignee": self.names[who], **fields}
        if due:
            body["due_date"] = due.isoformat()
        status, task = api(self.tokens["manager"], "POST", "/projects/%s/tasks" % self.key, body)
        assert status == 201, task
        return task["key"]

    def block(self, task, blocked_by):
        assert api(self.tokens["manager"], "POST", "/tasks/%s/dependencies" % task, {"blocked_by": blocked_by})[0] == 200

    def late_scenario(self):
        """bob (8 h a day) has 24 h of work due after one working day; carol (4 h a day) reviews it afterwards.

        bob: T1 runs from day 0 to 3 and finishes on working day 2; T3 follows, finishes on day 3. carol: T2 waits for T1 and takes 2 days,
        finishing on working day 4. The project is due on working day 3.
        """
        self.t1 = self.add("Build the thing", "bob", 24, workday(self.base, 1))
        self.t2 = self.add("Review the thing", "carol", 8, workday(self.base, 3))
        self.t3 = self.add("Standalone chore", "bob", 8, workday(self.base, 30), priority="low")
        self.block(self.t2, self.t1)
        api(self.tokens["manager"], "PATCH", "/projects/" + self.key, {"due_date": workday(self.base, 3).isoformat()})
        b = self.base
        self.finish = {self.t1: workday(b, 2), self.t2: workday(b, 4), self.t3: workday(b, 3)}
        self.due = {self.t1: workday(b, 1), self.t2: workday(b, 3), self.t3: workday(b, 30)}
        self.late = {k: (self.finish[k] - self.due[k]).days for k in (self.t1, self.t2)}
        self.project_finish, self.project_due = workday(b, 4), workday(b, 3)
        self.project_late = (self.project_finish - self.project_due).days
        return self


    def fixable_scenario(self):
        """bob (8 h a day) has three jobs in a row, and the manager (8 h a day) and carol (4 h a day) could each take one.

        bob: the big job 24 h (t 0 -> 3, done on working day 2), the review 8 h (3 -> 4, day 3), the docs 8 h (4 -> 5, day 4). The project is due on
        day 3, so the docs make it a day late. Giving the big job to the manager ends everything on day 2; the review or the docs to carol, on day 3.
        """
        b = self.base
        self.big = self.add("Big job", "bob", 24, workday(b, 2))
        self.review = self.add("Review", "bob", 8, workday(b, 3))
        self.docs = self.add("Docs", "bob", 8, workday(b, 3))
        api(self.tokens["manager"], "PATCH", "/projects/" + self.key, {"due_date": workday(b, 3).isoformat()})
        self.finish_now, self.due = workday(b, 4), workday(b, 3)
        self.person = {"bob": "Bob %d" % self.n, "carol": "Carol %d" % self.n, "manager": "Manager %d" % self.n}
        return self

    def summary(self, task, title, receiver, finish, late_after):
        """The sentence the advice should say about giving ``task`` to ``receiver``, worked out from the dates above."""
        margin = (self.due - finish).days  # calendar days: a weekend in between counts
        how = "on the due date" if margin == 0 else "%s before the due date" % days(margin)
        return ("Give %s (%s) to %s instead of %s: the project would finish %s, %s, and %s; now it finishes %s, %s after the due date, and 1 task is late."
                % (task, title, self.person[receiver], self.person["bob"], long_date(finish), how, "no task would be late" if not late_after else "%d tasks would be late" % late_after,
                   long_date(self.finish_now), days((self.finish_now - self.due).days)))


class ProjectionUi(unittest.TestCase):
    viewport = {"width": 1360, "height": 1100}

    def setUp(self):
        self.problems, self.contexts = [], []

    def tearDown(self):
        for c in self.contexts:
            c.close()
        self.assertEqual(self.problems, [], "browser reported problems")

    def open(self, token, hash_, viewport=None, **context_args):
        context = BROWSER.new_context(viewport=viewport or self.viewport, **context_args)
        self.contexts.append(context)
        context.add_init_script("sessionStorage.setItem('zpm_token', %s)" % json.dumps(token))
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

    def reports(self, team, who="manager", **kw):
        page = self.open(team.tokens[who], "#/p/%s/reports" % team.key, **kw)
        expect(page.locator("#projection-headline")).to_be_visible()
        return page

    def recalculate(self, page, button="Recalculate"):
        """Press the button, wait for the answer to the question it asks and for the card to be drawn again, and return the response.

        The card is rebuilt when the answer arrives, so anything typed into it before that is lost: tests must not touch the form until then."""
        with page.expect_response(lambda r: re.search(r"/projection(\?|$)", r.url)) as answer:
            page.get_by_role("button", name=button).click()
        answer.value.finished()
        page.evaluate("new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        return answer.value

    def no_junk(self, page, selector):
        match = JUNK.search(page.inner_text(selector))
        self.assertIsNone(match, "stray %r in %s" % (match and match.group(), selector))


class ReportsTabTests(ProjectionUi):
    def test_the_card_says_what_will_slip_and_why(self):
        team = Team().late_scenario()
        page = self.reports(team)
        card = page.locator("#projection")
        expect(card).to_contain_text("Delivery projection")
        expect(card.locator(".badge.overdue")).to_have_text("Projected late")
        expect(page.locator("#projection-headline")).to_have_text(
            "Projected to finish %s, %s after the due date (%s). 2 of 3 open tasks will miss their own due dates." % (long_date(team.project_finish), days(team.project_late), long_date(team.project_due))
        )
        rows = card.locator("table tbody tr")
        expect(rows).to_have_count(2)  # the standalone chore is fine and is not listed
        first = rows.filter(has=page.locator('a[href="#/t/%s"]' % team.t1))
        expect(first).to_contain_text("Build the thing")
        expect(first).to_contain_text(team.names["bob"])
        expect(first).to_contain_text(short_date(team.due[team.t1]))
        expect(first).to_contain_text(short_date(team.finish[team.t1]))
        expect(first).to_contain_text("+%dd" % team.late[team.t1])
        expect(first).to_contain_text("Starts right away: Bob")
        second = rows.filter(has=page.locator('a[href="#/t/%s"]' % team.t2))
        expect(second).to_contain_text("Waits for %s (Build the thing), which finishes %s." % (team.t1, long_date(team.finish[team.t1])))
        expect(second).to_contain_text("+%dd" % team.late[team.t2])
        expect(card.get_by_role("link", name=team.t2, exact=True)).to_have_attribute("href", "#/t/" + team.t2)
        self.no_junk(page, "#projection")

    def test_who_is_carrying_the_work_and_how_it_is_worked_out(self):
        team = Team().late_scenario()
        page = self.reports(team)
        card = page.locator("#projection")
        expect(card).to_contain_text("Who is carrying the work")
        bob = card.locator(".item:not(.suggestion)", has_text="Bob")
        expect(bob).to_contain_text("32h open \u00b7 8h a day")  # 24 + 8
        expect(bob).to_contain_text("busy until %s" % short_date(team.finish[team.t3]))
        expect(card.locator(".item:not(.suggestion)", has_text="Carol")).to_contain_text("8h open \u00b7 4h a day")
        card.get_by_text("How this is worked out").click()
        expect(card).to_contain_text("one task at a time")
        expect(card).to_contain_text("Work days: Monday to Friday; there is no holiday calendar.")

    def test_milestones_at_risk_are_named(self):
        team = Team()
        status, ms = api(team.tokens["manager"], "POST", "/projects/%s/milestones" % team.key, {"name": "Beta", "due_date": workday(team.base, 1).isoformat()})
        self.assertEqual(status, 201)
        team.add("Beta work", "bob", 24, workday(team.base, 5), milestone=ms["id"])  # three days of work, milestone after one
        page = self.reports(team)
        expect(page.locator("#projection")).to_contain_text("Milestones at risk: Beta (due %s, projected %s)" % (short_date(workday(team.base, 1)), short_date(workday(team.base, 2))))

    def test_a_project_that_fits_says_so_and_lists_nothing_to_worry_about(self):
        team = Team(due=workday(first_workday(TODAY), 10))
        team.add("Easy", "bob", 8, workday(team.base, 8))
        page = self.reports(team)
        expect(page.locator("#projection .badge.on_track")).to_have_text("On track")
        expect(page.locator("#projection-headline")).to_contain_text("before the due date")
        expect(page.locator("#projection table")).to_have_count(0)

    def test_a_project_without_open_work_or_on_hold_is_not_projected(self):
        team = Team()
        page = self.reports(team)
        expect(page.locator("#projection-headline")).to_have_text("No open work: there is nothing to project.")
        expect(page.locator("#projection .badge.done")).to_have_text("No open work")
        team.add("Anything", "bob", 8)
        api(team.tokens["manager"], "PATCH", "/projects/" + team.key, {"status": "on_hold"})
        page.reload()
        expect(page.locator("#projection-headline")).to_have_text("Not projected: the project is on hold.")
        expect(page.locator("#projection .badge.on_hold")).to_have_text("Not projected")

    def test_work_that_cannot_be_scheduled_is_reported_not_hidden(self):
        team = Team(carol_hours=0)
        team.add("Needs carol", "carol", 8, workday(team.base, 3))
        page = self.reports(team)
        expect(page.locator("#projection .badge.at_risk")).to_have_text("Cannot be projected")
        expect(page.locator("#projection-headline")).to_contain_text("Cannot be projected completely: 1 task cannot be scheduled")
        expect(page.locator("#projection table tbody tr")).to_contain_text("has no capacity (0 hours a week)")
        expect(page.locator("#projection table tbody tr")).to_contain_text("n/a")

    def test_a_viewer_can_read_the_projection(self):
        team = Team().late_scenario()
        status, made = api(ADMIN, "POST", "/users", {"username": "view%s" % team.key.lower(), "display_name": "Viewer", "role": "viewer"})
        self.assertEqual(status, 201)
        page = self.reports(team, who="manager")  # the manager's session proves the card; the viewer's proves it is not hidden from them
        page2 = self.open(made["token"], "#/p/%s/reports" % team.key)
        expect(page2.locator("#projection-headline")).to_contain_text("Projected to finish")
        expect(page2.locator("#projection table tbody tr")).to_have_count(2)
        expect(page.locator("#projection-headline")).to_contain_text("Projected to finish")


class WhatIfTests(ProjectionUi):
    def test_less_availability_moves_the_finish_and_can_be_reset(self):
        team = Team().late_scenario()
        page = self.reports(team)
        baseline = page.locator("#projection-headline").inner_text()
        page.get_by_label("Person whose availability changes").select_option(team.names["bob"])
        page.get_by_label("Availability in percent").fill("50")
        with page.expect_request(lambda r: "/projection" in r.url) as sent:
            page.get_by_role("button", name="Recalculate").click()
        self.assertIn("capacity=%s%%3A50" % team.names["bob"], sent.value.url)  # "bob:50", and nothing about estimates that do not overrun
        self.assertNotIn("overrun", sent.value.url)
        # bob at 4 h a day: T1 takes 6 days (t 0 -> 6, working day 5), carol's review follows for two more (t 6 -> 8, working day 7)
        slower = workday(team.base, 7)
        moved = (slower - team.project_finish).days
        scenario = page.locator("#projection-scenario")
        expect(scenario).to_contain_text("With these assumptions: the finish moves later by %s" % days(moved))
        expect(page.locator("#projection-headline")).to_contain_text("Projected to finish %s" % long_date(slower))
        expect(page.locator("#projection .item:not(.suggestion)", has_text="Bob")).to_contain_text("(50% available)")
        with page.expect_request(lambda r: "/projection" in r.url) as reset:
            page.get_by_role("button", name="Reset").click()
        self.assertTrue(reset.value.url.endswith("/projection"), reset.value.url)  # no assumptions left in the question
        expect(scenario).to_have_count(0)
        expect(page.locator("#projection-headline")).to_have_text(baseline)
        expect(page.get_by_label("Availability in percent")).to_have_value("100")
        expect(page.get_by_label("Estimates overrun in percent")).to_have_value("0")
        expect(page.get_by_label("Person whose availability changes")).to_have_value("")

    def test_estimates_that_overrun_stretch_every_task(self):
        team = Team(bob_hours=40, carol_hours=40)
        team.add("Only task", "bob", 16, workday(team.base, 5))  # two days
        page = self.reports(team)
        expect(page.locator("#projection-headline")).to_contain_text("Projected to finish %s" % long_date(workday(team.base, 1)))
        page.get_by_label("Estimates overrun in percent").fill("100")  # four days
        page.get_by_role("button", name="Recalculate").click()
        expect(page.locator("#projection-headline")).to_contain_text("Projected to finish %s" % long_date(workday(team.base, 3)))
        expect(page.locator("#projection-scenario")).to_contain_text("the finish moves later by")

    def test_a_task_that_becomes_late_is_counted(self):
        team = Team(bob_hours=40, carol_hours=40)
        team.add("Tight", "bob", 8, workday(team.base, 1))  # one day of work, two days allowed
        page = self.reports(team)
        expect(page.locator("#projection table")).to_have_count(0)
        page.get_by_label("Estimates overrun in percent").fill("300")  # 32 hours: four days
        page.get_by_role("button", name="Recalculate").click()
        expect(page.locator("#projection-scenario")).to_contain_text("and 1 task more will be late.")
        expect(page.locator("#projection table tbody tr")).to_have_count(1)

    def test_a_question_the_browser_can_see_is_wrong_is_never_sent(self):
        team = Team().late_scenario()
        page = self.reports(team)
        headline = page.locator("#projection-headline").inner_text()
        asked = []
        page.on("request", lambda r: asked.append(r.url) if "/projection" in r.url else None)
        page.get_by_label("Person whose availability changes").select_option(team.names["carol"])
        availability = page.get_by_label("Availability in percent")
        availability.fill("250")
        page.get_by_role("button", name="Recalculate").click()
        self.assertTrue(availability.evaluate("e => e.validity.rangeOverflow"))  # the browser says so itself
        availability.fill("")
        page.get_by_role("button", name="Recalculate").click()
        self.assertTrue(availability.evaluate("e => e.validity.valueMissing"))  # a person without a percentage is not "0%"
        self.assertEqual(asked, [])
        expect(page.locator("#projection-headline")).to_have_text(headline)
        availability.fill("5e1")  # valid for the browser, and fifty: the server is asked for "carol:50", not for "carol:5e1"
        answer = self.recalculate(page)
        self.assertIn("capacity=%s%%3A50" % team.names["carol"], answer.request.url)
        expect(page.locator("#projection-scenario")).to_be_visible()
        expect(page.locator("#projection-error")).to_have_count(0)
        # the card was drawn again with an answer: the person is still chosen, so the percentage is still required
        availability = page.get_by_label("Availability in percent")
        availability.fill("")
        before = len(asked)
        page.get_by_role("button", name="Recalculate").click()
        self.assertTrue(availability.evaluate("e => e.validity.valueMissing"))
        self.assertEqual(len(asked), before)
        availability.fill("100")  # carol at her normal availability is not an assumption: only the overrun is asked
        page.get_by_label("Estimates overrun in percent").fill("10")
        answer = self.recalculate(page)
        self.assertIn("overrun=10", answer.request.url)
        self.assertNotIn("capacity", answer.request.url)
        page.get_by_label("Person whose availability changes").select_option("")  # nobody chosen: the percentage no longer matters
        page.get_by_label("Availability in percent").fill("")
        page.get_by_label("Estimates overrun in percent").fill("1e2")  # one hundred
        answer = self.recalculate(page)
        self.assertIn("overrun=100", answer.request.url)
        self.assertNotIn("capacity", answer.request.url)

    def test_a_slow_answer_to_an_old_question_never_replaces_a_newer_one(self):
        team = Team().late_scenario()
        page = self.reports(team)
        held = []
        page.route("**/projection?*overrun=100*", lambda route: held.append(route))  # the first question is held back at the door
        page.get_by_label("Estimates overrun in percent").fill("100")
        page.get_by_role("button", name="Recalculate").click()
        for _ in range(200):
            if held:
                break
            page.wait_for_timeout(25)
        self.assertEqual(len(held), 1)
        page.get_by_label("Estimates overrun in percent").fill("50")
        page.get_by_role("button", name="Recalculate").click()  # the second question is answered at once
        scenario = page.locator("#projection-scenario")
        expect(scenario).to_contain_text("With these assumptions")
        headline, said = page.locator("#projection-headline").inner_text(), scenario.inner_text()
        with page.expect_response(lambda r: "overrun=100" in r.url):
            held[0].continue_()  # now the old answer arrives, late
        page.evaluate("new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")  # and the page has had time to react
        expect(page.locator("#projection-headline")).to_have_text(headline)
        expect(scenario).to_have_text(said)

    def test_a_question_the_server_rejects_is_explained_and_the_last_answer_stays(self):
        team = Team().late_scenario()
        page = self.reports(team)
        headline = page.locator("#projection-headline").inner_text()
        page.get_by_label("Person whose availability changes").select_option(team.names["carol"])
        # carol leaves while the page is open: the page cannot know, the server does
        status, users_ = api(ADMIN, "GET", "/users")
        carol = next(u for u in users_["items"] if u["username"] == team.names["carol"])
        self.assertEqual(api(ADMIN, "PATCH", "/users/%d" % carol["id"], {"active": False})[0], 200)
        page.get_by_label("Availability in percent").fill("80")
        page.get_by_role("button", name="Recalculate").click()
        expect(page.locator("#projection-error")).to_contain_text("unknown or inactive user %r" % team.names["carol"])
        expect(page.locator("#projection-headline")).to_have_text(headline)  # the previous answer is still there
        expect(page.get_by_label("Availability in percent")).to_have_value("80")  # and so is the form, so it can be corrected
        page.get_by_label("Person whose availability changes").select_option("")
        page.get_by_role("button", name="Recalculate").click()
        expect(page.locator("#projection-error")).to_have_count(0)


class AdviceUiTests(ProjectionUi):
    def rows(self, page):
        return page.locator("#projection-advice .suggestion")

    def test_the_card_suggests_the_moves_that_help_best_first(self):
        team = Team().fixable_scenario()
        page = self.reports(team)
        expect(page.locator("#projection-headline")).to_contain_text("%s after the due date" % days((team.finish_now - team.due).days))
        advice = page.locator("#projection-advice")
        expect(advice).to_contain_text("What would help")
        rows = self.rows(page)
        expect(rows).to_have_count(3)
        b = team.base
        expect(rows.nth(0)).to_contain_text(team.summary(team.big, "Big job", "manager", workday(b, 2), 0))
        expect(rows.nth(1)).to_contain_text(team.summary(team.review, "Review", "carol", workday(b, 3), 0))  # equally good as the docs: by key
        expect(rows.nth(2)).to_contain_text(team.summary(team.docs, "Docs", "carol", workday(b, 3), 0))
        expect(rows.nth(0)).to_contain_text("%s has 0h of other work." % team.person["manager"])
        expect(rows.nth(0).get_by_role("link", name="Open %s" % team.big)).to_have_attribute("href", "#/t/" + team.big)
        expect(advice).to_contain_text("Suggestions look only at capacity and queues, not at skills.")
        expect(advice).to_contain_text("Nothing is changed until you reassign the task yourself.")
        self.no_junk(page, "#projection")
        self.assertEqual(api(team.tokens["manager"], "GET", "/tasks/" + team.big)[1]["assignee"]["username"], team.names["bob"])  # looking is not doing

    def test_previewing_a_suggestion_shows_what_it_does_and_reset_brings_the_suggestions_back(self):
        team = Team().fixable_scenario()
        page = self.reports(team)
        baseline = page.locator("#projection-headline").inner_text()
        expect(self.rows(page)).to_have_count(3)
        with page.expect_request(lambda r: "/projection?" in r.url) as sent:
            self.rows(page).nth(0).get_by_role("button", name="Preview giving %s to %s" % (team.big, team.person["manager"])).click()
        self.assertIn("reassign=%s%%3A%s" % (team.big, team.names["manager"]), sent.value.url)
        earlier = (team.finish_now - workday(team.base, 2)).days
        expect(page.locator("#projection-scenario")).to_have_text("With these assumptions: %s goes to %s; the finish moves earlier by %s, and 1 task fewer will be late."
                                                                  % (team.big, team.person["manager"], days(earlier)))
        expect(page.locator("#projection-headline")).to_contain_text("Projected to finish %s" % long_date(workday(team.base, 2)))
        expect(page.locator("#projection-advice")).to_have_count(0)  # the question has changed: the old suggestions are not shown against the new picture
        page.get_by_role("button", name="Reset").click()
        expect(page.locator("#projection-headline")).to_have_text(baseline)
        expect(page.locator("#projection-scenario")).to_have_count(0)
        expect(self.rows(page)).to_have_count(3)

    def test_a_preview_can_be_combined_with_the_other_assumptions(self):
        team = Team().fixable_scenario()
        page = self.reports(team)
        expect(self.rows(page)).to_have_count(3)
        self.rows(page).nth(0).get_by_role("button", name="Preview giving %s to %s" % (team.big, team.person["manager"])).click()
        expect(page.locator("#projection-scenario")).to_contain_text("goes to")
        page.get_by_label("Estimates overrun in percent").fill("100")
        with page.expect_request(lambda r: "/projection?" in r.url) as sent:
            page.get_by_role("button", name="Recalculate").click()
        self.assertIn("overrun=100", sent.value.url)
        self.assertIn("reassign=", sent.value.url)  # the move is still part of the question
        expect(page.locator("#projection-scenario")).to_contain_text("%s goes to %s;" % (team.big, team.person["manager"]))

    def test_no_suggestions_are_shown_against_a_what_if_that_still_has_late_work(self):
        team = Team().fixable_scenario()
        page = self.reports(team)
        expect(self.rows(page)).to_have_count(3)
        page.get_by_label("Estimates overrun in percent").fill("100")  # everything takes twice as long: more is late, so there would be something to fix
        page.get_by_role("button", name="Recalculate").click()
        expect(page.locator("#projection-scenario")).to_be_visible()
        expect(page.locator("#projection table tbody tr")).not_to_have_count(0)
        expect(page.locator("#projection-advice")).to_have_count(0)  # the suggestions were worked out for the plan as it stands, not for this one

    def test_nothing_to_fix_means_no_suggestions_box(self):
        team = Team(due=workday(first_workday(TODAY), 10))
        team.add("Easy", "bob", 8, workday(team.base, 8))
        page = self.reports(team)
        expect(page.locator("#projection .badge.on_track")).to_have_text("On track")
        expect(page.locator("#projection-advice")).to_have_count(0)

    def test_when_no_move_helps_the_card_says_so(self):
        team = Team()
        team.add("Impossible", "bob", 24, workday(team.base, 0))  # three days of work due today: nobody else is any faster
        page = self.reports(team)
        expect(page.locator("#projection-advice")).to_contain_text("No single reassignment to another member of the project would help.")
        expect(self.rows(page)).to_have_count(0)

    def test_a_failing_advice_request_does_not_take_the_card_down(self):
        team = Team().fixable_scenario()
        page = self.open(team.tokens["manager"], "#/p/%s/reports" % team.key)
        page.route("**/projection/advice*", lambda route: route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": {"code": "x", "message": "Too many open tasks for advice"}})))
        page.reload()
        expect(page.locator("#projection-headline")).to_contain_text("Projected to finish")
        expect(page.locator("#projection-advice")).to_contain_text("Suggestions are not available: Too many open tasks for advice")
        expect(page.locator("#projection table tbody tr")).to_have_count(1)  # the late task is still listed with its reason

    def test_hostile_titles_in_a_suggestion_are_text(self):
        team = Team()
        hostile = '<img src=x onerror="window.__pwned=1"> *bold*'
        for title, hours in (("Big", 24), (hostile, 8), ("Docs", 8)):
            api(team.tokens["manager"], "POST", "/projects/%s/tasks" % team.key, {"title": title, "estimate_hours": hours, "assignee": team.names["bob"], "due_date": workday(team.base, 3 if hours == 8 else 2).isoformat()})
        api(team.tokens["manager"], "PATCH", "/projects/" + team.key, {"due_date": workday(team.base, 3).isoformat()})
        page = self.reports(team)
        expect(page.locator("#projection-advice")).to_contain_text(hostile)
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertEqual(page.locator("#projection img").count(), 0)

    def test_suggestions_fit_a_phone(self):
        team = Team().fixable_scenario()
        page = self.reports(team, viewport={"width": 390, "height": 800})
        expect(self.rows(page)).to_have_count(3)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)
        expect(self.rows(page).nth(0).get_by_role("button", name="Preview giving %s to %s" % (team.big, team.person["manager"]))).to_be_visible()


class FlagsEverywhereTests(ProjectionUi):
    def test_the_board_and_the_list_flag_late_tasks_and_only_those(self):
        team = Team().late_scenario()
        page = self.open(team.tokens["manager"], "#/p/%s/board" % team.key)
        for key in (team.t1, team.t2):
            chip = page.locator(".task-card", has_text=key).locator(".chip.late")
            expect(chip).to_have_count(1)
            expect(chip).to_have_attribute("aria-label", "Projected %s late" % days(team.late[key]))
            expect(chip).to_have_text("\u23F1 +%dd" % team.late[key])
        expect(page.locator(".task-card", has_text=team.t2).locator(".chip.late")).to_have_attribute("title", re.compile("Waits for %s" % team.t1))
        expect(page.locator(".task-card", has_text=team.t3).locator(".chip.late")).to_have_count(0)
        page.get_by_role("link", name="List", exact=True).click()
        expect(page.locator("table.table tbody tr", has_text=team.t1).locator(".chip.late")).to_have_count(1)
        expect(page.locator("table.table tbody tr", has_text=team.t3).locator(".chip.late")).to_have_count(0)

    def test_finishing_the_blocking_work_clears_the_warnings_without_a_reload(self):
        team = Team().late_scenario()
        page = self.open(team.tokens["manager"], "#/p/%s/board" % team.key)
        expect(page.locator(".chip.late")).to_have_count(2)
        page.locator(".task-card", has_text="Build the thing").drag_to(page.locator("section.column[data-status=done]"))
        expect(page.locator(".toast.ok", has_text="moved to Done")).to_be_visible()
        # with T1 done, carol's review starts at once (t 0 -> 2, working day 1), well before its due date; the finished card carries no chip
        expect(page.locator(".chip.late")).to_have_count(0)
        expect(page.locator("section.column[data-status=done] .task-card", has_text="Build the thing")).to_have_count(1)
        header = page.locator(".page-head")
        expect(header).to_be_visible()

    def test_the_dashboard_shows_the_projected_finish_of_every_project(self):
        team = Team().late_scenario()
        page = self.open(team.tokens["manager"], "#/dashboard")
        row = page.locator("table.table tbody tr", has_text=team.name)
        expect(row).to_contain_text(short_date(team.project_finish))
        expect(row).to_contain_text("%s after the due date" % days(team.project_late))
        expect(row).to_contain_text("2 tasks will be late")
        expect(page.locator("table.table thead")).to_contain_text("Projected finish")
        fine = Team(due=workday(first_workday(TODAY), 10))
        fine.add("Easy", "bob", 8, workday(fine.base, 8))
        page.reload()
        ok = page.locator("table.table tbody tr", has_text=fine.name)
        expect(ok).to_contain_text("in time")
        expect(ok).not_to_contain_text("will be late")
        empty = Team()
        page.reload()
        expect(page.locator("table.table tbody tr", has_text=empty.name).locator("td").last).to_have_text("\u2014")

    def test_when_the_projection_is_refused_the_dashboard_and_the_project_still_open_and_say_why(self):
        from unittest import mock

        from zylker_pm.services import projection

        team = Team().late_scenario()
        with mock.patch.object(projection, "MAX_TASKS", 1):  # the server runs in this process, so the limit applies to it too
            page = self.open(team.tokens["manager"], "#/dashboard")
            expect(page.locator("#projection-unavailable")).to_contain_text("Projected finish is not available: There are ")
            expect(page.locator("table.table tbody tr", has_text=team.name)).to_be_visible()  # the portfolio itself is still there
            page.goto(BASE + "/#/p/%s/board" % team.key)
            expect(page.locator(".task-card", has_text="Build the thing")).to_be_visible()
            expect(page.locator(".chip.late")).to_have_count(0)  # no projection, so no chips, and no errors in the console either
            page.goto(BASE + "/#/p/%s/reports" % team.key)
            expect(page.locator("#projection .error-text")).to_contain_text("a projection handles at most 1")

    def test_the_task_dialog_shows_the_projection_and_hides_it_when_it_no_longer_applies(self):
        team = Team().late_scenario()
        page = self.open(team.tokens["manager"], "#/t/" + team.t2)
        section = page.locator("#projection-section")
        expect(section).to_contain_text("Projected to finish %s \u2014 %s after its due date" % (long_date(team.finish[team.t2]), days(team.late[team.t2])))
        expect(section).to_contain_text("Waits for %s (Build the thing), which finishes %s." % (team.t1, long_date(team.finish[team.t1])))
        ok = page.locator(".modal").first.get_by_label("Status")
        expect(ok).to_be_visible()
        page.keyboard.press("Escape")
        page.goto(BASE + "/#/t/" + team.t3)
        section = page.locator("#projection-section")
        expect(section).to_contain_text("Projected to finish %s \u2014 %s before its due date" % (long_date(team.finish[team.t3]), days((team.due[team.t3] - team.finish[team.t3]).days)))
        page.locator(".modal").first.get_by_label("Status").select_option("done")
        expect(page.locator("#projection-section")).to_have_count(0)  # a finished task has no projection

    def test_changing_an_estimate_in_the_dialog_updates_the_projection_at_once(self):
        team = Team(bob_hours=40, carol_hours=40)
        key = team.add("Growing", "bob", 8, workday(team.base, 3))
        page = self.open(team.tokens["manager"], "#/t/" + key)
        expect(page.locator("#projection-section")).to_contain_text("Projected to finish %s" % long_date(team.base))
        page.get_by_label("Estimate (hours)").fill("40")
        page.get_by_label("Estimate (hours)").press("Tab")
        expect(page.locator("#projection-section")).to_contain_text("Projected to finish %s" % long_date(workday(team.base, 4)))
        expect(page.locator("#projection-section")).to_contain_text("%s after its due date" % days((workday(team.base, 4) - workday(team.base, 3)).days))


class SafetyAndLayoutTests(ProjectionUi):
    def test_hostile_titles_and_names_are_shown_as_text_never_executed(self):
        team = Team()
        status, task = api(team.tokens["manager"], "POST", "/projects/%s/tasks" % team.key, {
            "title": '<img src=x onerror="window.__pwned=1"> late', "estimate_hours": 40, "assignee": team.names["bob"], "due_date": workday(team.base, 1).isoformat()})
        self.assertEqual(status, 201, task)
        page = self.reports(team)
        expect(page.locator("#projection table tbody tr")).to_contain_text('<img src=x onerror="window.__pwned=1"> late')
        self.assertIsNone(page.evaluate("window.__pwned"))
        self.assertEqual(page.locator("#projection img").count(), 0)
        page.goto(BASE + "/#/p/%s/board" % team.key)
        expect(page.locator(".chip.late")).to_have_count(1)
        self.assertIsNone(page.evaluate("window.__pwned"))

    def test_the_card_fits_a_phone(self):
        team = Team().late_scenario()
        page = self.reports(team, viewport={"width": 390, "height": 800})
        expect(page.locator("#projection table")).to_be_visible()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)
        page.goto(BASE + "/#/dashboard")
        expect(page.locator("table.table", has_text="Projected finish")).to_be_visible()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)

    def test_the_warning_chip_and_the_card_work_in_dark_mode(self):
        team = Team().late_scenario()
        page = self.open(team.tokens["manager"], "#/p/%s/board" % team.key, color_scheme="dark")
        chip = page.locator(".task-card", has_text=team.t1).locator(".chip.late")
        expect(chip).to_have_css("color", "rgb(255, 122, 112)")  # the dark theme's red
        expect(chip).to_have_css("background-color", "rgb(61, 26, 24)")


if __name__ == "__main__":
    unittest.main()
