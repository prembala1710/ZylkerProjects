"""Task dependencies: finish-to-start, start-to-start and finish-to-finish, each with an optional lag or lead in days."""

from __future__ import annotations

from ..domain import DEFAULT_DEPENDENCY_TYPE, DEPENDENCY_TYPES, MAX_LAG_DAYS, OPEN_SQL, UNMET_DEPENDENCY_SQL
from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker
from . import activity, graph, projects, taskrefs, tasks

# One response never carries more than this many links; a project that has more is told so (``truncated``).
MAX_LISTED_DEPENDENCIES = 5000


def _details(row_type, lag_days):
    return {"type": row_type, "lag_days": lag_days}


def list_for_project(ctx, key):
    """Every dependency inside one project, so a chart can draw all of them from a single request.

    ``holding`` uses the rule behind ``blocked_by_open`` (the blocker has not finished, or for start-to-start has not
    started) and is only true while the blocked task is itself still open. ``schedule`` is the same hint the task
    dialog shows (``None`` when the blocker lacks the date the link hangs on).
    """
    project = projects.get_row(ctx.conn, key)
    rows = ctx.conn.execute(
        "SELECT d.type, d.lag_days, "
        "b.number AS b_number, b.title AS b_title, b.status AS b_status, b.start_date AS b_start, b.due_date AS b_due, "
        "t.number AS t_number, t.title AS t_title, t.status AS t_status, t.start_date AS t_start, t.due_date AS t_due, "
        "(" + UNMET_DEPENDENCY_SQL + " AND t.status IN " + OPEN_SQL + ") AS holding "
        "FROM task_dependencies d JOIN tasks b ON b.id = d.blocker_id JOIN tasks t ON t.id = d.blocked_id "
        "WHERE b.project_id = ?1 AND t.project_id = ?1 ORDER BY b.number, t.number LIMIT ?2",
        (project["id"], MAX_LISTED_DEPENDENCIES + 1),
    ).fetchall()
    prefix = project["key"] + "-"

    def end(r, side):
        return {
            "key": prefix + str(r[side + "_number"]),
            "title": r[side + "_title"],
            "status": r[side + "_status"],
            "start_date": r[side + "_start"],
            "due_date": r[side + "_due"],
        }

    items = [
        {
            "blocker": end(r, "b"),
            "blocked": end(r, "t"),
            "type": r["type"],
            "lag_days": r["lag_days"],
            "holding": bool(r["holding"]),
            "schedule": graph.schedule_hint(r["type"], r["lag_days"], r["b_start"], r["b_due"], r["t_start"], r["t_due"]),
        }
        for r in rows[:MAX_LISTED_DEPENDENCIES]
    ]
    return {"project": project["key"], "items": items, "truncated": len(rows) > MAX_LISTED_DEPENDENCIES}


def add_dependency(ctx, key, data):
    """Body: ``{"blocked_by": "WEB-1"}`` or ``{"blocks": "WEB-3"}`` (exactly one), plus optional ``type`` and ``lag_days``."""
    conn = ctx.conn
    c = Checker(data, {"blocked_by", "blocks", "type", "lag_days"})
    c.raw("blocked_by")
    c.raw("blocks")
    c.choice("type", DEPENDENCY_TYPES)
    c.integer("lag_days", minimum=-MAX_LAG_DAYS, maximum=MAX_LAG_DAYS)
    v = c.result()
    ends = [name for name in ("blocked_by", "blocks") if name in v]
    if len(ends) != 1:
        raise ValidationError({"blocked_by": "provide exactly one of 'blocked_by' or 'blocks'"})
    dep_type = v.get("type", DEFAULT_DEPENDENCY_TYPE)
    lag_days = v.get("lag_days", 0)
    row = taskrefs.get_row(conn, key)
    project = projects.get_row(conn, row["project_key"])
    projects.require_write(ctx, project)

    field = ends[0]
    other = taskrefs.lookup(conn, v[field], field)
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
    cycle = graph.find_cycle(conn, row["project_id"], [(blocker["id"], blocked["id"], dep_type)])
    if cycle:
        raise tasks._cycle_error(ctx, cycle)

    conn.execute(
        "INSERT INTO task_dependencies (blocker_id, blocked_id, created_at, type, lag_days) VALUES (?,?,?,?,?)",
        (blocker["id"], blocked["id"], ctx.now_iso, dep_type, lag_days),
    )
    activity.log(
        ctx,
        "task.dependency_added",
        project_id=row["project_id"],
        task_id=row["id"],
        task_key=taskrefs.key_of(row),
        details={"blocker": taskrefs.key_of(blocker), "blocked": taskrefs.key_of(blocked), **_details(dep_type, lag_days)},
    )
    return tasks.get_task(ctx, key)


def update_dependency(ctx, key, other_key, data):
    """Change the ``type`` and/or ``lag_days`` of an existing dependency between two tasks (either direction)."""
    conn = ctx.conn
    c = Checker(data, {"type", "lag_days"}, partial=True)
    c.choice("type", DEPENDENCY_TYPES)
    c.integer("lag_days", minimum=-MAX_LAG_DAYS, maximum=MAX_LAG_DAYS)
    v = c.result()
    if not v:
        raise ValidationError({"type": "provide 'type' and/or 'lag_days'"})
    row = taskrefs.get_row(conn, key)
    projects.require_write(ctx, projects.get_row(conn, row["project_key"]))
    other = taskrefs.get_row(conn, other_key)
    link = conn.execute(
        "SELECT * FROM task_dependencies WHERE (blocker_id = ?1 AND blocked_id = ?2) OR (blocker_id = ?2 AND blocked_id = ?1)",
        (row["id"], other["id"]),
    ).fetchone()
    if link is None:
        raise NotFound(
            "No dependency between %s and %s" % (taskrefs.key_of(row), taskrefs.key_of(other)), code="dependency_not_found"
        )
    new_type = v.get("type", link["type"])
    new_lag = v.get("lag_days", link["lag_days"])
    if (new_type, new_lag) == (link["type"], link["lag_days"]):
        return tasks.get_task(ctx, key)
    if new_type != link["type"]:
        old_edge = (link["blocker_id"], link["blocked_id"], link["type"])
        cycle = graph.find_cycle(conn, row["project_id"], [(link["blocker_id"], link["blocked_id"], new_type)], [old_edge])
        if cycle:
            raise tasks._cycle_error(ctx, cycle)
    conn.execute(
        "UPDATE task_dependencies SET type = ?, lag_days = ? WHERE blocker_id = ? AND blocked_id = ?",
        (new_type, new_lag, link["blocker_id"], link["blocked_id"]),
    )
    keys = taskrefs.keys_for_ids(conn, [link["blocker_id"], link["blocked_id"]])
    activity.log(
        ctx,
        "task.dependency_updated",
        project_id=row["project_id"],
        task_id=row["id"],
        task_key=taskrefs.key_of(row),
        details={
            "blocker": keys[link["blocker_id"]],
            "blocked": keys[link["blocked_id"]],
            "changes": {
                name: [old, new]
                for name, old, new in (("type", link["type"], new_type), ("lag_days", link["lag_days"], new_lag))
                if old != new
            },
        },
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
