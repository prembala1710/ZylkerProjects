"""Task listing: filters, sorting, paging, search, the board - and hostile input."""

import sqlite3

from helpers import ApiTestCase


class ListingTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.make_project("API", members=("bob",))
        mk = self.make_task
        mk("WEB", "Alpha login", priority="low", assignee="bob", due_date="2026-10-10", labels=["auth"])  # WEB-1 overdue
        mk("WEB", "Beta signup", priority="critical", assignee="carol", due_date="2026-10-30", labels=["auth", "ui"])  # WEB-2
        mk("WEB", "Gamma 100% done_ish", priority="high", due_date=None)  # WEB-3 unassigned, no due
        mk("API", "Delta endpoint", priority="medium", assignee="bob", due_date="2026-10-16")  # API-1 due today
        self.set_status("WEB-2", "in_progress")
        self.set_status("API-1", "done")
        self.get = lambda **q: self.ok(self.client("bob").get("/api/tasks", query=q))
        self.keys = lambda **q: [t["key"] for t in self.get(**q)["items"]]

    def test_default_order_is_by_project_then_number_with_totals(self):
        page = self.get()
        self.assertEqual([t["key"] for t in page["items"]], ["API-1", "WEB-1", "WEB-2", "WEB-3"])
        self.assertEqual((page["total"], page["limit"], page["offset"]), (4, 50, 0))

    def test_filters(self):
        self.assertEqual(self.keys(project="WEB"), ["WEB-1", "WEB-2", "WEB-3"])
        self.assertEqual(self.keys(project="web,api"), ["API-1", "WEB-1", "WEB-2", "WEB-3"])
        self.assertEqual(self.keys(status="in_progress,done"), ["API-1", "WEB-2"])
        self.assertEqual(self.keys(open="true"), ["WEB-1", "WEB-2", "WEB-3"])
        self.assertEqual(self.keys(closed="true"), ["API-1"])
        self.assertEqual(self.keys(priority="critical,high"), ["WEB-2", "WEB-3"])
        self.assertEqual(self.keys(assignee="bob"), ["API-1", "WEB-1"])
        self.assertEqual(self.keys(assignee="BOB"), ["API-1", "WEB-1"])  # usernames are case-insensitive
        self.assertEqual(self.keys(assignee="me"), ["API-1", "WEB-1"])  # requester is bob
        self.assertEqual(self.keys(assignee="none"), ["WEB-3"])
        self.assertEqual(self.keys(assignee="carol,none"), ["WEB-2", "WEB-3"])
        self.assertEqual(self.keys(label="ui"), ["WEB-2"])
        self.assertEqual(self.keys(label="auth"), ["WEB-1", "WEB-2"])
        self.assertEqual(self.keys(reporter="alice"), ["API-1", "WEB-1", "WEB-2", "WEB-3"])
        self.assertEqual(self.keys(type="bug"), [])

    def test_date_filters_and_overdue(self):
        self.assertEqual(self.keys(overdue="true"), ["WEB-1"])  # API-1 is due today (and done)
        self.assertEqual(self.keys(due_before="2026-10-16"), ["API-1", "WEB-1"])
        self.assertEqual(self.keys(due_after="2026-10-16"), ["API-1", "WEB-2"])
        self.assertEqual(self.client("bob").get("/api/tasks", query={"due_before": "tomorrow"}).status, 422)

    def test_text_search_matches_title_description_and_exact_key(self):
        self.assertEqual(self.keys(q="signup"), ["WEB-2"])
        self.assertEqual(self.keys(q="ALPHA"), ["WEB-1"])
        self.assertEqual(self.keys(q="web-3"), ["WEB-3"])
        self.assertEqual(self.keys(q="100%"), ["WEB-3"])  # % is a literal, not a wildcard
        self.assertEqual(self.keys(q="done_ish"), ["WEB-3"])  # so is _
        self.assertEqual(self.keys(q="%"), ["WEB-3"])
        self.assertEqual(self.keys(q="nothing-like-this"), [])

    def test_sorting(self):
        self.assertEqual(self.keys(sort="-priority"), ["WEB-2", "WEB-3", "API-1", "WEB-1"])
        self.assertEqual(self.keys(sort="priority"), ["WEB-1", "API-1", "WEB-3", "WEB-2"])
        self.assertEqual(self.keys(sort="due_date"), ["WEB-1", "API-1", "WEB-2", "WEB-3"])  # nulls last
        self.assertEqual(self.keys(sort="-due_date"), ["WEB-2", "API-1", "WEB-1", "WEB-3"])  # still last
        self.assertEqual(self.keys(sort="status,key"), ["WEB-1", "WEB-3", "WEB-2", "API-1"])
        # bob owns WEB-1 and API-1: ties fall back to creation order, then unassigned goes last
        self.assertEqual(self.keys(sort="assignee"), ["WEB-1", "API-1", "WEB-2", "WEB-3"])
        self.assertEqual(self.keys(sort="-title"), ["WEB-3", "API-1", "WEB-2", "WEB-1"])  # Gamma, Delta, Beta, Alpha

    def test_paging(self):
        page = self.get(limit=2, offset=1)
        self.assertEqual([t["key"] for t in page["items"]], ["WEB-1", "WEB-2"])
        self.assertEqual(page["total"], 4)
        self.assertEqual(self.get(limit=500)["limit"], 200)  # capped
        for bad in ({"limit": "0"}, {"limit": "x"}, {"offset": "-1"}):
            self.assertEqual(self.client("bob").get("/api/tasks", query=bad).status, 422, bad)

    def test_subtask_filters(self):
        self.make_task("WEB", "Child", parent="WEB-1")  # WEB-4
        self.assertEqual(self.keys(parent="WEB-1"), ["WEB-4"])
        self.assertEqual(self.keys(parent="none", project="WEB"), ["WEB-1", "WEB-2", "WEB-3"])
        self.assertEqual(self.keys(leaf="true", project="WEB"), ["WEB-2", "WEB-3", "WEB-4"])
        self.assertEqual(self.keys(parent="WEB-77"), [])

    def test_unknown_filter_values_are_422_not_silently_ignored(self):
        for bad in ({"status": "wip"}, {"priority": "p0"}, {"type": "chore"}, {"sort": "password"}, {"milestone": "abc"}):
            r = self.client("bob").get("/api/tasks", query=bad)
            self.assertEqual((r.status, r.error_code), (422, "validation_error"), bad)

    def test_hostile_input_cannot_reach_the_sql_layer(self):
        evil = "x'; DROP TABLE tasks; --"
        for params in ({"q": evil}, {"label": evil}, {"assignee": evil}, {"project": evil}, {"external_ref": evil}):
            self.assertEqual(self.get(**params)["total"], 0, params)
        for params in ({"sort": "title; DROP TABLE tasks"}, {"sort": "(SELECT 1)"}, {"status": evil}):
            self.assertEqual(self.client("bob").get("/api/tasks", query=params).status, 422, params)
        conn = sqlite3.connect(self.config.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 4)
        conn.close()
        t = self.make_task("WEB", evil)  # stored verbatim, returned verbatim
        self.assertEqual(self.ok(self.client("bob").get("/api/tasks/" + t["key"]))["title"], evil)


class BoardTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB")
        self.alice = self.client("alice")

    def board(self, **q):
        return self.ok(self.alice.get("/api/projects/WEB/board", query=q))

    def test_columns_follow_the_workflow_and_sort_by_priority_then_due_date(self):
        self.make_task("WEB", "low", priority="low")
        self.make_task("WEB", "crit late", priority="critical", due_date="2026-11-30")
        self.make_task("WEB", "crit soon", priority="critical", due_date="2026-10-20")
        self.make_task("WEB", "doing")
        self.set_status("WEB-4", "in_progress")
        b = self.board()
        self.assertEqual([c["status"] for c in b["columns"]], ["todo", "in_progress", "in_review", "blocked", "done", "cancelled"])
        self.assertEqual([t["key"] for t in b["columns"][0]["tasks"]], ["WEB-3", "WEB-2", "WEB-1"])
        self.assertEqual([t["key"] for t in b["columns"][1]["tasks"]], ["WEB-4"])
        self.assertFalse(b["truncated"])

    def test_old_closed_tasks_are_hidden_but_counted(self):
        self.make_task("WEB", "old")
        self.make_task("WEB", "recent")
        self.set_status("WEB-1", "done")
        self.advance_days(30)
        self.set_status("WEB-2", "done")
        done = lambda b: [t["key"] for t in b["columns"][4]["tasks"]]  # noqa: E731
        b = self.board()  # default: last 14 days
        self.assertEqual((done(b), b["hidden_closed"]), (["WEB-2"], 1))
        b = self.board(closed_days=0)
        self.assertEqual((done(b), b["hidden_closed"]), (["WEB-1", "WEB-2"], 0))
        self.assertEqual(self.alice.get("/api/projects/WEB/board", query={"closed_days": "x"}).status, 422)
