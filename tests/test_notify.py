"""Cliq notifications: message rendering, delivery over HTTP, failure isolation, config checks."""

from helpers import ApiTestCase, CaptureServer

from zylker_pm import notify
from zylker_pm.app import App
from zylker_pm.config import Config


class RenderTests(ApiTestCase):
    def test_every_event_renders_and_unknown_ones_do_not(self):
        base = {"key": "WEB-1", "title": "Fix login", "actor": "Alice"}
        out = notify.render("task_assigned", {**base, "assignee": "Bob", "priority": "high", "due_date": "2026-10-20"})
        self.assertEqual(out, "\U0001f4cc *WEB-1* Fix login\nAssigned to *Bob* by Alice \u00b7 priority high \u00b7 due 2026-10-20")
        self.assertIn("completed by Alice", notify.render("task_status", {**base, "new": "done"}))
        self.assertIn("marked blocked by Alice", notify.render("task_status", {**base, "new": "blocked"}))
        self.assertIsNone(notify.render("task_status", {**base, "new": "in_progress"}))
        self.assertIn("mentioned Bob", notify.render("mention", {**base, "mentioned": "Bob", "excerpt": "hi"}))
        self.assertIn("moved from active to on_hold", notify.render("project_status_changed", {"name": "Web", "project": "WEB", "old": "active", "new": "on_hold", "actor": "Alice"}))
        self.assertIsNone(notify.render("something_else", base))

    def test_links_use_the_public_url(self):
        out = notify.render("task_status", {"key": "WEB-7", "title": "T", "actor": "A", "new": "done"}, "https://pm.example.com")
        self.assertIn("[WEB-7](https://pm.example.com/#/t/WEB-7)", out)

    def test_user_text_cannot_inject_markup_links_or_mentions(self):
        out = notify.render("task_status", {"key": "WEB-1", "title": "*bold* [click](http://evil.example) {@all} <b>x</b> `c` ~s~ _i_", "actor": "A", "new": "done"})
        for ch in "[]{}<>`~_":
            self.assertNotIn(ch, out.split("\u2014")[0].replace("*WEB-1*", ""), ch)
        self.assertNotIn("](", out)
        self.assertNotIn("{@all}", out)

    def test_a_huge_digest_is_capped_below_the_cliq_limit(self):
        group = {"assignee": {"display_name": "X"}, "due_soon": [],
                 "overdue": [{"key": "A-%d" % i, "title": "t" * 100, "days_overdue": 1} for i in range(200)]}  # fmt: skip
        report = {"as_of": "2026-10-16", "days": 3, "overdue_count": 200, "due_soon_count": 0, "groups": [group]}
        text = notify.render_digest(report)
        self.assertEqual(len(text), notify.MAX_MESSAGE)
        self.assertLess(notify.MAX_MESSAGE, 5000)  # Cliq rejects anything longer

    def test_digest_rendering(self):
        self.make_project("DG", members=("bob",))
        self.make_task("DG", "Late one", assignee="bob", due_date="2026-10-14")
        self.make_task("DG", "Orphan", due_date="2026-10-16")
        with self.app.session(write=False) as ctx:
            from zylker_pm.services import reports

            text = notify.render_digest(reports.digest(ctx, {}), "https://pm.example.com")
        self.assertIn("*Task digest for 2026-10-16*", text)
        self.assertIn("*Bob*", text)
        self.assertIn("[DG-1](https://pm.example.com/#/t/DG-1) Late one \u2014 2 day(s) overdue", text)
        self.assertIn("*Unassigned*", text)
        self.assertIn("due today", text)
        self.assertTrue(text.endswith("1 overdue \u00b7 1 due soon"))
        empty = notify.render_digest({"as_of": "2026-10-16", "days": 3, "overdue_count": 0, "due_soon_count": 0, "groups": []})
        self.assertIn("Nothing overdue", empty)


class DeliveryTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.server = CaptureServer()
        self.addCleanup(self.server.close)

    def test_posts_json_text_with_bot_name_appended_to_the_endpoint(self):
        n = notify.CliqNotifier(self.server.url, bot_unique_name="pmbot")
        n.send("hello *world*")
        n.flush()
        [req] = self.server.requests
        self.assertEqual(req["body"], {"text": "hello *world*"})
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        self.assertIn("zapikey=SECRETKEY", req["path"])
        self.assertIn("bot_unique_name=pmbot", req["path"])

    def test_over_long_text_is_truncated_before_it_is_sent(self):
        n = notify.CliqNotifier(self.server.url)
        n.send("y" * 9000)
        n.flush()
        self.assertEqual(len(self.server.requests[0]["body"]["text"]), notify.MAX_MESSAGE)

    def test_with_bot_helper(self):
        self.assertEqual(notify.with_bot("https://x/y?a=1", ""), "https://x/y?a=1")
        self.assertEqual(notify.with_bot("https://x/y", "bot"), "https://x/y?bot_unique_name=bot")
        self.assertEqual(notify.with_bot("https://x/y?a=1", "bot"), "https://x/y?a=1&bot_unique_name=bot")
        self.assertEqual(notify.with_bot("https://x/y?bot_unique_name=old", "bot"), "https://x/y?bot_unique_name=old")

    def test_a_failing_webhook_never_raises_and_later_messages_still_flow(self):
        self.server.status = 500
        n = notify.CliqNotifier(self.server.url)
        with self.assertLogs("zylker.notify", level="WARNING") as logs:
            n.send("one")
            n.flush()
        self.server.status = 200
        n.send("two")
        n.flush()
        self.assertEqual([r["body"]["text"] for r in self.server.requests], ["one", "two"])
        self.assertTrue(any("HTTPError" in line for line in logs.output))
        self.assertFalse(any("SECRETKEY" in line for line in logs.output))  # the key must never reach the logs

    def test_unreachable_endpoint_is_swallowed(self):
        n = notify.CliqNotifier("http://127.0.0.1:9/nothing", timeout=0.5)
        with self.assertLogs("zylker.notify", level="WARNING"):
            n.send("x")
            n.flush(3)

    def test_end_to_end_through_the_app(self):
        cfg = Config(db_path=self.config.db_path, cliq_endpoint=self.server.url, cliq_bot_unique_name="pmbot", public_url="https://pm.example.com")
        app = App(cfg, clock=lambda: self.now)  # builds a real CliqNotifier from the config
        self.assertIsInstance(app.notifier, notify.CliqNotifier)
        from helpers import Client

        self.make_project("WEB", members=("bob",))
        c = Client(app, self.tokens["alice"])
        self.ok(c.post("/api/projects/WEB/tasks", {"title": "Wire it up", "assignee": "bob", "priority": "high"}), 201)
        app.notifier.flush()
        [req] = self.server.requests
        self.assertIn("[WEB-1](https://pm.example.com/#/t/WEB-1) Wire it up", req["body"]["text"])
        self.assertIn("Assigned to *Bob* by Alice", req["body"]["text"])

    def test_nothing_is_sent_when_the_request_is_rolled_back(self):
        cfg = Config(db_path=self.config.db_path, cliq_endpoint=self.server.url)
        app = App(cfg, clock=lambda: self.now)
        from helpers import Client

        self.make_project("WEB", members=("bob",))
        c = Client(app, self.tokens["alice"])
        # assignee valid but the dates are invalid -> 422 before anything is committed or announced
        r = c.post("/api/projects/WEB/tasks", {"title": "x", "assignee": "bob", "start_date": "2026-10-10", "due_date": "2026-10-01"})
        self.assertEqual(r.status, 422)
        app.notifier.flush()
        self.assertEqual(self.server.requests, [])


class ConfigTests(ApiTestCase):
    def check(self, **kw):
        Config(**kw).validate()

    def test_endpoint_must_be_https_unless_local(self):
        self.check(cliq_endpoint="https://cliq.zoho.com/api/v2/x?zapikey=k")
        self.check(cliq_endpoint="http://127.0.0.1:8099/hook")
        self.check(cliq_endpoint="http://localhost/hook")
        for bad in ("http://cliq.example.com/hook", "ftp://x/y", "not a url", "https://"):
            with self.assertRaises(ValueError, msg=bad):
                self.check(cliq_endpoint=bad)

    def test_timezone_and_public_url_are_validated(self):
        self.check(timezone="Asia/Kolkata")
        self.check(timezone="UTC", public_url="https://pm.example.com")
        with self.assertRaises(ValueError):
            self.check(timezone="Mars/Olympus")
        with self.assertRaises(ValueError):
            self.check(public_url="pm.example.com")

    def test_from_env(self):
        cfg = Config.from_env({"ZYLKER_DB": "/tmp/x.db", "ZYLKER_PORT": "9001", "ZYLKER_PUBLIC_URL": "https://pm.example.com/", "ZYLKER_TIMEZONE": "Asia/Kolkata"})
        self.assertEqual((cfg.db_path, cfg.port, cfg.public_url, cfg.timezone), ("/tmp/x.db", 9001, "https://pm.example.com", "Asia/Kolkata"))
        with self.assertRaises(ValueError):
            Config.from_env({"ZYLKER_PORT": "abc"})

    def test_today_follows_the_configured_timezone(self):
        # 2026-10-16 22:00 UTC is already Saturday the 17th in India (UTC+5:30)
        from datetime import datetime, timezone

        late = datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)
        app = App(Config(db_path=self.config.db_path, timezone="Asia/Kolkata"), clock=lambda: late)
        with app.session(write=False) as ctx:
            self.assertEqual(ctx.today.isoformat(), "2026-10-17")
        utc_app = App(Config(db_path=self.config.db_path), clock=lambda: late)
        with utc_app.session(write=False) as ctx:
            self.assertEqual(ctx.today.isoformat(), "2026-10-16")
