"""Task lookup by key (e.g. WEB-12)."""

from __future__ import annotations

from ..errors import NotFound, ValidationError
from ..util import parse_task_key

BASE = (
    "SELECT t.*, p.key AS project_key, p.status AS project_status "
    "FROM tasks t JOIN projects p ON p.id = t.project_id "
)


def key_of(row) -> str:
    return "%s-%d" % (row["project_key"], row["number"])


def get_row(conn, key):
    project_key, number = parse_task_key(key)
    row = conn.execute(BASE + "WHERE p.key = ? AND t.number = ?", (project_key, number)).fetchone()
    if row is None:
        raise NotFound("Task %s-%d not found" % (project_key, number), code="task_not_found")
    return row


def get_row_by_id(conn, task_id):
    row = conn.execute(BASE + "WHERE t.id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound("Task not found", code="task_not_found")
    return row


def keys_for_ids(conn, ids):
    """Map task ids to their keys in one query."""
    ids = list(ids)
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT t.id, p.key, t.number FROM tasks t JOIN projects p ON p.id = t.project_id "
        "WHERE t.id IN (%s)" % ",".join("?" * len(ids)),
        ids,
    )
    return {r["id"]: "%s-%d" % (r["key"], r["number"]) for r in rows}


def lookup(conn, key, field):
    """Resolve a task key given in a request body; problems are reported against ``field`` (422), not as a 404."""
    if not isinstance(key, str):
        raise ValidationError({field: "must be a task key such as WEB-12"})
    try:
        parse_task_key(key)
    except Exception:
        raise ValidationError({field: "invalid task key %r" % key}) from None
    try:
        return get_row(conn, key)
    except NotFound:
        raise ValidationError({field: "task %s does not exist" % key.upper()}) from None
