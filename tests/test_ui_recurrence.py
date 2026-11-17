"""Browser tests for recurring tasks (they reuse the server and browser fixtures of test_ui_e2e)."""

import test_ui_e2e as base
from playwright.sync_api import expect

# Each module gets its own server and database, so these tests cannot disturb the counts other browser tests rely on.
setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

api = lambda *a, **k: base.api(*a, **k)  # noqa: E731 - base.BASE is only known once the module fixture has run


class RecurrenceUiTests(base.UiCase):
    def setUp(self):
        super().setUp()
        status, _ = api(base.TOKENS["prem"], "POST", "/projects", {"key": "RCR", "name": "Recurring lab", "members": ["alice", "bob"]})
        self.assertEqual(status, 201)
        self.addCleanup(lambda: api(base.TOKENS["prem"], "DELETE", "/projects/RCR?confirm=RCR"))

    def rules(self):
        return api(base.TOKENS["alice"], "GET", "/projects/RCR/recurrences")[1]["items"]

    def open_settings(self, user="alice"):
        page = self.open(user, "#/p/RCR/settings")
        self.settle(page)
        return page

    def test_manager_creates_pauses_and_deletes_a_rule_and_the_generator_makes_the_task(self):
        page = self.open_settings()
        expect(page.get_by_text("No recurring tasks yet.")).to_be_visible()
        page.get_by_role("button", name="+ New recurring task").click()
        dialog = page.locator(".modal")
        dialog.get_by_label("Title").fill("Stand-up notes {date}")
        dialog.get_by_label("Repeat").select_option("daily")
        dialog.get_by_label("Assignee").select_option("bob")
        expect(dialog.locator("[role=status]")).to_contain_text("Every day. Next:")  # the live preview
        dialog.get_by_role("button", name="Create").click()
        expect(page.locator(".toast.ok")).to_contain_text("Recurring task created")
        row = page.locator(".recurrence-list .item", has_text="Stand-up notes")
        expect(row).to_contain_text("Every day")
        expect(row).to_contain_text("Next:")
        self.assertEqual([r["assignee"] for r in self.rules()], ["bob"])
        # the generator (a cron job or the server's own timer in real life) creates today's task
        status, report = api(base.TOKENS["prem"], "POST", "/recurrences/run")
        self.assertEqual((status, len(report["created"])), (200, 1))
        page.reload()
        self.settle(page)
        row = page.locator(".recurrence-list .item", has_text="Stand-up notes")
        expect(row).to_contain_text("1 task created")
        row.get_by_role("button", name="History").click()
        expect(page.locator(".modal")).to_contain_text("Created")
        expect(page.locator(".modal a", has_text="RCR-1")).to_be_visible()
        page.keyboard.press("Escape")
        row.get_by_role("button", name="Pause").click()
        expect(row.get_by_role("button", name="Resume")).to_be_visible()
        expect(row).to_contain_text("Paused")
        row.get_by_role("button", name="Delete").click()
        page.get_by_role("button", name="Delete", exact=True).last.click()
        expect(page.get_by_text("No recurring tasks yet.")).to_be_visible()
        self.assertEqual(api(base.TOKENS["alice"], "GET", "/tasks/RCR-1")[0], 200)  # the task it made stays

    def test_the_form_explains_a_schedule_that_cannot_work(self):
        page = self.open_settings()
        page.get_by_role("button", name="+ New recurring task").click()
        dialog = page.locator(".modal")
        dialog.get_by_label("Title").fill("No days chosen")
        expect(dialog.locator("[role=status]")).to_contain_text("Every week on")  # starts valid: today's weekday is ticked
        for box in dialog.get_by_role("group", name="Days of the week").get_by_role("checkbox").all():
            box.uncheck()
        expect(dialog.locator("[role=status]")).to_contain_text("at least one weekday")  # weekly, nothing ticked
        dialog.get_by_role("button", name="Create").click()
        expect(page.locator(".toast.error")).to_contain_text("at least one weekday")
        dialog.get_by_label("Mon", exact=True).check()
        expect(dialog.locator("[role=status]")).to_contain_text("Every week on Mon")
        dialog.get_by_label("Repeat").select_option("monthly")
        dialog.get_by_label("Day of the month").select_option("-1")
        expect(dialog.locator("[role=status]")).to_contain_text("Every month on the last day")
        self.assertEqual(self.rules(), [])  # nothing was saved by the failed attempt

    def test_viewers_and_members_can_read_but_not_change(self):
        api(base.TOKENS["alice"], "POST", "/projects/RCR/recurrences", {"title": "Read only", "frequency": "daily", "start_date": "2026-01-01"})
        for user in ("erin", "bob"):
            page = self.open_settings(user)
            row = page.locator(".recurrence-list .item", has_text="Read only")
            expect(row).to_be_visible()
            expect(row.get_by_role("button", name="History")).to_be_visible()
            for name in ("Edit", "Pause", "Delete"):
                expect(row.get_by_role("button", name=name)).to_have_count(0)
            expect(page.get_by_role("button", name="+ New recurring task")).to_have_count(0)
            self.assert_clean_text(page, "settings (%s)" % user)

    def test_hostile_titles_are_shown_as_text_and_in_the_audit_log(self):
        evil = "<img src=x onerror=window.__pwned=1> {date}"
        status, _ = api(base.TOKENS["alice"], "POST", "/projects/RCR/recurrences", {"title": evil, "frequency": "daily", "start_date": "2026-01-01"})
        self.assertEqual(status, 201)
        api(base.TOKENS["prem"], "POST", "/recurrences/run")
        page = self.open_settings("prem")  # an admin, who can also read the audit log
        expect(page.locator(".recurrence-list .item").first).to_contain_text("<img src=x")
        self.assertEqual(page.locator(".recurrence-list img").count(), 0)
        page.evaluate("location.hash = '#/audit'")
        self.settle(page)
        page.get_by_label("Category").select_option("recurrence")
        expect(page.locator(".audit-row", has_text="set up the recurring task").first).to_be_visible()
        expect(page.locator(".audit-row", has_text="from the recurring task").first).to_be_visible()
        self.assertEqual(page.evaluate("typeof window.__pwned"), "undefined")
        self.assertEqual(page.locator(".audit-list img").count(), 0)
        self.assert_clean_text(page, "audit log (recurrence)")

    def test_the_settings_tab_holds_together_on_a_phone(self):
        api(base.TOKENS["alice"], "POST", "/projects/RCR/recurrences", {"title": "L" * 190, "frequency": "weekly", "weekdays": ["mon", "tue", "wed", "thu", "fri"], "start_date": "2026-01-01"})
        page = self.open("alice", "#/p/RCR/settings", viewport={"width": 390, "height": 800})
        self.settle(page)
        expect(page.locator(".recurrence-list .item").first).to_be_visible()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)
        page.get_by_role("button", name="+ New recurring task").click()
        expect(page.locator(".modal")).to_be_visible()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"), 1)
