"""Milestones: dated goals that tasks can be linked to."""

from __future__ import annotations

from ..errors import NotFound
from ..util import Checker
from . import activity, projects

MILESTONE_SELECT = (
    "SELECT m.*, "
    "(SELECT COUNT(*) FROM tasks t WHERE t.milestone_id = m.id AND t.status <> 'cancelled') AS total, "
    "(SELECT COUNT(*) FROM tasks t WHERE t.milestone_id = m.id AND t.status = 'done') AS done "
    "FROM milestones m "
)


def to_dict(row, today_iso):
    total, done = row["total"], row["done"]
    if total > 0 and done == total:
        status = "achieved"
    elif row["due_date"] < today_iso:
        status = "overdue"
    else:
        status = "open"
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "due_date": row["due_date"],
        "status": status,
        "tasks_total": total,
        "tasks_done": done,
        "progress": round(100.0 * done / total, 1) if total else 0.0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get(conn, milestone_id):
    row = conn.execute(MILESTONE_SELECT + "WHERE m.id = ?", (milestone_id,)).fetchone()
    if row is None:
        raise NotFound("Milestone not found", code="milestone_not_found")
    return row


def list_milestones(ctx, project_key):
    project = projects.get_row(ctx.conn, project_key)
    rows = ctx.conn.execute(MILESTONE_SELECT + "WHERE m.project_id = ? ORDER BY m.due_date, m.id", (project["id"],))
    today = ctx.today.isoformat()
    return [to_dict(r, today) for r in rows]


def create_milestone(ctx, project_key, data):
    project = projects.get_row(ctx.conn, project_key)
    projects.require_write(ctx, project)
    c = Checker(data, {"name", "description", "due_date"})
    c.text("name", required=True, min_len=1, max_len=100)
    c.text("description", max_len=2000)
    c.date("due_date", required=True, nullable=False)
    v = c.result()
    cur = ctx.conn.execute(
        "INSERT INTO milestones (project_id, name, description, due_date, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (project["id"], v["name"], v.get("description", ""), v["due_date"], ctx.now_iso, ctx.now_iso),
    )
    activity.log(
        ctx, "milestone.created", project_id=project["id"], details={"name": v["name"], "due_date": v["due_date"]}
    )
    return to_dict(_get(ctx.conn, cur.lastrowid), ctx.today.isoformat())


def _project_of(ctx, milestone_id):
    row = _get(ctx.conn, milestone_id)
    project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
    projects.require_write(ctx, project)
    return row, project


def update_milestone(ctx, milestone_id, data):
    row, project = _project_of(ctx, milestone_id)
    c = Checker(data, {"name", "description", "due_date"}, partial=True)
    c.text("name", min_len=1, max_len=100)
    c.text("description", max_len=2000)
    c.date("due_date", nullable=False)
    v = c.result()
    changes = {f: [row[f], val] for f, val in v.items() if row[f] != val}
    if changes:
        ctx.conn.execute(
            "UPDATE milestones SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % f for f in changes),
            [c_[1] for c_ in changes.values()] + [ctx.now_iso, row["id"]],
        )
        activity.log(ctx, "milestone.updated", project_id=project["id"], details={"id": row["id"], "changes": changes})
    return to_dict(_get(ctx.conn, row["id"]), ctx.today.isoformat())


def delete_milestone(ctx, milestone_id):
    row, project = _project_of(ctx, milestone_id)
    ctx.conn.execute("DELETE FROM milestones WHERE id = ?", (row["id"],))
    activity.log(ctx, "milestone.deleted", project_id=project["id"], details={"name": row["name"]})
