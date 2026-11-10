"""Schema v2 -> v3: dependency types, informational links, checklists and watchers arrive without touching existing data."""

import os
import sqlite3

from helpers import ApiTestCase, Client

from zylker_pm import db
from zylker_pm.app import App
from zylker_pm.config import Config
from zylker_pm.db import init_db
from zylker_pm.services import users

NOW = "2026-01-01T00:00:00Z"


class MigrationTests(ApiTestCase):
    def v2_database(self, path):
        """A database exactly as the previous release created it: schema v2, with a dependency already in it."""
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA.replace(db.TASK_EXTRAS_SCHEMA, ""))
        conn.execute("PRAGMA user_version = 2")
        conn.execute("INSERT INTO users (id, username, display_name, role, token_hash, created_at) VALUES (1,'old','Old','admin','h',?)", (NOW,))
        conn.execute("INSERT INTO projects (id, key, name, status, owner_id, next_task_number, created_at, updated_at) VALUES (1,'OLD','Legacy','active',1,3,?,?)", (NOW, NOW))
        for number in (1, 2):
            conn.execute(
                "INSERT INTO tasks (id, project_id, number, title, reporter_id, created_at, updated_at) VALUES (?,1,?,?,1,?,?)",
                (number, number, "Task %d" % number, NOW, NOW),
            )
        conn.execute("INSERT INTO task_dependencies (blocker_id, blocked_id, created_at) VALUES (1,2,?)", (NOW,))
        conn.commit()
        conn.close()

    @staticmethod
    def shape(path):
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        return rows, version

    def test_a_v2_database_upgrades_in_place_keeping_its_data(self):
        path = os.path.join(self.tmp, "v2.db")
        self.v2_database(path)
        init_db(path)
        fresh = os.path.join(self.tmp, "fresh.db")
        init_db(fresh)
        self.assertEqual(self.shape(path), self.shape(fresh))  # the migration and a new install end up identical
        self.assertEqual(self.shape(path)[1], db.SCHEMA_VERSION)
        self.assertEqual(db.SCHEMA_VERSION, 3)
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT blocker_id, blocked_id, type, lag_days FROM task_dependencies").fetchall(), [(1, 2, "finish_to_start", 0)])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        conn.close()
        init_db(path)  # running it again is a no-op
        self.assertEqual(self.shape(path), self.shape(fresh))

    def test_a_v1_database_goes_all_the_way_to_the_current_version(self):
        path = os.path.join(self.tmp, "v1.db")
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA.replace(db.TASK_EXTRAS_SCHEMA, "").replace(db.PROJECT_FIELDS_SCHEMA, ""))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        fresh = os.path.join(self.tmp, "fresh.db")
        init_db(path)
        init_db(fresh)
        self.assertEqual(self.shape(path), self.shape(fresh))

    def test_the_upgraded_database_works_through_the_api(self):
        path = os.path.join(self.tmp, "v2b.db")
        self.v2_database(path)
        init_db(path)
        app = App(Config(db_path=path), clock=lambda: self.now)
        with app.session() as ctx:
            _, token = users.create_user(ctx, {"username": "root", "display_name": "Root", "role": "admin"})
        c = Client(app, token)
        old = self.ok(c.get("/api/tasks/OLD-2"))
        self.assertEqual((old["blocked_by"][0]["type"], old["blocked_by_open"]), ("finish_to_start", 1))  # still waits on OLD-1
        self.assertEqual(c.patch("/api/tasks/OLD-2", {"status": "in_progress"}).status, 409)
        self.ok(c.post("/api/tasks/OLD-1/checklist", {"text": "works"}), 201)
        self.ok(c.post("/api/tasks/OLD-1/watchers", {}))
        self.ok(c.post("/api/tasks/OLD-1/links", {"type": "relates_to", "task": "OLD-2"}), 201)
        self.ok(c.patch("/api/tasks/OLD-2/dependencies/OLD-1", {"type": "start_to_start"}))
        self.assertEqual(self.ok(c.post("/api/tasks/bulk", {"tasks": ["OLD-1", "OLD-2"], "changes": {"priority": "high"}}))["updated"], 2)

    def test_a_failed_migration_leaves_the_database_untouched(self):
        path = os.path.join(self.tmp, "broken.db")
        self.v2_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE task_watchers (surprise INTEGER)")  # the last statement of the migration blows up
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        conn = sqlite3.connect(path)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(task_dependencies)")]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        self.assertEqual(version, 2)
        self.assertNotIn("type", columns)  # the ALTERs ran first and were rolled back with everything else
        self.assertNotIn("task_links", names)
        self.assertNotIn("task_checklist_items", names)

    def test_the_database_enforces_the_basics_itself(self):
        path = os.path.join(self.tmp, "rules.db")
        self.v2_database(path)
        init_db(path)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        bad = [
            "INSERT INTO task_dependencies (blocker_id, blocked_id, created_at, type) VALUES (2,1,'x','start_to_finish')",
            "INSERT INTO task_dependencies (blocker_id, blocked_id, created_at, lag_days) VALUES (2,1,'x',400)",
            "INSERT INTO task_links (source_id, target_id, type, created_at) VALUES (1,1,'relates_to','x')",
            "INSERT INTO task_links (source_id, target_id, type, created_at) VALUES (1,2,'blocks','x')",
            "INSERT INTO task_checklist_items (task_id, position, text, created_at, updated_at) VALUES (1,1,'','x','x')",
            "INSERT INTO task_checklist_items (task_id, position, text, done, created_at, updated_at) VALUES (1,1,'t',1,'x','x')",
        ]
        for sql in bad:
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                conn.execute(sql)
        conn.execute("INSERT INTO task_links (source_id, target_id, type, created_at) VALUES (1,2,'duplicates','x')")
        with self.assertRaises(sqlite3.IntegrityError):  # one link per pair, in either direction
            conn.execute("INSERT INTO task_links (source_id, target_id, type, created_at) VALUES (2,1,'relates_to','x')")
        conn.close()
