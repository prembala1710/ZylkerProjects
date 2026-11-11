"""Baselines: a frozen copy of a project's plan, and how far the project has drifted from it since.

Setting a baseline records, for every task that is not cancelled, its title, dates, estimate and status, and the
project's milestones, as they are at that moment. A baseline is never edited, only deleted. :func:`variance` lays the
live project over it and says, task by task and for the project as a whole, what moved.

The rules (the README has them in plain words):

* Only *leaf* tasks (those without subtasks) are compared: a parent has no work of its own.
* A task's **finish** is the day it was completed when it is done, otherwise its due date. **Slip** is the finish now minus
  the finish in the baseline, in calendar days. So a task that was finished late counts as late even though nobody moved
  its due date, and a task that was already done when the baseline was set can never slip.
* A task that has been cancelled or deleted since is *dropped*, a task that is new is *added*, and a leaf that has been
  split into subtasks since is neither (its subtasks are *added*).
* Scope is the sum of the estimates of the leaf tasks; a task without an estimate counts as 0 hours.

The tables are created like the attachment tables: with ``CREATE TABLE IF NOT EXISTS`` by :func:`ensure_schema` when an
``App`` starts, independently of ``PRAGMA user_version``. They are purely additive, and a baseline does not point at tasks
(it keeps their numbers and titles), so deleting a task never changes what the baseline says about the plan.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import date, datetime, timezone

from ..db import connect
from ..domain import OPEN_STATUSES
from ..errors import Conflict, NotFound
from ..util import Checker
from . import activity, projects, users

MAX_BASELINES = 10  # per project
MAX_BASELINE_TASKS = 10000  # a project with more tasks than this cannot be baselined
MAX_REPORT_TASKS = 5000  # rows of ``tasks`` in one comparison
MAX_LISTED_CHANGES = 200  # rows of ``added`` and ``dropped`` in one comparison
MAX_NAME_CHARS = 60
MAX_NOTE_CHARS = 500

BASELINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS baselines (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_baselines_name ON baselines (project_id, name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS baseline_tasks (
    baseline_id INTEGER NOT NULL REFERENCES baselines(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    is_leaf INTEGER NOT NULL CHECK (is_leaf IN (0,1)),
    status TEXT NOT NULL,
    start_date TEXT,
    due_date TEXT,
    estimate_hours REAL,
    completed_date TEXT,
    PRIMARY KEY (baseline_id, number)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS baseline_milestones (
    baseline_id INTEGER NOT NULL REFERENCES baselines(id) ON DELETE CASCADE,
    milestone_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    due_date TEXT NOT NULL,
    PRIMARY KEY (baseline_id, milestone_id)
) WITHOUT ROWID;
"""


def ensure_schema(path: str) -> None:
    """Create the baseline tables in an initialised database file (no-op for new, missing or in-memory ones)."""
    if path == ":memory:" or not os.path.exists(path):
        return
    conn = connect(path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] > 0:
            conn.executescript(BASELINE_SCHEMA)
    finally:
        conn.close()


def _require_schema(ctx):
    found = ctx.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'baselines'").fetchone()
    if found is None:
        raise Conflict(
            "The baseline tables are missing from this database. Restart the server (or run `zylker-pm init`) to create them.",
            code="baselines_not_initialised",
        )


# ---------------------------------------------------------------------------------------------- comparing (pure)


def _days(first, second):
    """Whole calendar days from one ISO date to another (negative when the second is earlier)."""
    return (date.fromisoformat(second) - date.fromisoformat(first)).days


def _shift(first, second):
    return _days(first, second) if first and second else None


def _finish(task):
    """The day a task was completed (done) or is planned to be done (anything else); ``None`` without a date."""
    if task["status"] == "done" and task.get("completed_date"):
        return task["completed_date"]
    return task["due_date"]


def _compare_task(key, before, now, today):
    finish_before, finish_now = _finish(before), _finish(now)
    slip = _shift(finish_before, finish_now)
    return {
        "key": "%s-%d" % (key, now["number"]),
        "number": now["number"],
        "title": now["title"],
        "status": now["status"],
        "assignee": now.get("assignee"),
        "state": "no_dates" if slip is None else "slipped" if slip > 0 else "ahead" if slip < 0 else "on_plan",
        "slip_days": slip,
        "overdue": now["status"] in OPEN_STATUSES and bool(now["due_date"]) and now["due_date"] < today,
        "start_shift_days": _shift(before["start_date"], now["start_date"]),
        "due_shift_days": _shift(before["due_date"], now["due_date"]),
        "baseline": {
            "status": before["status"],
            "start_date": before["start_date"],
            "due_date": before["due_date"],
            "finish": finish_before,
            "estimate_hours": before["estimate_hours"],
        },
        "now": {
            "start_date": now["start_date"],
            "due_date": now["due_date"],
            "finish": finish_now,
            "finished": now["status"] == "done",
            "estimate_hours": now["estimate_hours"],
        },
    }


_STATE_ORDER = {"slipped": 0, "ahead": 1, "on_plan": 2, "no_dates": 3}


def compare(key, planned, current, planned_milestones, current_milestones, today):
    """Lay the live project over a baseline. Everything is passed in as plain dicts, so this needs no database.

    ``planned`` and ``current`` are lists of ``{number, title, status, start_date, due_date, estimate_hours,
    completed_date, is_leaf}`` (current ones may also carry ``assignee``); milestones are ``{id, name, due_date}``;
    ``today`` is an ISO date. Returns the ``summary``, the compared ``tasks`` (worst slip first), the ``added`` and
    ``dropped`` tasks and the ``milestones``.
    """
    leaves = {t["number"]: t for t in planned if t["is_leaf"]}
    live = {t["number"]: t for t in current}
    compared, added, dropped, split = [], [], [], 0
    for number in sorted(leaves):
        before, now = leaves[number], live.get(number)
        if now is None or now["status"] == "cancelled":
            dropped.append(
                {
                    "key": "%s-%d" % (key, number),
                    "number": number,
                    "title": before["title"],
                    "reason": "deleted" if now is None else "cancelled",
                    "baseline": {"due_date": before["due_date"], "estimate_hours": before["estimate_hours"]},
                }
            )
        elif not now["is_leaf"]:
            split += 1
        else:
            compared.append(_compare_task(key, before, now, today))
    for number in sorted(live):
        now = live[number]
        if now["is_leaf"] and now["status"] != "cancelled" and number not in leaves:
            added.append(
                {
                    "key": "%s-%d" % (key, number),
                    "number": number,
                    "title": now["title"],
                    "status": now["status"],
                    "assignee": now.get("assignee"),
                    "start_date": now["start_date"],
                    "due_date": now["due_date"],
                    "finish": _finish(now),
                    "estimate_hours": now["estimate_hours"],
                    "overdue": now["status"] in OPEN_STATUSES and bool(now["due_date"]) and now["due_date"] < today,
                }
            )
    compared.sort(key=lambda r: (_STATE_ORDER[r["state"]], -abs(r["slip_days"] or 0), r["number"]))

    states = Counter(r["state"] for r in compared)
    slips = [r["slip_days"] for r in compared if r["state"] == "slipped"]
    finishes_before = [f for f in map(_finish, leaves.values()) if f]
    finishes_now = [r["now"]["finish"] for r in compared if r["now"]["finish"]] + [a["finish"] for a in added if a["finish"]]
    finish_before, finish_now = max(finishes_before, default=None), max(finishes_now, default=None)
    hours_before = round(sum(t["estimate_hours"] or 0 for t in leaves.values()), 2)
    hours_now = round(sum(r["now"]["estimate_hours"] or 0 for r in compared) + sum(a["estimate_hours"] or 0 for a in added), 2)

    live_milestones = {m["id"]: m for m in current_milestones}
    milestone_rows = []
    for m in sorted(planned_milestones, key=lambda m: (m["due_date"], m["id"])):
        now = live_milestones.get(m["id"])
        slip = _days(m["due_date"], now["due_date"]) if now else None
        milestone_rows.append(
            {
                "id": m["id"],
                "name": now["name"] if now else m["name"],
                "state": "dropped" if now is None else "slipped" if slip > 0 else "ahead" if slip < 0 else "on_plan",
                "baseline_due_date": m["due_date"],
                "due_date": now["due_date"] if now else None,
                "slip_days": slip,
            }
        )
    planned_ids = {m["id"] for m in planned_milestones}
    milestones_added = [
        {"id": m["id"], "name": m["name"], "due_date": m["due_date"]}
        for m in sorted(current_milestones, key=lambda m: (m["due_date"], m["id"]))
        if m["id"] not in planned_ids
    ]
    milestone_states = Counter(r["state"] for r in milestone_rows)

    return {
        "summary": {
            "tasks": {
                "planned": len(leaves),
                "now": len(compared) + len(added),
                "compared": len(compared),
                "on_plan": states["on_plan"],
                "slipped": states["slipped"],
                "ahead": states["ahead"],
                "no_dates": states["no_dates"],
                "added": len(added),
                "dropped": len(dropped),
                "split": split,
            },
            "slip": {"max_days": max(slips, default=0), "average_days": round(sum(slips) / len(slips), 1) if slips else 0.0},
            "overdue": sum(1 for r in compared if r["overdue"]) + sum(1 for a in added if a["overdue"]),
            "finish": {"baseline": finish_before, "now": finish_now, "shift_days": _shift(finish_before, finish_now)},
            "scope_hours": {"baseline": hours_before, "now": hours_now, "change": round(hours_now - hours_before, 2)},
            "milestones": {
                "planned": len(milestone_rows),
                "on_plan": milestone_states["on_plan"],
                "slipped": milestone_states["slipped"],
                "ahead": milestone_states["ahead"],
                "dropped": milestone_states["dropped"],
                "added": len(milestones_added),
            },
        },
        "tasks": compared[:MAX_REPORT_TASKS],
        "tasks_truncated": len(compared) > MAX_REPORT_TASKS,
        "added": added[:MAX_LISTED_CHANGES],
        "dropped": dropped[:MAX_LISTED_CHANGES],
        "milestones": {"items": milestone_rows, "added": milestones_added},
    }


# ---------------------------------------------------------------------------------------------------- the database

_SUMMARY = (
    "SELECT b.*, u.username, u.display_name, p.key AS project_key, "
    "(SELECT COUNT(*) FROM baseline_tasks t WHERE t.baseline_id = b.id) AS task_count, "
    "(SELECT COUNT(*) FROM baseline_tasks t WHERE t.baseline_id = b.id AND t.is_leaf = 1) AS leaf_count, "
    "(SELECT COALESCE(SUM(t.estimate_hours), 0) FROM baseline_tasks t WHERE t.baseline_id = b.id AND t.is_leaf = 1) AS hours, "
    "(SELECT MAX(CASE WHEN t.status = 'done' AND t.completed_date IS NOT NULL THEN t.completed_date ELSE t.due_date END) "
    "FROM baseline_tasks t WHERE t.baseline_id = b.id AND t.is_leaf = 1) AS finish, "
    "(SELECT COUNT(*) FROM baseline_milestones m WHERE m.baseline_id = b.id) AS milestone_count "
    "FROM baselines b JOIN projects p ON p.id = b.project_id LEFT JOIN users u ON u.id = b.created_by "
)


def to_dict(row):
    return {
        "id": row["id"],
        "project": row["project_key"],
        "name": row["name"],
        "note": row["note"],
        "created_at": row["created_at"],
        "created_by": users.ref(row["created_by"], row["username"], row["display_name"]),
        "tasks": row["task_count"],
        "leaf_tasks": row["leaf_count"],
        "milestones": row["milestone_count"],
        "estimate_hours": round(row["hours"], 2),
        "finish": row["finish"],
    }


def _get(conn, project, baseline_id):
    row = conn.execute(_SUMMARY + "WHERE b.id = ? AND b.project_id = ?", (baseline_id, project["id"])).fetchone()
    if row is None:
        raise NotFound("Baseline not found", code="baseline_not_found")
    return row


def _local_date(ts, tz):
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()


# Control characters and invisible direction overrides could make a name read as something else. A note may hold line breaks.
_INVISIBLE = "\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2060\u2066-\u2069\ufeff"
_BAD_NAME = re.compile("[\x00-\x1f\x7f-\x9f%s]" % _INVISIBLE)
_BAD_NOTE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f%s]" % _INVISIBLE)


def list_baselines(ctx, key):
    _require_schema(ctx)
    project = projects.get_row(ctx.conn, key)
    rows = ctx.conn.execute(_SUMMARY + "WHERE b.project_id = ? ORDER BY b.id DESC", (project["id"],)).fetchall()
    return {"items": [to_dict(r) for r in rows], "limits": {"max_baselines": MAX_BASELINES}}


def create(ctx, key, data):
    """Freeze the plan of a project as it is now. ``{"name"?, "note"?}``; the name defaults to ``Baseline N``."""
    _require_schema(ctx)
    conn = ctx.conn
    project = projects.get_row(conn, key)
    projects.require_write(ctx, project)
    c = Checker(data, {"name", "note"})
    c.text("name", max_len=MAX_NAME_CHARS)
    c.text("note", max_len=MAX_NOTE_CHARS)
    if _BAD_NAME.search(c.out.get("name", "")):
        c.errors["name"] = "must not contain control or direction characters"
    if _BAD_NOTE.search(c.out.get("note", "")):
        c.errors["note"] = "must not contain control or direction characters"
    v = c.result()

    taken = {r[0].lower() for r in conn.execute("SELECT name FROM baselines WHERE project_id = ?", (project["id"],))}
    if len(taken) >= MAX_BASELINES:
        raise Conflict("A project can have at most %d baselines; delete one first" % MAX_BASELINES, code="too_many_baselines")
    name = v.get("name")
    if name:
        if name.lower() in taken:
            raise Conflict("This project already has a baseline called %r" % name, code="baseline_name_taken")
    else:
        n = len(taken) + 1
        while "baseline %d" % n in taken:
            n += 1
        name = "Baseline %d" % n

    tasks = conn.execute(
        "SELECT t.number, t.title, t.status, t.start_date, t.due_date, t.estimate_hours, t.completed_at, "
        "NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id) AS is_leaf "
        "FROM tasks t WHERE t.project_id = ? AND t.status <> 'cancelled' ORDER BY t.number LIMIT ?",
        (project["id"], MAX_BASELINE_TASKS + 1),
    ).fetchall()
    if len(tasks) > MAX_BASELINE_TASKS:
        raise Conflict("This project has more than %d tasks, which is more than a baseline can hold" % MAX_BASELINE_TASKS, code="baseline_too_large")
    milestones = conn.execute("SELECT id, name, due_date FROM milestones WHERE project_id = ? ORDER BY id", (project["id"],)).fetchall()

    cur = conn.execute(
        "INSERT INTO baselines (project_id, name, note, created_by, created_at) VALUES (?,?,?,?,?)",
        (project["id"], name, v.get("note", ""), ctx.actor_id, ctx.now_iso),
    )
    baseline_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO baseline_tasks (baseline_id, number, title, is_leaf, status, start_date, due_date, estimate_hours, completed_date) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                baseline_id,
                t["number"],
                t["title"],
                t["is_leaf"],
                t["status"],
                t["start_date"],
                t["due_date"],
                t["estimate_hours"],
                _local_date(t["completed_at"], ctx.tz) if t["status"] == "done" else None,
            )
            for t in tasks
        ],
    )
    conn.executemany(
        "INSERT INTO baseline_milestones (baseline_id, milestone_id, name, due_date) VALUES (?,?,?,?)",
        [(baseline_id, m["id"], m["name"], m["due_date"]) for m in milestones],
    )
    activity.log(
        ctx,
        "baseline.created",
        project_id=project["id"],
        details={"id": baseline_id, "name": name, "tasks": len(tasks), "milestones": len(milestones)},
    )
    return to_dict(_get(conn, project, baseline_id))


def variance(ctx, key, baseline_id):
    """The baseline and how the project as it is now compares with it (see :func:`compare`)."""
    _require_schema(ctx)
    conn = ctx.conn
    project = projects.get_row(conn, key)
    row = _get(conn, project, baseline_id)
    planned = [
        {**dict(r), "is_leaf": bool(r["is_leaf"])}
        for r in conn.execute(
            "SELECT number, title, is_leaf, status, start_date, due_date, estimate_hours, completed_date FROM baseline_tasks WHERE baseline_id = ?",
            (baseline_id,),
        )
    ]
    planned_milestones = [dict(r) for r in conn.execute("SELECT milestone_id AS id, name, due_date FROM baseline_milestones WHERE baseline_id = ?", (baseline_id,))]
    current = [
        {
            "number": r["number"],
            "title": r["title"],
            "status": r["status"],
            "start_date": r["start_date"],
            "due_date": r["due_date"],
            "estimate_hours": r["estimate_hours"],
            "completed_date": _local_date(r["completed_at"], ctx.tz) if r["status"] == "done" else None,
            "is_leaf": bool(r["is_leaf"]),
            "assignee": users.ref(r["assignee_id"], r["username"], r["display_name"]),
        }
        for r in conn.execute(
            "SELECT t.number, t.title, t.status, t.start_date, t.due_date, t.estimate_hours, t.completed_at, t.assignee_id, "
            "u.username, u.display_name, NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id) AS is_leaf "
            "FROM tasks t LEFT JOIN users u ON u.id = t.assignee_id WHERE t.project_id = ?",
            (project["id"],),
        )
    ]
    current_milestones = [dict(r) for r in conn.execute("SELECT id, name, due_date FROM milestones WHERE project_id = ?", (project["id"],))]
    today = ctx.today.isoformat()
    return {
        "baseline": to_dict(row),
        "project": project["key"],
        "as_of": today,
        **compare(project["key"], planned, current, planned_milestones, current_milestones, today),
    }


def delete(ctx, key, baseline_id):
    _require_schema(ctx)
    conn = ctx.conn
    project = projects.get_row(conn, key)
    projects.require_write(ctx, project)
    row = _get(conn, project, baseline_id)
    conn.execute("DELETE FROM baselines WHERE id = ?", (row["id"],))  # its tasks and milestones go with it
    activity.log(ctx, "baseline.deleted", project_id=project["id"], details={"id": row["id"], "name": row["name"]})
