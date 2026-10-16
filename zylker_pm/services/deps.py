"""Finish-to-start task dependencies."""

from __future__ import annotations

from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker, parse_task_key
from . import activity, graph, projects, taskrefs, tasks


def _lookup(conn, key, field):
    if not isinstance(key, str):
        raise ValidationError({field: "must be a task key such as WEB-12"})
    try:
        parse_task_key(key)
    except Exception:
        raise ValidationError({field: "invalid task key %r" % key}) from None
    try:
        return taskrefs.get_row(conn, key)
    except NotFound:
        raise ValidationError({field: "task %s does not exist" % key.upper()}) from None


def add_dependency(ctx, key, data):
    """Body: ``{"blocked_by": "WEB-1"}`` or ``{"blocks": "WEB-3"}`` (exactly one)."""
    conn = ctx.conn
    c = Checker(data, {"blocked_by", "blocks"})
    c.raw("blocked_by")
    c.raw("blocks")
    v = c.result()
    if len(v) != 1:
        raise ValidationError({"blocked_by": "provide exactly one of 'blocked_by' or 'blocks'"})
    row = taskrefs.get_row(conn, key)
    project = projects.get_row(conn, row["project_key"])
    projects.require_write(ctx, project)

    field = "blocked_by" if "blocked_by" in v else "blocks"
    other = _lookup(conn, v[field], field)
    if other["project_id"] != row["project_id"]:
        raise ValidationError({field: "dependencies must be between tasks of the same project"})
    if other["id"] == row["id"]:
        raise ValidationError({field: "a task cannot depend on itself"})
    blocker, blocked = (other, row) if field == "blocked_by" else (row, other)

    exists = conn.execute(
        "SELECT 1 FROM task_dependencies WHERE blocker_id = ? AND blocked_id = ?", (blocker["id"], blocked["id"])
    ).fetchone()
    if exists:
        raise Conflict("That dependency already exists", code="dependency_exists")
    cycle = graph.find_cycle(conn, row["project_id"], [(blocker["id"], blocked["id"])])
    if cycle:
        raise tasks._cycle_error(ctx, cycle)

    conn.execute(
        "INSERT INTO task_dependencies (blocker_id, blocked_id, created_at) VALUES (?,?,?)",
        (blocker["id"], blocked["id"], ctx.now_iso),
    )
    activity.log(
        ctx,
        "task.dependency_added",
        project_id=row["project_id"],
        task_id=row["id"],
        task_key=taskrefs.key_of(row),
        details={"blocker": taskrefs.key_of(blocker), "blocked": taskrefs.key_of(blocked)},
    )
    return tasks.get_task(ctx, key)


def remove_dependency(ctx, key, other_key):
    conn = ctx.conn
    row = taskrefs.get_row(conn, key)
    project = projects.get_row(conn, row["project_key"])
    projects.require_write(ctx, project)
    other = taskrefs.get_row(conn, other_key)
    cur = conn.execute(
        "DELETE FROM task_dependencies WHERE (blocker_id = ?1 AND blocked_id = ?2) "
        "OR (blocker_id = ?2 AND blocked_id = ?1)",
        (row["id"], other["id"]),
    )
    if cur.rowcount == 0:
        raise NotFound("No dependency between %s and %s" % (taskrefs.key_of(row), taskrefs.key_of(other)),
                       code="dependency_not_found")  # fmt: skip
    activity.log(
        ctx,
        "task.dependency_removed",
        project_id=row["project_id"],
        task_id=row["id"],
        task_key=taskrefs.key_of(row),
        details={"other": taskrefs.key_of(other)},
    )
    return tasks.get_task(ctx, key)
