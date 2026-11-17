"""Append-only audit trail shown in project, task and admin activity feeds."""

from __future__ import annotations

import json

from ..errors import BadRequest
from ..util import truncate


def log(ctx, action, *, project_id=None, project_key=None, task_id=None, task_key=None, details=None):
    """Record one event. The project key is stored beside the id so history stays readable after a delete."""
    if project_key is None and project_id is not None:
        row = ctx.conn.execute("SELECT key FROM projects WHERE id = ?", (project_id,)).fetchone()
        project_key = row[0] if row else None
    ctx.conn.execute(
        "INSERT INTO activity (project_id, project_key, task_id, task_key, actor_id, action, details, created_at, ip, request_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project_id, project_key, task_id, task_key, ctx.actor_id, action, json.dumps(details or {}), ctx.now_iso, ctx.ip, ctx.request_id),
    )


def detach_task(conn, task_ids):
    """A deleted task's history keeps its ``task_key`` but lets go of the numeric id, because SQLite may hand
    that id to the next task created - which must not inherit the old task's history."""
    ids = list(task_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        conn.execute("UPDATE activity SET task_id = NULL WHERE task_id IN (%s)" % ",".join("?" * len(chunk)), chunk)


def detach_project(conn, project_id):
    """Same for a deleted project: the history stays (with its ``project_key``), unlinked from the reusable id."""
    conn.execute("UPDATE activity SET project_id = NULL WHERE project_id = ?", (project_id,))


def diff_value(value):
    """Keep audit entries compact: long strings are truncated."""
    return truncate(value, 200) if isinstance(value, str) else value


def list_activity(ctx, *, project_id=None, task_id=None, limit=50, before=None):
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise BadRequest("'limit' must be an integer") from None
    where, args = [], []
    if project_id is not None:
        where.append("a.project_id = ?")
        args.append(project_id)
    if task_id is not None:
        where.append("a.task_id = ?")
        args.append(task_id)
    if before is not None:
        try:
            args.append(int(before))
        except (TypeError, ValueError):
            raise BadRequest("'before' must be an activity id") from None
        where.append("a.id < ?")
    sql = (
        "SELECT a.*, u.username, u.display_name, COALESCE(p.key, a.project_key) AS project_key "
        "FROM activity a LEFT JOIN users u ON u.id = a.actor_id "
        "LEFT JOIN projects p ON p.id = a.project_id "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY a.id DESC LIMIT ?"
    )
    rows = ctx.conn.execute(sql, args + [limit]).fetchall()
    items = [
        {
            "id": r["id"],
            "action": r["action"],
            "actor": {"id": r["actor_id"], "username": r["username"], "display_name": r["display_name"]}
            if r["actor_id"]
            else None,
            "project": r["project_key"],
            "task": r["task_key"],
            "details": json.loads(r["details"] or "{}"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"items": items, "next_before": items[-1]["id"] if len(items) == limit else None}
