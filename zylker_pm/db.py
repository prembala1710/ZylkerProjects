"""SQLite schema, connections and transactions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = 8

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

# Added in schema v3: the audit log grows up.
#  - The activity table loses its ON DELETE CASCADE to projects. Deleting a project used to erase that
#    project's whole history, which is exactly what an audit trail must not allow. `project_key` keeps the
#    history readable after the project is gone.
#  - `ip` and `request_id` tie an event to the request that caused it.
#  - Indexes for the admin log monitor's filters.
#  - Rows whose task_id no longer points at the task they were written for (SQLite reuses the highest row id
#    after a delete) are detached: the task_key stays, so nothing is lost, but a new task no longer inherits
#    a deleted task's history.
AUDIT_SCHEMA = """
CREATE TABLE activity_v3 (
    id INTEGER PRIMARY KEY,
    project_id INTEGER,
    project_key TEXT,
    task_id INTEGER,
    task_key TEXT,
    actor_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    ip TEXT,
    request_id TEXT
);
INSERT INTO activity_v3 (id, project_id, project_key, task_id, task_key, actor_id, action, details, created_at)
SELECT a.id, a.project_id, p.key,
       CASE WHEN a.task_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM tasks t JOIN projects tp ON tp.id = t.project_id
                WHERE t.id = a.task_id AND tp.key || '-' || t.number = a.task_key)
            THEN a.task_id END,
       a.task_key, a.actor_id, a.action, a.details, a.created_at
FROM activity a LEFT JOIN projects p ON p.id = a.project_id;
DROP TABLE activity;
ALTER TABLE activity_v3 RENAME TO activity;
CREATE INDEX idx_activity_project ON activity (project_id, id);
CREATE INDEX idx_activity_task ON activity (task_id, id);
CREATE INDEX idx_activity_actor ON activity (actor_id, id);
CREATE INDEX idx_activity_action ON activity (action, id);
CREATE INDEX idx_activity_created ON activity (created_at);
"""

# Added in schema v4: recurring tasks. A rule (task_recurrences) describes a schedule and a task template; every
# date it produces is recorded once in recurrence_occurrences, whose primary key is what makes generation
# idempotent: running the generator twice, or two generators at once, can never create the same task twice.
RECURRENCE_SCHEMA = """
CREATE TABLE task_recurrences (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'task' CHECK (type IN ('task','bug','story','epic')),
    priority TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low','medium','high','critical')),
    assignee_id INTEGER REFERENCES users(id),
    estimate_hours REAL CHECK (estimate_hours IS NULL OR (estimate_hours >= 0 AND estimate_hours <= 10000)),
    labels TEXT NOT NULL DEFAULT '[]',
    due_offset_days INTEGER NOT NULL DEFAULT 0 CHECK (due_offset_days BETWEEN 0 AND 365),
    frequency TEXT NOT NULL CHECK (frequency IN ('daily','weekly','monthly')),
    interval_count INTEGER NOT NULL DEFAULT 1 CHECK (interval_count BETWEEN 1 AND 365),
    weekdays TEXT NOT NULL DEFAULT '[]',
    month_day INTEGER CHECK (month_day IS NULL OR month_day = -1 OR month_day BETWEEN 1 AND 31),
    start_date TEXT NOT NULL,
    end_date TEXT,
    max_occurrences INTEGER CHECK (max_occurrences IS NULL OR max_occurrences BETWEEN 1 AND 1000),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    generated_through TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_recurrences_project ON task_recurrences (project_id);

CREATE TABLE recurrence_occurrences (
    recurrence_id INTEGER NOT NULL REFERENCES task_recurrences(id) ON DELETE CASCADE,
    occurrence_date TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('created','skipped')),
    reason TEXT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    task_key TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (recurrence_id, occurrence_date)
);
"""

# Added in schema v5: outbound webhooks. A delivery row is the "outbox": it is written in the same transaction as
# the change it announces, so an event is queued if and only if the change was committed.
WEBHOOK_SCHEMA = """
CREATE TABLE webhooks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    events TEXT NOT NULL,
    project_key TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    failure_streak INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT,
    disabled_at TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_status INTEGER,
    last_error TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE webhook_deliveries (
    id INTEGER PRIMARY KEY,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    event_id INTEGER,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','sending','delivered','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    claimed_at TEXT,
    response_status INTEGER,
    error TEXT,
    duration_ms INTEGER,
    attempt_log TEXT NOT NULL DEFAULT '[]',
    redelivery_of INTEGER,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_wd_due ON webhook_deliveries (status, next_attempt_at);
CREATE INDEX idx_wd_hook ON webhook_deliveries (webhook_id, id);
CREATE INDEX idx_wd_finished ON webhook_deliveries (finished_at);
"""

# Added in schema v6: full-text search. One FTS5 table holds a searchable copy of every task, comment and project;
# triggers keep it in step with the data, whichever code path changes it (API, CSV import, recurring tasks, CLI),
# so no service can forget to update it. Row ids encode the source - task id * 4, comment id * 4 + 1, project
# id * 4 + 2 - which makes every update and delete a primary-key operation instead of a scan.
# A Python whose SQLite lacks FTS5 gets the "like" engine instead (no index, substring matching); `search_meta`
# records which one this database uses.
def _clean(expr):
    """Strip the two control characters search uses as highlight markers from indexed text."""
    return "replace(replace(%s, char(2), ''), char(3), '')" % expr


_SEARCH_COLUMNS = "rowid, kind, ref, project_id, task_id, title, labels, body"
_SEARCH_TASKS = (
    "SELECT t.id * 4, 'task', t.id, t.project_id, t.id, %s, %s, %s FROM tasks t"
    % (
        _clean("t.title"),
        _clean("COALESCE((SELECT group_concat(label, ' ') FROM task_labels WHERE task_id = t.id), '')"),
        _clean("t.description"),
    )
)
_SEARCH_COMMENTS = (
    "SELECT c.id * 4 + 1, 'comment', c.id, t.project_id, c.task_id, '', '', %s FROM comments c JOIN tasks t ON t.id = c.task_id"
    % _clean("c.body")
)
_SEARCH_PROJECTS = (
    "SELECT p.id * 4 + 2, 'project', p.id, p.id, NULL, %s, '', %s FROM projects p"
    % (_clean("p.key || ' ' || p.name"), _clean("p.description"))
)


def _put(rows, where):
    return "INSERT INTO search_index (%s) %s WHERE %s;" % (_SEARCH_COLUMNS, rows, where)


SEARCH_BACKFILL_STATEMENTS = tuple(
    "INSERT INTO search_index (%s) %s" % (_SEARCH_COLUMNS, rows) for rows in (_SEARCH_TASKS, _SEARCH_COMMENTS, _SEARCH_PROJECTS)
)
SEARCH_BACKFILL = "\n".join(statement + ";" for statement in SEARCH_BACKFILL_STATEMENTS)

SEARCH_FTS_OBJECTS = (
    """
CREATE VIRTUAL TABLE search_index USING fts5(
    kind UNINDEXED, ref UNINDEXED, project_id UNINDEXED, task_id UNINDEXED,
    title, labels, body,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER search_tasks_ai AFTER INSERT ON tasks BEGIN
    %(task_new)s
END;
CREATE TRIGGER search_tasks_au AFTER UPDATE OF title, description ON tasks BEGIN
    DELETE FROM search_index WHERE rowid = NEW.id * 4;
    %(task_new)s
END;
CREATE TRIGGER search_tasks_project AFTER UPDATE OF project_id ON tasks WHEN OLD.project_id IS NOT NEW.project_id BEGIN
    UPDATE search_index SET project_id = NEW.project_id WHERE rowid = NEW.id * 4;
    UPDATE search_index SET project_id = NEW.project_id WHERE rowid IN (SELECT id * 4 + 1 FROM comments WHERE task_id = NEW.id);
END;
CREATE TRIGGER search_tasks_bd BEFORE DELETE ON tasks BEGIN
    DELETE FROM search_index WHERE rowid = OLD.id * 4;
    DELETE FROM search_index WHERE rowid IN (SELECT id * 4 + 1 FROM comments WHERE task_id = OLD.id);
END;

CREATE TRIGGER search_labels_ai AFTER INSERT ON task_labels BEGIN
    DELETE FROM search_index WHERE rowid = NEW.task_id * 4;
    %(task_of_label)s
END;
CREATE TRIGGER search_labels_ad AFTER DELETE ON task_labels BEGIN
    DELETE FROM search_index WHERE rowid = OLD.task_id * 4;
    %(task_of_old_label)s
END;

CREATE TRIGGER search_comments_ai AFTER INSERT ON comments BEGIN
    %(comment_new)s
END;
CREATE TRIGGER search_comments_au AFTER UPDATE OF body ON comments BEGIN
    DELETE FROM search_index WHERE rowid = NEW.id * 4 + 1;
    %(comment_new)s
END;
CREATE TRIGGER search_comments_ad AFTER DELETE ON comments BEGIN
    DELETE FROM search_index WHERE rowid = OLD.id * 4 + 1;
END;

CREATE TRIGGER search_projects_ai AFTER INSERT ON projects BEGIN
    %(project_new)s
END;
CREATE TRIGGER search_projects_au AFTER UPDATE OF key, name, description ON projects BEGIN
    DELETE FROM search_index WHERE rowid = NEW.id * 4 + 2;
    %(project_new)s
END;
CREATE TRIGGER search_projects_bd BEFORE DELETE ON projects BEGIN
    DELETE FROM search_index WHERE rowid IN (SELECT id * 4 FROM tasks WHERE project_id = OLD.id);
    DELETE FROM search_index WHERE rowid IN (SELECT c.id * 4 + 1 FROM comments c JOIN tasks t ON t.id = c.task_id WHERE t.project_id = OLD.id);
    DELETE FROM search_index WHERE rowid = OLD.id * 4 + 2;
END;
"""
    % {
        "task_new": _put(_SEARCH_TASKS, "t.id = NEW.id"),
        "task_of_label": _put(_SEARCH_TASKS, "t.id = NEW.task_id"),
        "task_of_old_label": _put(_SEARCH_TASKS, "t.id = OLD.task_id"),
        "comment_new": _put(_SEARCH_COMMENTS, "c.id = NEW.id"),
        "project_new": _put(_SEARCH_PROJECTS, "p.id = NEW.id"),
    }
)

_SEARCH_META = "CREATE TABLE search_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\nINSERT INTO search_meta VALUES ('engine', '%s');\n"


def fts5_available() -> bool:
    """Does this Python's SQLite have the FTS5 module?"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def search_migration() -> str:
    """The v5 -> v6 script. Decided when it runs, so a database is created for the SQLite it will actually be used with."""
    if fts5_available():
        return _SEARCH_META % "fts5" + SEARCH_FTS_OBJECTS + SEARCH_BACKFILL
    return _SEARCH_META % "like"


# Added in schema v7: API tokens. A user can hold several, each with its own name, expiry, role cap, read-only flag
# and optional address allow-list; only the SHA-256 of the token is stored. The users.token_hash column stays the
# user's primary token (full powers, no expiry); these are the narrower, expiring credentials for integrations.
API_TOKEN_SCHEMA = """
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    hint TEXT NOT NULL,
    role_cap TEXT NOT NULL CHECK (role_cap IN ('viewer','member','manager','admin')),
    read_only INTEGER NOT NULL DEFAULT 0 CHECK (read_only IN (0,1)),
    allowed_ips TEXT NOT NULL DEFAULT '[]',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT,
    last_used_ip TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    revoked_at TEXT,
    revoked_by INTEGER REFERENCES users(id)
);
CREATE INDEX idx_api_tokens_user ON api_tokens (user_id, id);
"""


# Added in schema v8: sprints. A sprint is a time-boxed set of tasks of one project. `sprint_items` is the history of
# which tasks were in it and when they came and went (that is what makes scope change and burndown computable);
# `outcome` stays NULL while an item is live, which the partial unique index turns into "a task is in at most one
# live sprint". Only one sprint per project can be active at a time.
SPRINT_SCHEMA = """
CREATE TABLE sprints (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    goal TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','active','closed')),
    committed_hours REAL,
    committed_count INTEGER,
    completed_hours REAL,
    completed_count INTEGER,
    carried_count INTEGER,
    dropped_count INTEGER,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    closed_at TEXT,
    UNIQUE (project_id, name),
    CHECK (end_date >= start_date)
);
CREATE UNIQUE INDEX idx_one_active_sprint ON sprints (project_id) WHERE status = 'active';

CREATE TABLE sprint_items (
    id INTEGER PRIMARY KEY,
    sprint_id INTEGER NOT NULL REFERENCES sprints(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    hours REAL NOT NULL DEFAULT 0 CHECK (hours >= 0),
    added_at TEXT NOT NULL,
    added_after_start INTEGER NOT NULL DEFAULT 0 CHECK (added_after_start IN (0,1)),
    left_at TEXT,
    done_at TEXT,
    outcome TEXT CHECK (outcome IS NULL OR outcome IN ('done','carried','dropped','removed'))
);
CREATE UNIQUE INDEX idx_one_live_sprint_per_task ON sprint_items (task_id) WHERE outcome IS NULL;
CREATE INDEX idx_sprint_items_sprint ON sprint_items (sprint_id);
"""

# Each entry upgrades a database from version N to N + 1, inside one transaction. A new database is created
# as version 1 and then walked through every migration, so a fresh install and an upgrade cannot differ.
MIGRATIONS = {1: PROJECT_FIELDS_SCHEMA, 2: AUDIT_SCHEMA, 3: RECURRENCE_SCHEMA, 4: WEBHOOK_SCHEMA, 5: search_migration, 6: API_TOKEN_SCHEMA, 7: SPRINT_SCHEMA}

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
    """Create the schema on a new database, upgrade an older one, refuse one from the future."""
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "Database schema v%d is newer than this application (v%d)" % (version, SCHEMA_VERSION)
            )
        if version == 0:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version = 1")
            version = 1
        while version < SCHEMA_VERSION:
            _migrate(conn, version)
            version += 1
        if search_engine(conn) == "fts5" and not fts5_available():
            raise RuntimeError(
                "This database uses SQLite FTS5 full-text search, which this Python's SQLite does not provide. "
                "Use a Python build with FTS5, or restore a backup made before schema v6."
            )
    finally:
        conn.close()


def search_engine(conn) -> str | None:
    """'fts5' or 'like' (None on a database older than schema v6)."""
    try:
        row = conn.execute("SELECT value FROM search_meta WHERE key = 'engine'").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def switch_search_to_fts5(conn) -> None:
    """Move a database that fell back to the 'like' engine onto FTS5 (all or nothing)."""
    script = "BEGIN IMMEDIATE;\n%s\n%s\nUPDATE search_meta SET value = 'fts5' WHERE key = 'engine';\nCOMMIT;" % (SEARCH_FTS_OBJECTS, SEARCH_BACKFILL)
    try:
        conn.executescript(script)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """All-or-nothing: a failed migration leaves the database exactly as it was."""
    body = MIGRATIONS[version]
    script = "BEGIN IMMEDIATE;\n%s\nPRAGMA user_version = %d;\nCOMMIT;" % (body() if callable(body) else body, version + 1)
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
