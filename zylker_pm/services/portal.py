"""The client portal: what a client contact - or a staff member previewing the portal - sees of a client's projects.

The portal is built to show *less* than the rest of the application, and it does so by construction: nothing here goes through
the general project, task or report endpoints, and every query asks only for what a client may see. Who is working on a task, the
hours logged and estimated, budgets and other custom project fields, comments, descriptions, people's workload, the reasons the
projection gives for a delay (they name colleagues and other clients' work), the baselines and every other project simply are
not selected, so no later change to those endpoints can leak into the portal.

A project reaches the portal only when it is linked to the contact's client and shared at a level other than ``none``:

* ``summary``: the project's rating and the sentence behind it, its progress in numbers, the milestones and the manager's latest
  message to the client;
* ``full``: the summary plus the forecast (when the project will be done), what was completed, what is coming up, what needs
  attention and what changed in the last few days, task by task.

Anything else - an unknown key, another client's project, a project that is not shared, an archived one - is the same 404.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from ..domain import OPEN_STATUSES
from ..errors import Conflict, Forbidden, NotFound, ValidationError
from ..util import parse_date
from . import clients, milestones, projection, reports

DEFAULT_DAYS = 7
MAX_DAYS = 90
UPCOMING_DAYS = 14
MAX_ITEMS = 10  # rows in one list (the counts always cover everything)
MAX_LATE = 5
MAX_MILESTONES = 8

STATUS_WORDS = {
    "on_track": "is on track",
    "at_risk": "is at risk",
    "off_track": "is off track",
    "complete": "is complete",
    "on_hold": "is on hold",
    "not_started": "has no tasks yet",
}


def _scope(ctx, params):
    """The client whose portal is being looked at: a contact always sees its own, staff name the client to preview."""
    clients.require_schema(ctx)
    raw = params.get("client")
    if ctx.client is not None:
        if raw not in (None, "") and raw != str(ctx.client["id"]):
            raise Forbidden("Client accounts can only see their own portal", code="client_access_denied")
        return clients.get_row(ctx.conn, ctx.client["id"])
    if raw in (None, ""):
        raise ValidationError({"client": "required for staff: the id of the client whose portal to preview"})
    if not re.fullmatch(r"[0-9]{1,18}", raw):
        raise NotFound("Client not found", code="client_not_found")
    return clients.get_row(ctx.conn, int(raw))


def _days(params):
    raw = params.get("days")
    if raw in (None, ""):
        return DEFAULT_DAYS
    if not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= MAX_DAYS:
        raise ValidationError({"days": "must be a whole number from 1 to %d" % MAX_DAYS})
    return int(raw)


def _local(ts, tz):
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz).date()


def _whole(percent):
    return int(percent + 0.5)


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# -------------------------------------------------------------------------------------------------- the project list


def list_projects(ctx, params):
    client = _scope(ctx, params)
    conn = ctx.conn
    rows = conn.execute(
        clients.LINK_SELECT + "WHERE cp.client_id = ? AND cp.share <> 'none' AND p.status <> 'archived' ORDER BY p.name COLLATE NOCASE, p.key",
        (client["id"],),
    ).fetchall()
    items = []
    for r in rows:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (r["project_id"],)).fetchone()
        m = reports.project_metrics(ctx, project)
        upcoming = next((ms for ms in milestones.list_milestones(ctx, r["key"]) if ms["status"] != "achieved"), None)
        items.append({
            "key": r["key"], "name": r["name"], "status": r["status"], "owner": r["owner_name"],
            "start_date": r["start_date"], "due_date": r["due_date"], "share": r["share"],
            "rating": reports.health(project, m, ctx.today),
            "progress": {"percent": m["tasks_pct"], "done": m["done"], "total": m["leaf_tasks"]},
            "update": clients.update_dict(r, staff=False),
            "next_milestone": {k: upcoming[k] for k in ("name", "due_date", "status")} if upcoming else None,
        })  # fmt: skip
    return {"client": {"id": client["id"], "name": client["name"]}, "items": items}


# ------------------------------------------------------------------------------------------------ a project's status


def project_report(ctx, key, params):
    client = _scope(ctx, params)
    days = _days(params)
    link = ctx.conn.execute(
        clients.LINK_SELECT + "WHERE cp.client_id = ? AND p.key = ? AND cp.share <> 'none' AND p.status <> 'archived'",
        (client["id"], (key or "").upper()),
    ).fetchone()
    if link is None:
        raise NotFound("Project %s not found" % (key or "").upper(), code="project_not_found")
    project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?", (link["project_id"],)).fetchone()
    return build(ctx, client, project, link, days)


def _headline(project, rating, m, completed, days, forecast):
    parts = ["%s %s." % (project["name"], STATUS_WORDS[rating["status"]])]
    if m["leaf_tasks"]:
        window = "today" if days == 1 else "in the last %d days" % days
        parts.append("%d of %s %s done (%d%%); %s finished %s." % (
            m["done"], _plural(m["leaf_tasks"], "task"), "is" if m["done"] == 1 or m["leaf_tasks"] == 1 else "are", _whole(m["tasks_pct"]),
            "none" if not completed else completed, window))  # fmt: skip
    if forecast and forecast.get("state") == "projected":
        parts.append(forecast["headline"])
    return " ".join(parts)


def _forecast(ctx, project):
    """``(forecast, {milestone id: its projection})``; the forecast is ``unavailable`` when there is too much open work to project."""
    try:
        view = projection.project_view(ctx, projection.compute(ctx), project)
    except Conflict:
        return {"state": "unavailable", "headline": "A forecast is not available right now."}, {}
    late = [t for t in view["tasks"] if t["late_by_days"]]
    count = view["counts"]["late_tasks"]
    return {
        "state": view["state"], "headline": view["headline"], "finish": view["finish"], "due_date": view["due_date"],
        "late_by_days": view["late_by_days"], "verdict": view["verdict"],
        "late": {
            "count": count,
            "items": [{k: t[k] for k in ("key", "title", "due_date", "projected_finish", "late_by_days")} for t in late[:MAX_LATE]],
            "more": max(count - MAX_LATE, 0),
        },
    }, {ms["id"]: ms for ms in view["milestones"]}  # fmt: skip


def _list(rows, limit=MAX_ITEMS):
    return {"count": len(rows), "items": rows[:limit], "more": max(len(rows) - limit, 0)}


def _rescheduled(ctx, project, leaves, frm, today):
    """Open tasks whose due date was moved in the period (first old date to last new date); tasks created in it are new, not moved."""
    tz, conn = ctx.tz, ctx.conn
    open_now = {r["id"]: r for r in leaves if r["status"] in OPEN_STATUSES and not (_local(r["created_at"], tz) >= frm)}
    first, last = {}, {}
    floor = (frm - timedelta(days=1)).isoformat() + "T00:00:00Z"
    for row in conn.execute(
        "SELECT task_id, details, created_at FROM activity WHERE project_id = ? AND task_id IS NOT NULL "
        "AND action IN ('task.updated', 'task.status_changed') AND created_at >= ? ORDER BY id",
        (project["id"], floor),
    ):
        if row["task_id"] not in open_now or not frm <= _local(row["created_at"], tz) <= today:
            continue
        try:
            old, new = json.loads(row["details"])["changes"]["due_date"]
        except (ValueError, KeyError, TypeError):
            continue  # not a change of the due date
        first.setdefault(row["task_id"], old)
        last[row["task_id"]] = new
    moved = []
    for tid, old in first.items():
        a, b = parse_date(old), parse_date(last[tid])
        if a and b and a != b:
            r = open_now[tid]
            moved.append({"key": "%s-%d" % (project["key"], r["number"]), "number": r["number"], "title": r["title"], "from": old, "to": last[tid], "days": (b - a).days})
    moved.sort(key=lambda x: (-x["days"], x["number"]))
    for x in moved:
        del x["number"]
    return {
        "later": sum(1 for x in moved if x["days"] > 0), "earlier": sum(1 for x in moved if x["days"] < 0),
        "count": len(moved), "items": moved[:MAX_ITEMS], "more": max(len(moved) - MAX_ITEMS, 0),
    }  # fmt: skip


def build(ctx, client, project, link, days):
    conn, tz, today = ctx.conn, ctx.tz, ctx.today
    level = link["share"]
    frm = today - timedelta(days=days - 1)
    key = project["key"]
    m = reports.project_metrics(ctx, project)
    rating = reports.health(project, m, today)
    leaves = conn.execute(reports.LEAF_SQL, (today.isoformat(), project["id"])).fetchall()

    def inside(ts):
        day = _local(ts, tz)
        return day is not None and frm <= day <= today

    def ref(r):
        return "%s-%d" % (key, r["number"])

    completed = sorted((r for r in leaves if r["status"] == "done" and inside(r["completed_at"])), key=lambda r: (_local(r["completed_at"], tz), r["number"]), reverse=True)
    added = [r for r in leaves if r["status"] != "cancelled" and inside(r["created_at"])]
    all_milestones = milestones.list_milestones(ctx, key)
    forecast, projected_milestones = _forecast(ctx, project) if level == "full" else (None, {})

    shown = [x for x in all_milestones if x["status"] != "achieved"]
    milestone_items = []
    for x in shown[:MAX_MILESTONES]:
        item = {k: x[k] for k in ("name", "due_date", "status", "tasks_done", "tasks_total", "progress")}
        if level == "full":
            p = projected_milestones.get(x["id"])
            item["projected_finish"] = p["projected_finish"] if p else None
            item["late_by_days"] = p["late_by_days"] if p else None
        milestone_items.append(item)

    report = {
        "client": {"id": client["id"], "name": client["name"]},
        "project": {"key": key, "name": project["name"], "status": project["status"], "owner": link["owner_name"], "start_date": project["start_date"], "due_date": project["due_date"]},
        "level": level,
        "as_of": today.isoformat(),
        "period": {"from": frm.isoformat(), "to": today.isoformat(), "days": days},
        "rating": rating,
        "headline": _headline(project, rating, m, len(completed), days, forecast),
        "update": clients.update_dict(link, staff=False),
        "progress": {
            "tasks": {"total": m["leaf_tasks"], "done": m["done"], "open": m["open"], "percent": m["tasks_pct"]},
            "period": {"completed": len(completed), "added": len(added)},
        },
        "milestones": {"items": milestone_items, "more": max(len(shown) - MAX_MILESTONES, 0), "achieved": len(all_milestones) - len(shown)},
    }  # fmt: skip
    if level != "full":
        return report

    open_leaves = [r for r in leaves if r["status"] in OPEN_STATUSES]
    soon = (today + timedelta(days=UPCOMING_DAYS - 1)).isoformat()
    upcoming = sorted((r for r in open_leaves if r["due_date"] and today.isoformat() <= r["due_date"] <= soon), key=lambda r: (r["due_date"], r["number"]))
    overdue = sorted((r for r in open_leaves if r["due_date"] and r["due_date"] < today.isoformat()), key=lambda r: (r["due_date"], r["number"]))
    cancelled = sorted((r for r in leaves if r["status"] == "cancelled" and inside(r["completed_at"])), key=lambda r: r["number"])
    report.update({
        "forecast": forecast,
        "completed": _list([{"key": ref(r), "title": r["title"], "completed_on": _local(r["completed_at"], tz).isoformat()} for r in completed]),
        "upcoming": {"days": UPCOMING_DAYS, **_list([{"key": ref(r), "title": r["title"], "due_date": r["due_date"], "status": r["status"]} for r in upcoming])},
        "attention": {
            "overdue": _list([{"key": ref(r), "title": r["title"], "due_date": r["due_date"], "days_overdue": (today - parse_date(r["due_date"])).days} for r in overdue]),
            "blocked": _list([{"key": ref(r), "title": r["title"]} for r in sorted((r for r in open_leaves if r["status"] == "blocked"), key=lambda r: r["number"])]),
        },
        "changes": {
            "added": _list([{"key": ref(r), "title": r["title"], "created_on": _local(r["created_at"], tz).isoformat()} for r in sorted(added, key=lambda r: r["number"])]),
            "cancelled": _list([{"key": ref(r), "title": r["title"], "cancelled_on": _local(r["completed_at"], tz).isoformat()} for r in cancelled]),
            "rescheduled": _rescheduled(ctx, project, leaves, frm, today),
        },
    })  # fmt: skip
    return report
