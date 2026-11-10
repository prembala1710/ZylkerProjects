"""Webhook settings and the two commands that run the delivery worker."""

import unittest
from unittest import mock

from helpers import Client
from test_cli import CliBase
from webhook_helpers import Receiver

from zylker_pm import cli
from zylker_pm.app import App
from zylker_pm.config import Config


class WebhookConfigTests(unittest.TestCase):
    def test_defaults_are_the_safe_ones(self):
        cfg = Config.from_env({})
        self.assertEqual((cfg.webhook_allow_private, cfg.webhook_timeout), (False, 10.0))

    def test_settings_come_from_the_environment(self):
        cfg = Config.from_env({"ZYLKER_WEBHOOK_ALLOW_PRIVATE": "Yes", "ZYLKER_WEBHOOK_TIMEOUT": "2.5"})
        self.assertEqual((cfg.webhook_allow_private, cfg.webhook_timeout), (True, 2.5))
        for off in ("", "0", "false", "no", "off", "nope"):
            self.assertFalse(Config.from_env({"ZYLKER_WEBHOOK_ALLOW_PRIVATE": off}).webhook_allow_private, off)
        for on in ("1", "true", "TRUE", "on", "yes"):
            self.assertTrue(Config.from_env({"ZYLKER_WEBHOOK_ALLOW_PRIVATE": on}).webhook_allow_private, on)

    def test_bad_timeouts_are_rejected_with_the_variable_name(self):
        for bad in ("abc", "0", "0.5", "61", "-3"):
            with self.assertRaises(ValueError, msg=bad) as caught:
                Config.from_env({"ZYLKER_WEBHOOK_TIMEOUT": bad})
            self.assertIn("ZYLKER_WEBHOOK_TIMEOUT", str(caught.exception))
        with self.assertRaises(ValueError):
            Config(webhook_timeout=0).validate()


class WebhookCliTests(CliBase):
    def test_worker_once_delivers_what_is_due_and_exits(self):
        code, out, _ = self.run_cli("init", "--admin", "prem")
        api = Client(App(Config(db_path=self.db, webhook_allow_private=True)), self.token_in(out))  # real clock, like the CLI's worker
        receiver = Receiver()
        self.addCleanup(receiver.close)
        self.ok(api.post("/api/webhooks", {"name": "ops", "url": receiver.url, "events": ["*"]}), 201)
        self.ok(api.post("/api/projects", {"key": "OPS", "name": "Ops"}), 201)
        self.ok(api.post("/api/projects/OPS/tasks", {"title": "First"}), 201)
        with mock.patch.dict("os.environ", {"ZYLKER_WEBHOOK_ALLOW_PRIVATE": "1"}):
            code, out, err = self.run_cli("webhooks-worker", "--once")
        self.assertEqual((code, out.strip()), (0, "Attempted 2 webhook deliveries."), err)
        self.assertEqual(sorted(r["headers"]["X-Zylker-Event"] for r in receiver.requests), ["project.created", "task.created"])
        with mock.patch.dict("os.environ", {"ZYLKER_WEBHOOK_ALLOW_PRIVATE": "1"}):
            _, out, _ = self.run_cli("webhooks-worker", "--once")
        self.assertEqual(out.strip(), "Attempted 0 webhook deliveries.")  # nothing is sent twice

    def test_worker_once_without_permission_for_private_targets_records_the_refusal(self):
        code, out, _ = self.run_cli("init", "--admin", "prem")
        api = Client(App(Config(db_path=self.db, webhook_allow_private=True)), self.token_in(out))
        receiver = Receiver()
        self.addCleanup(receiver.close)
        hook = self.ok(api.post("/api/webhooks", {"name": "ops", "url": receiver.url, "events": ["*"]}), 201)
        self.ok(api.post("/api/webhooks/%d/test" % hook["id"]), 202)
        code, out, _ = self.run_cli("webhooks-worker", "--once")  # the CLI's own settings do not allow loopback
        self.assertEqual((code, out.strip()), (0, "Attempted 1 webhook delivery."))
        self.assertEqual(receiver.requests, [])
        (item,) = self.ok(api.get("/api/webhooks/%d/deliveries" % hook["id"]))["items"]
        self.assertEqual((item["status"], item["error"]), ("failed", "blocked_address"))

    def test_worker_runs_until_interrupted(self):
        with mock.patch.object(cli, "WebhookWorker") as worker:
            worker.return_value.run_forever.side_effect = KeyboardInterrupt
            self.run_cli("init")
            code, out, _ = self.run_cli("webhooks-worker")
        self.assertEqual(code, 0)
        self.assertIn("Stopping.", out)
        worker.return_value.stop.assert_called_once()

    def test_serve_starts_and_stops_a_worker_unless_told_not_to(self):
        fake = mock.Mock()
        fake.serve_forever.side_effect = KeyboardInterrupt
        for flags, expected in (([], "webhook worker: on"), (["--no-webhook-worker"], "webhook worker: off")):
            with mock.patch.object(cli, "make_threaded_server", return_value=fake), mock.patch.object(cli, "WebhookWorker") as worker:
                code, out, _ = self.run_cli("serve", *flags)
            self.assertEqual(code, 0)
            self.assertIn(expected, out)
            if flags:
                worker.assert_not_called()
            else:
                worker.return_value.start.assert_called_once()
                worker.return_value.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
