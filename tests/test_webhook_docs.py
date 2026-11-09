"""What docs/API.md tells receivers must be true: the code samples verify real deliveries and the example payload has the real shape."""

import json
import os
import re
import shutil
import subprocess
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from webhook_helpers import WebhookCase

from zylker_pm.webhook_worker import WebhookWorker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def verification_section():
    with open(os.path.join(ROOT, "docs", "API.md"), encoding="utf-8") as f:
        text = f.read()
    return text[text.index("### Verifying a delivery"):]


def code_block(language, text):
    match = re.search(r"```%s\n(.*?)```" % language, text, re.S)
    assert match, "no %s block found" % language
    return match.group(1)


class DocumentedBehaviourTests(WebhookCase):
    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc)  # the samples compare timestamps with the real clock
        self.make_project("WEB")
        self.hook = self.make_hook(name="Docs", events=["task.*"])
        self.make_task("WEB", "Homepage layout", assignee="bob", priority="high", due_date="2026-10-30")
        self.ok(self.set_status("WEB-1", "in_progress"))
        self.assertEqual(WebhookWorker(self.config, concurrency=1).run_until_idle(), 2)  # the real sender, over a real socket
        self.requests = {r["headers"]["X-Zylker-Event"]: r for r in self.receiver.requests}

    def test_the_python_sample_verifies_real_deliveries_and_rejects_tampering(self):
        namespace = {}
        exec(code_block("python", verification_section()), namespace)  # noqa: S102 - our own documentation
        verify = namespace["verify"]
        request = self.requests["task.status_changed"]
        headers, body, secret = request["headers"], request["raw"], self.hook["secret"]
        self.assertTrue(verify(secret, headers, body))
        self.assertFalse(verify("whsec_not_the_secret", headers, body))
        self.assertFalse(verify(secret, headers, body + b" "))
        self.assertFalse(verify(secret, {**headers, "X-Zylker-Timestamp": str(int(headers["X-Zylker-Timestamp"]) + 1)}, body))
        self.assertFalse(verify(secret, {k: v for k, v in headers.items() if k != "X-Zylker-Signature"}, body))
        with mock.patch("time.time", return_value=time.time() + 3600):
            self.assertFalse(verify(secret, headers, body), "a captured request must not verify an hour later")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_node_sample_agrees(self):
        request = self.requests["task.created"]
        h = {k.lower(): v for k, v in request["headers"].items()}  # Node lower-cases header names
        driver = """
const [secret, headers, bodyHex] = JSON.parse(process.argv[1]);
const body = Buffer.from(bodyHex, 'hex');
console.log(JSON.stringify([
  verify(secret, headers, body),
  verify('whsec_not_the_secret', headers, body),
  verify(secret, headers, Buffer.concat([body, Buffer.from(' ')])),
  verify(secret, headers, body, 0),
]));
"""
        script = code_block("javascript", verification_section()) + driver
        out = subprocess.run(["node", "-e", script, json.dumps([self.hook["secret"], h, request["raw"].hex()])], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [True, False, False, False])  # the last one has no tolerance for the elapsed time

    def test_the_example_payload_has_the_real_shape(self):
        with open(os.path.join(ROOT, "docs", "API.md"), encoding="utf-8") as f:
            text = f.read()
        example = json.loads(code_block("json", text[text.index("### Delivery format"):]))
        real = json.loads(self.requests["task.status_changed"]["raw"])
        self.assertEqual(sorted(example), sorted(real))
        self.assertEqual(sorted(example["task"]), sorted(real["task"]))
        self.assertEqual(sorted(example["project"]), sorted(k for k in real["project"] if k != "url"))  # url only with ZYLKER_PUBLIC_URL
        self.assertEqual(sorted(example["actor"]), sorted(real["actor"]))
        self.assertEqual(example["details"].keys(), real["details"].keys())
        self.assertEqual(list(example), sorted(example), "the documented body should show the sorted keys the server really sends")

    def test_the_event_table_lists_exactly_the_catalogue(self):
        from zylker_pm.services import webhooks

        with open(os.path.join(ROOT, "docs", "API.md"), encoding="utf-8") as f:
            text = f.read()
        section = text[text.index("### Events"):text.index("### Delivery format")]
        rows = [line for line in section.splitlines() if line.startswith("| `")]
        documented = {name for line in rows for name in re.findall(r"`([a-z_]+\.[a-z_]+)`", line)}
        self.assertEqual(sorted(documented), sorted(webhooks.EVENTS), "docs/API.md and services/webhooks.EVENT_GROUPS disagree")

    def test_documented_headers_are_the_ones_sent(self):
        with open(os.path.join(ROOT, "docs", "API.md"), encoding="utf-8") as f:
            text = f.read()
        table = text[text.index("### Delivery format"):text.index("### Verifying a delivery")]
        documented = set(re.findall(r"^\| `([A-Za-z-]+)` \|", table, re.M))
        sent = set(self.requests["task.created"]["headers"])
        self.assertTrue(documented <= sent, sorted(documented - sent))
        self.assertEqual(sorted(h for h in sent if h.startswith("X-Zylker-")), sorted(h for h in documented if h.startswith("X-Zylker-")))
        self.assertEqual(self.requests["task.created"]["headers"]["User-Agent"].split("/")[0], "ZylkerProjects")
        self.assertEqual(self.requests["task.created"]["headers"]["Content-Type"], "application/json")


if __name__ == "__main__":
    unittest.main()
