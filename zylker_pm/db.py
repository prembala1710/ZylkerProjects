"""SQLite schema, connections and transactions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = 3

# Added in schema v2. Shared by fresh databases (SCHEMA) and the v1 -> v2 migration so the two can never drift.
PROJECT_FIELDS_SCHEMA = """
CREATE TABLE project_fields (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type TEXT NOT NULL CHECK (type IN ('text','number','url')),
    description TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 0 CHECK (required IN (0,1)),
    max_length INTEGER CHECK (max_length IS NULL OR (max_length >= 1 AND max_length <= 2000)),
    min_value REAL,
    max_value REAL,
    decimals INTEGER CHECK (decimals IS NULL OR (decimals >= 0 AND decimals <= 3)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE project_field_values (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    field_id INTEGER NOT NULL REFERENCES project_fields(id) ON DELETE CASCADE,
    text_value TEXT,
    number_value REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, field_id),
    CHECK ((text_value IS NULL) <> (number_value IS NULL))
);
CREATE INDEX idx_pfv_field ON project_field_values (field_id);
"""

# Added in schema v3: dependency types and lag, informational task links, checklists and watchers. Like the v2 block,
# this one string is used by fresh databases (SCHEMA) and by the v2 -> v3 migration so the two can never drift.
# The ALTERs add columns with defaults, so every existing dependency becomes a plain finish-to-start link with no lag.
TASK_EXTRAS_SCHEMA = """
ALTER TABLE task_dependencies ADD COLUMN type TEXT NOT NULL DEFAULT 'finish_to_start'
    CHECK (type IN ('finish_to_start','start_to_start','finish_to_finish'));
ALTER TABLE task_dependencies ADD COLUMN lag_days INTEGER NOT NULL DEFAULT 0
    CHECK (lag_days >= -365 AND lag_days <= 365);

CREATE TABLE task_links (
    source_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('relates_to','duplicates','cloned_from')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id),
    CHECK (source_id <> target_id)
);
CREATE INDEX idx_task_links_target ON task_links (target_id);
CREATE UNIQUE INDEX idx_task_links_pair ON task_links (MIN(source_id, target_id), MAX(source_id, target_id));

CREATE TABLE task_checklist_items (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 1),
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 200),
    done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0,1)),
    done_by INTEGER REFERENCES users(id),
    done_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((done = 1) = (done_at IS NOT NULL))
);
CREATE INDEX idx_checklist_task ON task_checklist_items (task_id, position);

CREATE TABLE task_watchers (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    added_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, user_id)
);
CREATE INDEX idx_watchers_user ON task_watchers (user_id);
"""

# Each entry upgrades a database from version N to N + 1, inside one transaction.
MIGRATIONS = {1: PROJECT_FIELDS_SCHEMA, 2: TASK_EXTRAS_SCHEMA}

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
""" + PROJECT_FIELDS_SCHEMA + TASK_EXTRAS_SCHEMA


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
    """Create the schema on a new database, upgrade an older one, refuse one from the future."""
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
        else:
            while version < SCHEMA_VERSION:
                _migrate(conn, version)
                version += 1
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """All-or-nothing: a failed migration leaves the database exactly as it was."""
    script = "BEGIN IMMEDIATE;\n%s\nPRAGMA user_version = %d;\nCOMMIT;" % (MIGRATIONS[version], version + 1)
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


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
