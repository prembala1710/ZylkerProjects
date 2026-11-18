"""Sprints: time-boxed sets of a project's tasks, planned against capacity and measured while they run.

Lifecycle: ``planned`` -> ``active`` -> ``closed``. A project has at most one active sprint, and a task is in at most
one live sprint (the database enforces both). Everything that happens to a task's membership is kept as history
(``sprint_items``), which is what makes scope change, burndown and velocity computable afterwards.

Sizes are the tasks' ``estimate_hours`` as they were when the task joined the sprint (a task without an estimate counts
as 0 hours and is reported as unestimated). Days are the server's timezone; the working week is Monday to Friday.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from ..domain import CLOSED_SQL, CLOSED_STATUSES, OPEN_SQL
from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker, parse_date
from . import activity, projects, taskrefs, users

MAX_DAYS = 60  # the longest a sprint may run
MAX_TASKS_PER_REQUEST = 100
MAX_SPRINTS_PER_PROJECT = 500
BEHIND_TOLERANCE = 0.2  # remaining work may exceed the ideal line by this share of the commitment before it is "at risk"
SCOPE_GROWTH_LIMIT = 0.25  # more than this much added after the start is "at risk"
VELOCITY_WINDOW = 3
STATUSES = ("planned", "active", "closed")


# ------------------------------------------------------------------------------ small helpers


def _local_date(ctx, stamp):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ctx.tz).date()


def working_days(first: date, last: date) -> int:
    """Monday-to-Friday days from ``first`` to ``last``, both included."""
    if last < first:
        return 0
    return sum(1 for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() < 5)


def _round(x):
    return round(float(x), 2)


def _get_row(ctx, sprint_id):
    row = ctx.conn.execute(
        "SELECT s.*, p.key AS project_key, p.status AS project_status FROM sprints s JOIN projects p ON p.id = s.project_id WHERE s.id = ?",
        (sprint_id,),
    ).fetchone()
    if row is None:
        raise NotFound("Sprint not found", code="sprint_not_found")
    return row


def _project_of(ctx, row):
    return projects.get_row(ctx.conn, row["project_key"])


def _items(ctx, sprint_id):
    return ctx.conn.execute(
        "SELECT i.*, t.number, t.title, t.status AS t_status, t.priority, t.completed_at, t.assignee_id, t.estimate_hours AS t_estimate, "
        "p.key AS pkey, u.username, u.display_name FROM sprint_items i JOIN tasks t ON t.id = i.task_id "
        "JOIN projects p ON p.id = t.project_id LEFT JOIN users u ON u.id = t.assignee_id WHERE i.sprint_id = ? ORDER BY i.id",
        (sprint_id,),
    ).fetchall()


def _is_done(item):
    return item["outcome"] == "done" or (item["outcome"] is None and item["t_status"] == "done")


def _is_closed(item):
    """Finished (done or cancelled) as far as this sprint is concerned."""
    if item["outcome"] in ("done", "dropped"):
        return True
    return item["outcome"] is None and item["t_status"] in CLOSED_STATUSES


def _closed_stamp(item):
    return item["done_at"] if item["outcome"] in ("done", "dropped") else item["completed_at"]


def _counted(item):
    """Part of the sprint's scope (everything but what was taken out again)."""
    return item["outcome"] != "removed"


# ------------------------------------------------------------------------------ serialising


def _plan(ctx, row, items):
    """Capacity against load for a sprint that has not finished: who can do how much, and who is over."""
    today = ctx.today
    first = max(parse_date(row["start_date"]), today) if row["status"] == "active" else parse_date(row["start_date"])
    days = working_days(first, parse_date(row["end_date"]))
    live = [i for i in items if i["outcome"] is None and not _is_closed(i)]
    load = {}
    for i in live:
        load[i["assignee_id"]] = load.get(i["assignee_id"], 0.0) + i["hours"]
    project_id = row["project_id"]
    people = {}
    for m in projects.member_rows(ctx.conn, project_id):
        if m["active"]:
            people[m["id"]] = m
    for uid in load:
        if uid is not None and uid not in people:
            people[uid] = users.get_row(ctx.conn, uid)
    out = []
    for uid, u in people.items():
        capacity = u["weekly_capacity_hours"] / 5.0 * days
        planned = load.get(uid, 0.0)
        out.append({
            "user": users.ref(u["id"], u["username"], u["display_name"]), "capacity_hours": _round(capacity),
            "planned_hours": _round(planned), "utilisation": _round(planned / capacity) if capacity else None,
            "overallocated": planned > capacity + 1e-9,
        })  # fmt: skip
    out.sort(key=lambda p: (-p["planned_hours"], p["user"]["username"]))
    capacity_total = sum(p["capacity_hours"] for p in out)
    planned_total = sum(i["hours"] for i in live)
    return {
        "working_days": days,
        "capacity_hours": _round(capacity_total),
        "planned_hours": _round(planned_total),
        "utilisation": _round(planned_total / capacity_total) if capacity_total else None,
        "unassigned_hours": _round(load.get(None, 0.0)),
        "unestimated_tasks": sum(1 for i in live if i["t_estimate"] is None),
        "people": out,
    }


def _health(ctx, row, items, burn):
    today = ctx.today
    end = parse_date(row["end_date"])
    remaining = sum(i["hours"] for i in items if _counted(i) and not _is_closed(i))
    reasons, status = [], "on_track"
    if today > end and remaining > 0:
        status = "off_track"
        reasons.append("The sprint ended on %s and %g h of work is still open" % (end.isoformat(), _round(remaining)))
    else:
        base = row["committed_hours"] or 0
        today_row = next((d for d in burn if d["date"] == today.isoformat()), None)
        if today_row and today_row["remaining"] is not None and base > 0 and today_row["remaining"] > today_row["ideal"] + BEHIND_TOLERANCE * base:
            status = "at_risk"
            reasons.append("%g h remaining against an ideal of %g h" % (today_row["remaining"], today_row["ideal"]))
        added = sum(i["hours"] for i in items if i["added_after_start"] and _counted(i))
        if base > 0 and added > SCOPE_GROWTH_LIMIT * base:
            status = "at_risk"
            reasons.append("Scope grew by %g h after the start (%d%% of the commitment)" % (_round(added), round(100 * added / base)))
    return {"status": status, "reasons": reasons}


def to_dict(ctx, row, detail=False):
    items = _items(ctx, row["id"])
    counted = [i for i in items if _counted(i)]
    scope_hours = sum(i["hours"] for i in counted)
    done = [i for i in counted if _is_done(i)]
    open_ = [i for i in counted if not _is_closed(i)]
    added = [i for i in counted if i["added_after_start"]]
    start, end = parse_date(row["start_date"]), parse_date(row["end_date"])
    out = {
        "id": row["id"], "project": row["project_key"], "name": row["name"], "goal": row["goal"], "status": row["status"],
        "start_date": row["start_date"], "end_date": row["end_date"], "working_days": working_days(start, end),
        "days_left": max((end - ctx.today).days, 0) if row["status"] == "active" else None,
        "committed": None if row["committed_hours"] is None else {"hours": _round(row["committed_hours"]), "tasks": row["committed_count"]},
        "scope": {
            "hours": _round(scope_hours), "tasks": len(counted), "added_hours": _round(sum(i["hours"] for i in added)),
            "added_tasks": len(added), "removed_tasks": sum(1 for i in items if i["outcome"] == "removed"),
        },
        "progress": {
            "done_hours": _round(sum(i["hours"] for i in done)), "done_tasks": len(done),
            "remaining_hours": _round(sum(i["hours"] for i in open_)), "remaining_tasks": len(open_),
            "percent": round(100 * sum(i["hours"] for i in done) / scope_hours) if scope_hours else (100 if counted and not open_ else 0),
        },
        "unestimated_tasks": sum(1 for i in counted if i["t_estimate"] is None and i["outcome"] != "dropped"),
        "result": None, "health": None,
        "created_at": row["created_at"], "started_at": row["started_at"], "closed_at": row["closed_at"],
    }  # fmt: skip
    if row["status"] == "closed":
        out["result"] = {
            "completed_hours": _round(row["completed_hours"]), "completed_tasks": row["completed_count"],
            "carried_tasks": row["carried_count"], "dropped_tasks": row["dropped_count"],
            "completion_rate": _round(row["completed_hours"] / (row["committed_hours"] + sum(i["hours"] for i in added))) if (row["committed_hours"] or 0) + sum(i["hours"] for i in added) else None,
        }  # fmt: skip
    if row["status"] == "active":
        out["health"] = _health(ctx, row, items, _burn_days(ctx, row, items))
    if detail:
        out["items"] = [_item_dict(i) for i in items]
        out["plan"] = _plan(ctx, row, items) if row["status"] != "closed" else None
    return out


def _item_dict(i):
    return {
        "task": "%s-%d" % (i["pkey"], i["number"]), "title": i["title"], "status": i["t_status"], "priority": i["priority"],
        "assignee": users.ref(i["assignee_id"], i["username"], i["display_name"]), "hours": _round(i["hours"]),
        "estimated": i["t_estimate"] is not None, "added_at": i["added_at"], "added_after_start": bool(i["added_after_start"]),
        "outcome": i["outcome"], "left_at": i["left_at"],
    }  # fmt: skip


# ------------------------------------------------------------------------------ burndown


def _burn_days(ctx, row, items):
    """Day-by-day remaining work of a started sprint, from the history of what came, went and finished."""
    start, end = parse_date(row["start_date"]), parse_date(row["end_date"])
    last_real = ctx.today if row["status"] == "active" else _local_date(ctx, row["closed_at"])
    ideal_from = max(start, _local_date(ctx, row["started_at"]))  # a sprint started late has less time, not an impossible line
    wd_total = working_days(ideal_from, end)
    baseline = row["committed_hours"] or 0
    entries = []
    for i in items:
        added = start if not i["added_after_start"] else max(_local_date(ctx, i["added_at"]), start)
        left = _local_date(ctx, i["left_at"]) if i["outcome"] == "removed" else None
        closed = None
        if _is_closed(i) and _closed_stamp(i):
            closed = max(_local_date(ctx, _closed_stamp(i)), start)
        entries.append((i, added, left, closed))
    days, d = [], start
    while d <= max(end, start):
        if d < ideal_from:
            ideal = baseline
        elif wd_total:
            ideal = baseline * working_days(d + timedelta(days=1), end) / wd_total
        else:
            ideal = baseline if d < end else 0
        day = {"date": d.isoformat(), "working_day": d.weekday() < 5, "ideal": _round(ideal), "remaining": None, "scope": None, "completed": None}
        if d <= last_real:
            scope = remaining = completed = 0.0
            for i, added, left, closed in entries:
                if added > d or (left is not None and left <= d):
                    continue
                scope += i["hours"]
                if closed is not None and closed <= d:
                    if _is_done(i):
                        completed += i["hours"]
                else:
                    remaining += i["hours"]
            day.update(remaining=_round(remaining), scope=_round(scope), completed=_round(completed))
        days.append(day)
        d += timedelta(days=1)
    return days


def burndown(ctx, sprint_id):
    row = _get_row(ctx, sprint_id)
    if row["status"] == "planned":
        raise Conflict("The sprint has not started, so there is nothing to burn down yet", code="sprint_not_started")
    items = _items(ctx, row["id"])
    days = _burn_days(ctx, row, items)
    return {
        "sprint": row["id"], "name": row["name"], "status": row["status"], "from": row["start_date"], "to": row["end_date"],
        "baseline_hours": _round(row["committed_hours"] or 0), "unestimated_tasks": sum(1 for i in items if _counted(i) and i["t_estimate"] is None),
        "days": days,
    }  # fmt: skip


# ------------------------------------------------------------------------------ reading


def list_sprints(ctx, project_key, params):
    project = projects.get_row(ctx.conn, project_key)
    wanted = [s.strip() for s in (params.get("status") or "").split(",") if s.strip()]
    if any(s not in STATUSES for s in wanted):
        raise ValidationError({"status": "must be a comma-separated list of: " + ", ".join(STATUSES)})
    rows = ctx.conn.execute(
        "SELECT s.*, ? AS project_key, ? AS project_status FROM sprints s WHERE s.project_id = ? ORDER BY s.start_date DESC, s.id DESC",
        (project["key"], project["status"], project["id"]),
    ).fetchall()
    return [to_dict(ctx, r) for r in rows if not wanted or r["status"] in wanted]


def get_sprint(ctx, sprint_id):
    return to_dict(ctx, _get_row(ctx, sprint_id), detail=True)


def backlog(ctx, project_key):
    """Open tasks (leaf tasks, so parents are not counted on top of their children) that no live sprint holds."""
    project = projects.get_row(ctx.conn, project_key)
    rows = ctx.conn.execute(
        "SELECT t.*, u.username, u.display_name FROM tasks t LEFT JOIN users u ON u.id = t.assignee_id "
        "WHERE t.project_id = ? AND t.status IN %s AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id) "
        "AND NOT EXISTS (SELECT 1 FROM sprint_items i WHERE i.task_id = t.id AND i.outcome IS NULL) "
        "ORDER BY CASE t.priority WHEN 'critical' THEN 3 WHEN 'high' THEN 2 WHEN 'medium' THEN 1 ELSE 0 END DESC, t.number" % OPEN_SQL,
        (project["id"],),
    ).fetchall()
    items = [
        {
            "task": "%s-%d" % (project["key"], r["number"]), "title": r["title"], "status": r["status"], "priority": r["priority"],
            "assignee": users.ref(r["assignee_id"], r["username"], r["display_name"]), "estimate_hours": r["estimate_hours"],
        }
        for r in rows
    ]  # fmt: skip
    return {
        "project": project["key"], "items": items, "total": len(items),
        "hours": _round(sum(r["estimate_hours"] or 0 for r in rows)), "unestimated_tasks": sum(1 for r in rows if r["estimate_hours"] is None),
    }  # fmt: skip


# ------------------------------------------------------------------------------ writing


def _check_dates(v, first=None, last=None):
    start = parse_date(v.get("start_date", first))
    end = parse_date(v.get("end_date", last))
    if start is None or end is None:
        return start, end
    if end < start:
        raise ValidationError({"end_date": "must not be before start_date"})
    if (end - start).days + 1 > MAX_DAYS:
        raise ValidationError({"end_date": "a sprint can run at most %d days" % MAX_DAYS})
    return start, end


def _check_overlap(ctx, project_id, start, end, ignore_id=None):
    row = ctx.conn.execute(
        "SELECT name, start_date, end_date FROM sprints WHERE project_id = ? AND status <> 'closed' AND id IS NOT ? "
        "AND start_date <= ? AND end_date >= ? ORDER BY start_date LIMIT 1",
        (project_id, ignore_id, end.isoformat(), start.isoformat()),
    ).fetchone()
    if row:
        raise Conflict(
            "These dates overlap the sprint %r (%s to %s)" % (row["name"], row["start_date"], row["end_date"]),
            code="sprint_overlap", details={"sprint": row["name"]},
        )  # fmt: skip


def _name_taken(ctx, project_id, name, ignore_id=None):
    if ctx.conn.execute("SELECT 1 FROM sprints WHERE project_id = ? AND name = ? AND id IS NOT ?", (project_id, name, ignore_id)).fetchone():
        raise Conflict("The project already has a sprint named %r" % name, code="sprint_name_taken")


def create_sprint(ctx, project_key, data):
    project = projects.get_row(ctx.conn, project_key)
    projects.require_write(ctx, project)
    c = Checker(data, {"name", "goal", "start_date", "end_date"})
    c.text("name", required=True, min_len=1, max_len=60)
    c.text("goal", max_len=500)
    c.date("start_date", required=True, nullable=False)
    c.date("end_date", required=True, nullable=False)
    v = c.result()
    start, end = _check_dates(v)
    if ctx.conn.execute("SELECT COUNT(*) FROM sprints WHERE project_id = ?", (project["id"],)).fetchone()[0] >= MAX_SPRINTS_PER_PROJECT:
        raise Conflict("A project can have at most %d sprints" % MAX_SPRINTS_PER_PROJECT, code="sprint_limit")
    _name_taken(ctx, project["id"], v["name"])
    _check_overlap(ctx, project["id"], start, end)
    cur = ctx.conn.execute(
        "INSERT INTO sprints (project_id, name, goal, start_date, end_date, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
        (project["id"], v["name"], v.get("goal", ""), start.isoformat(), end.isoformat(), ctx.actor_id, ctx.now_iso),
    )
    activity.log(ctx, "sprint.created", project_id=project["id"], details={"id": cur.lastrowid, "name": v["name"], "start_date": start.isoformat(), "end_date": end.isoformat()})
    return get_sprint(ctx, cur.lastrowid)


def update_sprint(ctx, sprint_id, data):
    row = _get_row(ctx, sprint_id)
    project = _project_of(ctx, row)
    projects.require_write(ctx, project)
    if row["status"] == "closed":
        raise Conflict("A closed sprint cannot be changed", code="sprint_closed")
    allowed = {"name", "goal", "start_date", "end_date"} if row["status"] == "planned" else {"name", "goal", "end_date"}
    c = Checker(data, allowed, partial=True)
    c.text("name", min_len=1, max_len=60)
    c.text("goal", max_len=500)
    if "start_date" in allowed:
        c.date("start_date", nullable=False)
    c.date("end_date", nullable=False)
    v = c.result()
    start, end = _check_dates(v, row["start_date"], row["end_date"])
    if row["status"] == "active" and end < ctx.today:
        raise ValidationError({"end_date": "an active sprint cannot end before today"})
    if "name" in v:
        _name_taken(ctx, row["project_id"], v["name"], row["id"])
    if {"start_date", "end_date"} & v.keys():
        _check_overlap(ctx, row["project_id"], start, end, row["id"])
    changes = {}
    for field in ("name", "goal", "start_date", "end_date"):
        if field in v and v[field] != row[field]:
            changes[field] = [row[field], v[field]]
    if changes:
        ctx.conn.execute("UPDATE sprints SET %s WHERE id = ?" % ", ".join("%s = ?" % f for f in changes), [c_[1] for c_ in changes.values()] + [row["id"]])
        activity.log(ctx, "sprint.updated", project_id=row["project_id"], details={"id": row["id"], "name": row["name"], "changes": changes})
    return get_sprint(ctx, row["id"])


def delete_sprint(ctx, sprint_id):
    row = _get_row(ctx, sprint_id)
    projects.require_write(ctx, _project_of(ctx, row))
    if row["status"] != "planned":
        raise Conflict("Only a sprint that has not started can be deleted", code="sprint_not_deletable")
    released = ctx.conn.execute("SELECT COUNT(*) FROM sprint_items WHERE sprint_id = ? AND outcome IS NULL", (row["id"],)).fetchone()[0]
    ctx.conn.execute("DELETE FROM sprints WHERE id = ?", (row["id"],))
    activity.log(ctx, "sprint.deleted", project_id=row["project_id"], details={"id": row["id"], "name": row["name"], "released_tasks": released})


def _relatives(conn, task_id):
    """Ids of the task's ancestors and descendants."""
    rows = conn.execute(
        "WITH RECURSIVE up(id) AS (SELECT parent_id FROM tasks WHERE id = ?1 AND parent_id IS NOT NULL "
        "UNION SELECT t.parent_id FROM tasks t JOIN up ON t.id = up.id WHERE t.parent_id IS NOT NULL), "
        "down(id) AS (SELECT id FROM tasks WHERE parent_id = ?1 UNION SELECT t.id FROM tasks t JOIN down ON t.parent_id = down.id) "
        "SELECT id FROM up UNION SELECT id FROM down",
        (task_id,),
    ).fetchall()
    return [r[0] for r in rows]


def _insert_item(ctx, sprint, task, after_start):
    ctx.conn.execute(
        "INSERT INTO sprint_items (sprint_id, task_id, hours, added_at, added_after_start) VALUES (?,?,?,?,?)",
        (sprint["id"], task["id"], task["estimate_hours"] or 0, ctx.now_iso, int(after_start)),
    )


def _refuse_if_unfit(ctx, sprint, task):
    key = taskrefs.key_of(task)
    if task["project_id"] != sprint["project_id"]:
        raise Conflict("%s belongs to another project" % key, code="task_other_project", details={"task": key})
    if task["status"] in CLOSED_STATUSES:
        raise Conflict("%s is %s; only open tasks can be planned" % (key, task["status"]), code="task_closed", details={"task": key})
    live = ctx.conn.execute(
        "SELECT s.name, s.id FROM sprint_items i JOIN sprints s ON s.id = i.sprint_id WHERE i.task_id = ? AND i.outcome IS NULL", (task["id"],)
    ).fetchone()
    if live:
        raise Conflict("%s is already in the sprint %r" % (key, live["name"]), code="task_in_sprint", details={"task": key, "sprint": live["name"]})
    related = _relatives(ctx.conn, task["id"])
    if related:
        clash = ctx.conn.execute(
            "SELECT i.task_id FROM sprint_items i WHERE i.sprint_id = ? AND i.outcome IS NULL AND i.task_id IN (%s) LIMIT 1" % ",".join("?" * len(related)),
            [sprint["id"]] + related,
        ).fetchone()
        if clash:
            other = taskrefs.key_of(taskrefs.get_row_by_id(ctx.conn, clash[0]))
            raise Conflict(
                "%s is a parent or subtask of %s, which is already in this sprint; plan one of them, so the work is not counted twice" % (key, other),
                code="overlapping_scope", details={"task": key, "related": other},
            )  # fmt: skip


def add_tasks(ctx, sprint_id, data):
    row = _get_row(ctx, sprint_id)
    projects.require_write(ctx, _project_of(ctx, row))
    if row["status"] == "closed":
        raise Conflict("A closed sprint cannot take tasks", code="sprint_closed")
    c = Checker(data, {"tasks"})
    c.raw("tasks", required=True)
    v = c.result()
    keys = v["tasks"]
    if not isinstance(keys, list) or not keys or len(keys) > MAX_TASKS_PER_REQUEST or not all(isinstance(k, str) for k in keys):
        raise ValidationError({"tasks": "must be a list of 1-%d task keys such as [\"WEB-1\"]" % MAX_TASKS_PER_REQUEST})
    seen, rows = set(), []
    for k in keys:
        t = taskrefs.get_row(ctx.conn, k)
        if t["id"] not in seen:
            seen.add(t["id"])
            rows.append(t)
    after_start = row["status"] == "active"
    for t in rows:  # all or nothing: the first problem aborts the request (and with it the transaction)
        _refuse_if_unfit(ctx, row, t)
        _insert_item(ctx, row, t, after_start)
        activity.log(
            ctx, "sprint.task_added", project_id=row["project_id"], task_id=t["id"], task_key=taskrefs.key_of(t),
            details={"sprint": row["name"], "hours": t["estimate_hours"] or 0, "after_start": after_start},
        )  # fmt: skip
    return get_sprint(ctx, row["id"])


def remove_task(ctx, sprint_id, task_key):
    row = _get_row(ctx, sprint_id)
    projects.require_write(ctx, _project_of(ctx, row))
    if row["status"] == "closed":
        raise Conflict("A closed sprint cannot be changed", code="sprint_closed")
    task = taskrefs.get_row(ctx.conn, task_key)
    item = ctx.conn.execute("SELECT id FROM sprint_items WHERE sprint_id = ? AND task_id = ? AND outcome IS NULL", (row["id"], task["id"])).fetchone()
    if item is None:
        raise NotFound("%s is not in this sprint" % taskrefs.key_of(task), code="task_not_in_sprint")
    ctx.conn.execute("UPDATE sprint_items SET outcome = 'removed', left_at = ? WHERE id = ?", (ctx.now_iso, item["id"]))
    activity.log(ctx, "sprint.task_removed", project_id=row["project_id"], task_id=task["id"], task_key=taskrefs.key_of(task), details={"sprint": row["name"]})
    return get_sprint(ctx, row["id"])


def start_sprint(ctx, sprint_id):
    row = _get_row(ctx, sprint_id)
    projects.require_write(ctx, _project_of(ctx, row))
    if row["status"] != "planned":
        raise Conflict("The sprint is already %s" % row["status"], code="sprint_not_planned")
    other = ctx.conn.execute("SELECT name FROM sprints WHERE project_id = ? AND status = 'active'", (row["project_id"],)).fetchone()
    if other:
        raise Conflict("The sprint %r is still active; close it first" % other["name"], code="sprint_already_active")
    start, end = parse_date(row["start_date"]), parse_date(row["end_date"])
    if ctx.today < start:
        raise Conflict("The sprint is planned to start on %s; change its dates to start it earlier" % row["start_date"], code="sprint_not_due")
    if ctx.today > end:
        raise Conflict("The sprint was planned to end on %s; change its dates first" % row["end_date"], code="sprint_expired")
    items = _items(ctx, row["id"])
    live = [i for i in items if i["outcome"] is None]
    if not live:
        raise Conflict("Add at least one task before starting the sprint", code="empty_sprint")
    closed = [i for i in live if i["t_status"] in CLOSED_STATUSES]
    if closed:  # a task finished while the sprint was still being planned does not belong in the commitment
        for i in closed:
            ctx.conn.execute("UPDATE sprint_items SET outcome = 'removed', left_at = ? WHERE id = ?", (ctx.now_iso, i["id"]))
        closed_ids = {c_["id"] for c_ in closed}
        live = [i for i in live if i["id"] not in closed_ids]
        if not live:
            raise Conflict("Every planned task has already been closed", code="empty_sprint")
    hours = sum(i["hours"] for i in live)
    ctx.conn.execute(
        "UPDATE sprints SET status = 'active', started_at = ?, committed_hours = ?, committed_count = ? WHERE id = ?",
        (ctx.now_iso, hours, len(live), row["id"]),
    )
    activity.log(ctx, "sprint.started", project_id=row["project_id"], details={"id": row["id"], "name": row["name"], "committed_hours": _round(hours), "committed_tasks": len(live), "dropped_already_closed": len(closed)})
    return get_sprint(ctx, row["id"])


def close_sprint(ctx, sprint_id, data):
    row = _get_row(ctx, sprint_id)
    projects.require_write(ctx, _project_of(ctx, row))
    if row["status"] != "active":
        raise Conflict("Only an active sprint can be closed", code="sprint_not_active")
    c = Checker(data, {"carry_over"})
    c.raw("carry_over")
    v = c.result()
    choice = v.get("carry_over", "backlog")
    target = None
    if choice == "next":
        target = ctx.conn.execute(
            "SELECT * FROM sprints WHERE project_id = ? AND status = 'planned' ORDER BY start_date, id LIMIT 1", (row["project_id"],)
        ).fetchone()
        if target is None:
            raise Conflict("There is no planned sprint to carry the unfinished tasks into", code="no_next_sprint")
    elif isinstance(choice, int) and not isinstance(choice, bool):
        target = ctx.conn.execute("SELECT * FROM sprints WHERE id = ? AND project_id = ?", (choice, row["project_id"])).fetchone()
        if target is None:
            raise ValidationError({"carry_over": "no such sprint in this project"})
        if target["status"] != "planned":
            raise Conflict("Unfinished tasks can only be carried into a sprint that has not started", code="target_not_planned")
    elif choice != "backlog":
        raise ValidationError({"carry_over": "must be \"backlog\", \"next\" or the id of a planned sprint of this project"})
    items = [i for i in _items(ctx, row["id"]) if i["outcome"] is None]
    done = dropped = carried = 0
    done_hours = 0.0
    for i in items:
        if i["t_status"] == "done":
            outcome, done, done_hours = "done", done + 1, done_hours + i["hours"]
        elif i["t_status"] == "cancelled":
            outcome, dropped = "dropped", dropped + 1
        else:
            outcome, carried = "carried", carried + 1
        ctx.conn.execute(
            "UPDATE sprint_items SET outcome = ?, left_at = ?, done_at = ? WHERE id = ?",
            (outcome, ctx.now_iso, i["completed_at"] if outcome in ("done", "dropped") else None, i["id"]),
        )
    moved = 0
    if target is not None:
        for i in items:
            if i["t_status"] not in CLOSED_STATUSES:
                ctx.conn.execute(
                    "INSERT INTO sprint_items (sprint_id, task_id, hours, added_at, added_after_start) VALUES (?,?,?,?,0)",
                    (target["id"], i["task_id"], i["t_estimate"] or 0, ctx.now_iso),
                )
                moved += 1
    ctx.conn.execute(
        "UPDATE sprints SET status = 'closed', closed_at = ?, completed_hours = ?, completed_count = ?, carried_count = ?, dropped_count = ? WHERE id = ?",
        (ctx.now_iso, done_hours, done, carried, dropped, row["id"]),
    )
    activity.log(
        ctx, "sprint.closed", project_id=row["project_id"],
        details={
            "id": row["id"], "name": row["name"], "completed_tasks": done, "completed_hours": _round(done_hours), "carried_tasks": carried,
            "dropped_tasks": dropped, "carried_into": target["name"] if target is not None else None, "moved": moved,
        },
    )  # fmt: skip
    return get_sprint(ctx, row["id"])


# ------------------------------------------------------------------------------ velocity and forecast


def velocity(ctx, project_key, params):
    """How much a team finishes per sprint, and - from that - how long what is left will take."""
    project = projects.get_row(ctx.conn, project_key)
    raw = params.get("sprints", "6")
    try:
        n = int(raw)
    except ValueError:
        raise ValidationError({"sprints": "must be an integer"}) from None
    if not 1 <= n <= 20:
        raise ValidationError({"sprints": "must be between 1 and 20"})
    rows = ctx.conn.execute("SELECT * FROM sprints WHERE project_id = ? AND status = 'closed' ORDER BY end_date DESC, id DESC LIMIT ?", (project["id"], n)).fetchall()
    history = []
    for r in rows:
        added = ctx.conn.execute("SELECT COALESCE(SUM(hours), 0) FROM sprint_items WHERE sprint_id = ? AND added_after_start = 1 AND outcome <> 'removed'", (r["id"],)).fetchone()[0]
        total = (r["committed_hours"] or 0) + added
        history.append({
            "id": r["id"], "name": r["name"], "start_date": r["start_date"], "end_date": r["end_date"],
            "committed_hours": _round(r["committed_hours"] or 0), "added_hours": _round(added), "completed_hours": _round(r["completed_hours"] or 0),
            "completed_tasks": r["completed_count"], "carried_tasks": r["carried_count"], "dropped_tasks": r["dropped_count"],
            "completion_rate": _round((r["completed_hours"] or 0) / total) if total else None,
            "days": (parse_date(r["end_date"]) - parse_date(r["start_date"])).days + 1,
        })  # fmt: skip
    recent = [h["completed_hours"] for h in history[:VELOCITY_WINDOW]]
    average = sum(recent) / len(recent) if recent else None
    ahead = ctx.conn.execute(
        "SELECT COALESCE(SUM(i.hours), 0) FROM sprint_items i JOIN sprints s ON s.id = i.sprint_id JOIN tasks t ON t.id = i.task_id "
        "WHERE s.project_id = ? AND s.status <> 'closed' AND i.outcome IS NULL AND t.status NOT IN %s" % CLOSED_SQL,
        (project["id"],),
    ).fetchone()[0]
    b = backlog(ctx, project["key"])
    remaining = ahead + b["hours"]
    forecast = None
    if average:
        length = max(1, round(sum(h["days"] for h in history[:VELOCITY_WINDOW]) / len(recent)))
        anchor = ctx.conn.execute("SELECT MIN(start_date) FROM sprints WHERE project_id = ? AND status <> 'closed'", (project["id"],)).fetchone()[0]
        first_day = parse_date(anchor) if anchor else ctx.today

        def finish(velocity_hours):
            if not velocity_hours:
                return None
            count = math.ceil(remaining / velocity_hours) if remaining else 0
            return {"sprints": count, "finish_date": (first_day + timedelta(days=count * length - 1)).isoformat() if count else ctx.today.isoformat()}

        forecast = {
            "remaining_hours": _round(remaining), "in_sprints_hours": _round(ahead), "backlog_hours": _round(b["hours"]),
            "unestimated_tasks": b["unestimated_tasks"], "sprint_length_days": length, "velocity": _round(average),
            "expected": finish(average), "optimistic": finish(max(recent)), "pessimistic": finish(min(recent)),
            "basis": "the average of the last %d closed sprint(s), sprints run back to back from %s" % (len(recent), first_day.isoformat()),
        }  # fmt: skip
    return {
        "project": project["key"], "sprints": history, "average_velocity": None if average is None else _round(average),
        "window": VELOCITY_WINDOW, "forecast": forecast,
        "note": None if forecast else "Close at least one sprint with finished work to get a forecast",
    }  # fmt: skip
