"""Documentation must not drift from the code: every route and every setting has to be written down."""

import os
import re
import unittest

from helpers import ApiTestCase  # noqa: F401  (also puts the repo root on sys.path)

from zylker_pm.app import ROUTES, App
from zylker_pm.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class DocsTests(unittest.TestCase):
    def test_every_route_is_in_the_api_reference(self):
        App(Config(db_path=":memory:"))  # importing the routes registers them
        doc = read("docs", "API.md")
        missing = [f"{r.method} {r.pattern}" for r in ROUTES if f"`{r.method} {r.pattern}`" not in doc]
        self.assertEqual(missing, [], "routes missing from docs/API.md")

    def test_documented_roles_match_the_code(self):
        App(Config(db_path=":memory:"))
        doc = read("docs", "API.md")
        for r in ROUTES:
            if r.public:
                continue
            row = next(line for line in doc.splitlines() if line.startswith(f"| `{r.method} {r.pattern}`"))
            self.assertIn("| %s |" % r.role, row, f"{r.method} {r.pattern}: docs disagree with the code about the role")

    def test_no_stale_routes_are_documented(self):
        App(Config(db_path=":memory:"))
        real = {f"{r.method} {r.pattern}" for r in ROUTES}
        documented = set(re.findall(r"`((?:GET|POST|PATCH|DELETE) /api/[^`\s]+)`", read("docs", "API.md")))
        self.assertEqual(sorted(documented - real), [], "documented endpoints that do not exist")

    def test_every_environment_variable_is_documented(self):
        source = read("zylker_pm", "config.py")
        variables = {"ZYLKER_" + name for name in re.findall(r'(?<![.\w])get\("([A-Z_]+)"', source)}
        self.assertGreaterEqual(len(variables), 7)
        readme = read("README.md")
        self.assertEqual([v for v in sorted(variables) if v not in readme], [], "env vars missing from the README")

    def test_every_cli_command_is_documented(self):
        from zylker_pm.cli import build_parser

        commands = [name for action in build_parser()._actions if hasattr(action, "choices") and action.choices for name in action.choices]
        readme = read("README.md")
        self.assertEqual([c for c in commands if "zylker-pm %s" % c not in readme and "zylker_pm %s" % c not in readme], [])

    def test_the_example_csv_actually_imports(self):
        from helpers import ApiTestCase as Base

        class Probe(Base):
            def runTest(self):
                self.make_project("DEMO", members=("bob", "carol"))
                with open(os.path.join(ROOT, "examples", "jira_style_import.csv"), "rb") as f:
                    raw = f.read()
                r = self.client("alice").request("POST", "/api/projects/DEMO/import", raw=raw, content_type="text/csv")
                body = self.ok(r)
                self.assertEqual((body["created"], body["errors"]), (3, []))
                tasks = self.ok(self.client("alice").get("/api/tasks", query={"project": "DEMO", "sort": "key"}))["items"]
                self.assertEqual([t["status"] for t in tasks], ["in_progress", "todo", "done"])
                self.assertEqual(tasks[2]["due_date"], "2026-10-05")

        probe = Probe()
        result = unittest.TestResult()
        probe.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
