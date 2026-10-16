"""SQLite schema, connections and transactions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    email TEXT,
    role TEXT NOT NULL CHECK (role IN ('viewer','member','manager','admin')),
    weekly_capacity_hours REAL NOT NULL DEFAULT 40
        CHECK (weekly_capacity_hours >= 0 AND weekly_capacity_hours <= 168),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL
        CHECK (status IN ('planning','active','on_hold','completed','archived')),
    owner_id INTEGER REFERENCES users(id),
    start_date TEXT,
    due_date TEXT,
    next_task_number INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE milestones (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'task' CHECK (type IN ('task','bug','story','epic')),
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo','in_progress','in_review','blocked','done','cancelled')),
    priority TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('low','medium','high','critical')),
    assignee_id INTEGER REFERENCES users(id),
    reporter_id INTEGER NOT NULL REFERENCES users(id),
    parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    start_date TEXT,
    due_date TEXT,
    estimate_hours REAL CHECK (estimate_hours IS NULL OR estimate_hours >= 0),
    external_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (project_id, number)
);
CREATE INDEX idx_tasks_project_status ON tasks (project_id, status);
CREATE INDEX idx_tasks_assignee ON tasks (assignee_id, status);
CREATE INDEX idx_tasks_parent ON tasks (parent_id);
CREATE INDEX idx_tasks_due ON tasks (due_date);
CREATE INDEX idx_tasks_extref ON tasks (project_id, external_ref);

CREATE TABLE task_labels (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    PRIMARY KEY (task_id, label)
);

CREATE TABLE task_dependencies (
    blocker_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocked_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (blocker_id, blocked_id),
    CHECK (blocker_id <> blocked_id)
);
CREATE INDEX idx_dep_blocked ON task_dependencies (blocked_id);

CREATE TABLE comments (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at TEXT
);
CREATE INDEX idx_comments_task ON comments (task_id);

CREATE TABLE time_entries (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    hours REAL NOT NULL CHECK (hours > 0 AND hours <= 24),
    work_date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_time_task ON time_entries (task_id);
CREATE INDEX idx_time_user_date ON time_entries (user_id, work_date);

CREATE TABLE activity (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    task_id INTEGER,
    task_key TEXT,
    actor_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_activity_project ON activity (project_id, id);
CREATE INDEX idx_activity_task ON activity (task_id, id);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with manual transaction control (autocommit mode)."""
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str) -> None:
    """Create the schema on a new database; refuse databases from the future."""
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                "Database schema v%d is newer than this application (v%d)" % (version, SCHEMA_VERSION)
            )
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, write: bool = True):
    """BEGIN IMMEDIATE for writers (serialises task numbering); BEGIN for readers."""
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
