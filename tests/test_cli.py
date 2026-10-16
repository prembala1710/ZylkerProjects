"""The command line, the demo seeder and database backup."""

import contextlib
import io
import json
import os
import sqlite3
from unittest import mock

from helpers import ApiTestCase, CaptureServer, Client

from zylker_pm import cli
from zylker_pm.app import App
from zylker_pm.config import Config


class CliBase(ApiTestCase):
    """Runs the CLI against its own database file (separate from the fixture one)."""

    def setUp(self):
        super().setUp()
        self.db = os.path.join(self.tmp, "cli.db")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZYLKER_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = cli.main(["--db", self.db, *args])
            else:
                code = cli.main(["--db", self.db, *args])
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def token_in(text):
        return next(line.strip() for line in text.splitlines() if line.strip().startswith("zpm_"))

    def api(self, token):
        return Client(App(Config(db_path=self.db), clock=lambda: self.now), token)


class CliTests(CliBase):
    def test_init_creates_a_working_admin_and_is_safe_to_repeat(self):
        code, out, _ = self.run_cli("init", "--admin", "prem", "--name", "Prem B", "--email", "prem@example.com")
        self.assertEqual(code, 0)
        token = self.token_in(out)
        me = self.ok(self.api(token).get("/api/me"))
        self.assertEqual((me["username"], me["role"], me["display_name"], me["email"]), ("prem", "admin", "Prem B", "prem@example.com"))
        code, out, _ = self.run_cli("init", "--admin", "prem")
        self.assertEqual(code, 0)
        self.assertIn("already exists", out)
        self.assertNotIn("zpm_", out)  # a second run must never mint or reveal another admin token
        self.assertEqual(self.api(token).get("/api/me").status, 200)

    def test_create_user_and_rotate_token(self):
        self.run_cli("init")
        code, out, _ = self.run_cli("create-user", "dana", "--name", "Dana K", "--role", "manager", "--capacity", "30")
        self.assertEqual(code, 0)
        first = self.token_in(out)
        me = self.ok(self.api(first).get("/api/me"))
        self.assertEqual((me["role"], me["weekly_capacity_hours"]), ("manager", 30))
        code, out, _ = self.run_cli("rotate-token", "dana")
        second = self.token_in(out)
        self.assertNotEqual(first, second)
        self.assertEqual(self.api(first).get("/api/me").status, 401)
        self.assertEqual(self.api(second).get("/api/me").status, 200)
        code, _, err = self.run_cli("rotate-token", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("No such user", err)
        code, _, err = self.run_cli("create-user", "dana")
        self.assertEqual((code, "already taken" in err), (1, True))

    def test_bad_input_is_a_clear_error_not_a_traceback(self):
        self.run_cli("init")
        code, _, err = self.run_cli("create-user", "X")  # too short
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Error:"))
        with mock.patch.dict(os.environ, {"ZYLKER_TIMEZONE": "Nowhere/Land"}):
            code, _, err = self.run_cli("init")
        self.assertEqual(code, 1)
        self.assertIn("Unknown timezone", err)
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("create-user", "ok", "--role", "emperor")
        self.assertEqual(cm.exception.code, 2)

    def test_backup_is_consistent_and_does_not_overwrite_by_default(self):
        self.run_cli("init", "--admin", "prem")
        target = os.path.join(self.tmp, "backup.db")
        code, out, _ = self.run_cli("backup", target)
        self.assertEqual(code, 0)
        conn = sqlite3.connect(target)
        self.assertEqual(conn.execute("SELECT username FROM users").fetchall(), [("prem",)])
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()
        code, _, err = self.run_cli("backup", target)
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.run_cli("backup", target, "--force")[0], 0)

    def test_digest_prints_and_refuses_to_post_without_a_webhook(self):
        _, out, _ = self.run_cli("init", "--admin", "prem")
        token = self.token_in(out)
        c = self.api(token)
        self.ok(c.post("/api/projects", {"key": "DG", "name": "D"}), 201)
        self.ok(c.post("/api/projects/DG/tasks", {"title": "Late thing", "due_date": "2026-10-01"}), 201)
        code, out, _ = self.run_cli("digest", "--days", "5")
        self.assertEqual(code, 0)
        self.assertIn("Late thing", out)
        self.assertIn("overdue", out)
        code, _, err = self.run_cli("digest", "--post")
        self.assertEqual(code, 2)
        self.assertIn("ZYLKER_CLIQ_ENDPOINT", err)

    def test_import_csv_dry_run_then_real_and_exit_codes(self):
        _, out, _ = self.run_cli("init", "--admin", "prem")
        token = self.token_in(out)
        self.ok(self.api(token).post("/api/projects", {"key": "IMP", "name": "Import"}), 201)
        path = os.path.join(self.tmp, "in.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("summary,priority\nOne,High\nTwo,Low\n")
        code, out, _ = self.run_cli("import-csv", path, "--project", "IMP", "--as", "prem", "--dry-run")
        report = json.loads(out)
        self.assertEqual((code, report["dry_run"], report["created"]), (0, True, 2))
        self.assertEqual(self.ok(self.api(token).get("/api/tasks"))["total"], 0)
        code, out, _ = self.run_cli("import-csv", path, "--project", "IMP", "--as", "prem")
        self.assertEqual((code, json.loads(out)["created"]), (0, 2))
        code, out, _ = self.run_cli("import-csv", "-", "--project", "IMP", "--as", "prem", stdin="summary\n\n,\nGood\n")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("import-csv", "-", "--project", "IMP", "--as", "prem", stdin="summary,due date\nBackwards,2026-01-01\nX,2026-12-12\n")
        self.assertEqual(code, 0)
        bad = "summary,start date,due date\nBad,2026-02-01,2026-01-01\n"
        code, out, _ = self.run_cli("import-csv", "-", "--project", "IMP", "--as", "prem", stdin=bad)
        self.assertEqual((code, len(json.loads(out)["errors"])), (3, 1))  # exit 3 = completed with row errors
        self.assertEqual(self.run_cli("import-csv", path, "--project", "IMP", "--as", "ghost")[0], 1)
        self.assertEqual(self.run_cli("import-csv", path, "--project", "NOPE", "--as", "prem")[0], 1)


class SeedTests(CliBase):
    def test_cli_prints_a_token_for_every_demo_user_and_refuses_a_second_run(self):
        self.run_cli("init")
        code, out, _ = self.run_cli("seed-demo")
        self.assertEqual(code, 0)
        names = [line.split("'")[1] for line in out.splitlines() if line.startswith("API token for")]
        self.assertEqual(sorted(names), ["alice", "bob", "carol", "dave", "erin"])
        code, _, err = self.run_cli("seed-demo")
        self.assertEqual(code, 1)
        self.assertIn("already contains projects", err)

    def test_demo_data_is_valid_and_tells_a_coherent_story(self):
        from zylker_pm.db import init_db
        from zylker_pm.seed import seed_demo

        init_db(self.db)
        app = App(Config(db_path=self.db), clock=lambda: self.now)  # fixed clock: fully deterministic
        tokens = seed_demo(app)
        self.assertEqual(app.clock(), self.now)  # the seeder restores the clock it wound back
        c = Client(app, tokens["alice"])
        portfolio = self.ok(c.get("/api/reports/portfolio"))
        self.assertEqual({p["key"]: p["health"]["status"] for p in portfolio["projects"]}, {"APP": "off_track", "WEB": "at_risk", "INT": "on_track"})
        self.assertEqual([p["key"] for p in portfolio["projects"]], ["APP", "WEB", "INT"])  # worst first
        wl = {i["user"]["username"]: i for i in self.ok(c.get("/api/reports/workload"))["items"]}
        self.assertTrue(wl["carol"]["overallocated"])  # part-time and loaded: the demo's workload story
        self.assertFalse(wl["bob"]["overallocated"])
        self.assertEqual(self.ok(c.get("/api/projects/WEB/critical-path"))["path"], ["WEB-4", "WEB-6", "WEB-7", "WEB-10"])
        series = [d["remaining"] for d in self.ok(c.get("/api/projects/WEB/burndown"))["days"] if d["remaining"] is not None]
        self.assertEqual((series[0], max(series)), (2, 10))  # scope grew from 2 to 10 tasks: visible scope creep
        for key in ("WEB-5", "WEB-8", "APP-1"):
            self.assertGreater(len(self.ok(c.get("/api/tasks/%s/activity" % key))["items"]), 1)
        self.assertEqual(len(self.ok(c.get("/api/tasks/WEB-5/comments"))["items"]), 2)
        self.assertEqual(Client(app, tokens["erin"]).post("/api/projects/WEB/tasks", {"title": "x"}).status, 403)  # viewer

    def test_seeding_never_pings_a_real_cliq_channel(self):
        from zylker_pm.db import init_db
        from zylker_pm.seed import seed_demo

        server = CaptureServer()
        self.addCleanup(server.close)
        init_db(self.db)
        app = App(Config(db_path=self.db, cliq_endpoint=server.url), clock=lambda: self.now)
        self.assertTrue(type(app.notifier).__name__ == "CliqNotifier")  # a real notifier IS configured...
        seed_demo(app)
        app.notifier.flush()
        self.assertEqual(server.requests, [])  # ...and yet the fake data produced no messages
        self.assertEqual(type(app.notifier).__name__, "CliqNotifier")  # restored afterwards

    def test_demo_users_never_overwrite_existing_accounts(self):
        self.run_cli("init")
        _, out, _ = self.run_cli("create-user", "alice", "--role", "admin", "--name", "Real Alice")
        real = self.token_in(out)
        _, out, _ = self.run_cli("seed-demo")
        self.assertNotIn("API token for 'alice'", out)  # the existing account keeps its identity and token
        me = self.ok(self.api(real).get("/api/me"))
        self.assertEqual((me["role"], me["display_name"]), ("admin", "Real Alice"))
