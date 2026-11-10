"""``zylker-pm msp-export`` and ``zylker-pm msp-import``: files, standard streams, exit codes and plain error messages."""

import contextlib
import io
import json
import os
import xml.etree.ElementTree as ET
from unittest import mock

from helpers import ApiTestCase
from msproject_helpers import doc, task

from zylker_pm import cli


class MspCliCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZYLKER_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.make_project("SRC", members=("bob",))
        self.make_task("SRC", "Epic", type="epic")
        self.make_task("SRC", "Child", parent="SRC-1", assignee="bob", estimate_hours=16, due_date="2026-12-01")
        self.make_task("SRC", "Later", estimate_hours=4)
        self.client("alice").post("/api/tasks/SRC-3/dependencies", {"blocked_by": "SRC-2", "type": "finish_to_finish", "lag_days": 2})
        self.make_project("DST", members=("bob",))

    def run_cli(self, *args, stdin=None):
        """Standard streams that have a ``.buffer`` like the real ones: the XML is written and read as bytes."""
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
        err = io.StringIO()
        stdin_stream = io.TextIOWrapper(io.BytesIO(stdin if isinstance(stdin, bytes) else (stdin or "").encode("utf-8")), encoding="utf-8")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", stdin_stream):
            code = cli.main(["--db", self.config.db_path, *args])
        out.flush()
        return code, out.buffer.getvalue(), err.getvalue()

    def tasks(self, key):
        return self.ok(self.client("alice").get("/api/tasks", query={"project": key, "limit": 100, "sort": "number"}))["items"]


class ExportTests(MspCliCase):
    def test_it_writes_the_file_it_names_and_says_so(self):
        path = os.path.join(self.tmp, "plan.xml")
        code, out, _ = self.run_cli("msp-export", "--project", "SRC", "-o", path)
        self.assertEqual(code, 0)
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(out.decode("utf-8").strip(), "Wrote %s (%d bytes)" % (path, len(data)))
        root = ET.fromstring(data)
        self.assertEqual(root.tag, "{http://schemas.microsoft.com/project}Project")
        self.assertEqual(sorted(t.text for t in root.iter("{http://schemas.microsoft.com/project}Name") if t.text in ("Epic", "Child", "Later")), ["Child", "Epic", "Later"])

    def test_the_default_name_is_the_project_key_in_the_current_folder(self):
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        code, out, _ = self.run_cli("msp-export", "--project", "src")  # the key's case does not matter
        self.assertEqual(code, 0)
        self.assertIn("Wrote SRC-msproject.xml", out.decode("utf-8"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "SRC-msproject.xml")))

    def test_a_dash_writes_the_xml_itself_to_standard_output_and_nothing_else(self):
        code, out, err = self.run_cli("msp-export", "--project", "SRC", "-o", "-")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(b"<?xml"))
        names = [t.text for t in ET.fromstring(out).iter("{http://schemas.microsoft.com/project}Name")]
        self.assertTrue({"Epic", "Child", "Later"} <= set(names))

    def test_an_unknown_project_and_an_unwritable_path_are_plain_errors(self):
        code, out, err = self.run_cli("msp-export", "--project", "NOPE", "-o", "-")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("Project NOPE not found", err)
        code, _, err = self.run_cli("msp-export", "--project", "SRC", "-o", os.path.join(self.tmp, "missing-folder", "x.xml"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Error: "), err)
        self.assertNotIn("Traceback", err)


class ImportTests(MspCliCase):
    def exported(self, key="SRC"):
        return self.run_cli("msp-export", "--project", key, "-o", "-")[1]

    def test_a_file_exported_from_one_project_comes_into_another_whole(self):
        path = os.path.join(self.tmp, "src.xml")
        with open(path, "wb") as f:
            f.write(self.exported())
        code, out, err = self.run_cli("msp-import", path, "--project", "DST", "--as", "alice")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        self.assertEqual((report["created"], report["links"], report["errors"], report["dry_run"]), (3, 1, [], False))
        self.assertEqual(report["keys"], ["DST-1", "DST-2", "DST-3"])
        rows = {t["title"]: t for t in self.tasks("DST")}
        self.assertEqual((rows["Child"]["parent"], rows["Child"]["estimate_hours"], rows["Child"]["due_date"], rows["Child"]["assignee"]["username"]), ("DST-1", 16.0, "2026-12-01", "bob"))
        detail = self.ok(self.client("alice").get("/api/tasks/DST-3"))
        self.assertEqual([(d["key"], d["type"], d["lag_days"]) for d in detail["blocked_by"]], [("DST-2", "finish_to_finish", 2)])

    def test_a_dry_run_says_what_would_happen_and_writes_nothing(self):
        before = self.tasks("DST")
        code, out, _ = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", "--dry-run", stdin=self.exported())
        report = json.loads(out)
        self.assertEqual((code, report["dry_run"], report["created"]), (0, True, 3))
        self.assertEqual(self.tasks("DST"), before)
        self.assertEqual(self.tasks("DST"), [])

    def test_the_xml_can_come_from_standard_input_and_importing_it_twice_adds_nothing(self):
        data = self.exported()
        code, out, _ = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", stdin=data)
        self.assertEqual((code, json.loads(out)["created"]), (0, 3))
        code, out, _ = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", stdin=data)
        again = json.loads(out)
        self.assertEqual((code, again["created"], again["skipped_duplicates"], again["links"]), (0, 0, 3, 0))
        self.assertEqual(len(self.tasks("DST")), 3)

    def test_exit_code_3_means_it_finished_but_a_dependency_had_to_be_left_out(self):
        data = doc(task(1, "Good"), task(2, "Loop A", links=[4]), task(3, "Loop B", links=[2]), task(4, "Loop C", links=[3]))
        code, out, _ = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", stdin=data)
        report = json.loads(out)
        self.assertEqual((code, report["created"], report["links"], len(report["errors"])), (3, 4, 2, 1))
        self.assertIn("circular", report["errors"][0]["message"])
        self.assertEqual(len(self.tasks("DST")), 4)  # what could be imported was

    def test_who_runs_it_matters_and_every_failure_is_one_plain_line(self):
        data = self.exported()
        code, out, err = self.run_cli("msp-import", "-", "--project", "DST", "--as", "ghost", stdin=data)
        self.assertEqual((code, out, err), (1, b"", "No such user: ghost\n"))
        code, out, err = self.run_cli("msp-import", "-", "--project", "DST", "--as", "vera", stdin=data)  # a viewer
        self.assertEqual((code, out), (1, b""))
        self.assertTrue(err.startswith("Error: "), err)
        code, _, err = self.run_cli("msp-import", "-", "--project", "NOPE", "--as", "alice", stdin=data)
        self.assertEqual(code, 1)
        self.assertIn("Project NOPE not found", err)
        code, _, err = self.run_cli("msp-import", os.path.join(self.tmp, "nothing.xml"), "--project", "DST", "--as", "alice")
        self.assertEqual(code, 1)
        self.assertIn("nothing.xml", err)
        self.assertNotIn("Traceback", err)
        code, _, err = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", stdin="this is not xml")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Error: "), err)
        code, _, err = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", stdin=b"")
        self.assertEqual(code, 1)
        self.assertEqual(self.tasks("DST"), [])

    def test_people_who_are_not_members_are_added_only_when_asked(self):
        def plan(title, name):
            return doc(task(1, title), resources=['<Resource><UID>1</UID><ID>1</ID><Name>Carol</Name><Type>1</Type></Resource>'],
                       assignments=['<Assignment><UID>1</UID><TaskUID>1</TaskUID><ResourceUID>1</ResourceUID><Work>PT8H0M0S</Work></Assignment>'], Name=name)  # fmt: skip

        code, out, _ = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", stdin=plan("Work", "First plan"))
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("carol", report["warnings"][0]["message"])
        self.assertIsNone(self.tasks("DST")[0]["assignee"])
        code, out, _ = self.run_cli("msp-import", "-", "--project", "DST", "--as", "alice", "--add-missing-members", stdin=plan("Work 2", "Second plan"))
        self.assertEqual((code, json.loads(out)["warnings"]), (0, []))
        self.assertEqual({t["title"]: t["assignee"] and t["assignee"]["username"] for t in self.tasks("DST")}["Work 2"], "carol")
