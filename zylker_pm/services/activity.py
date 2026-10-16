"""Append-only audit trail shown in project, task and admin activity feeds."""

from __future__ import annotations

import json

from ..errors import BadRequest
from ..util import truncate


def log(ctx, action, *, project_id=None, task_id=None, task_key=None, details=None):
    ctx.conn.execute(
        "INSERT INTO activity (project_id, task_id, task_key, actor_id, action, details, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (project_id, task_id, task_key, ctx.actor_id, action, json.dumps(details or {}), ctx.now_iso),
    )


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
        "SELECT a.*, u.username, u.display_name, p.key AS project_key "
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
