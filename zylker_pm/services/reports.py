"""Reporting: project summary and health, portfolio, workload, burndown, critical path, digest, timesheet.

All numbers are derived from live data; nothing is cached. "Leaf" tasks (those without
subtasks) carry the work, so progress/overdue/hour metrics count leaves only and parents
are treated as containers. This avoids double counting.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from ..domain import CLOSED_STATUSES, OPEN_SQL, PRIORITIES, STATUSES, TASK_TYPES
from ..errors import Forbidden, ValidationError
from ..util import parse_date
from . import collab, milestones, projects, users

LEAF_SQL = (
    "SELECT t.id, t.number, t.title, t.status, t.priority, t.due_date, t.assignee_id, t.estimate_hours, "
    "t.created_at, t.completed_at, "
    "(SELECT COALESCE(SUM(te.hours), 0) FROM time_entries te WHERE te.task_id = t.id) AS logged, "
    "(SELECT COUNT(*) FROM task_dependencies d JOIN tasks b ON b.id = d.blocker_id "
    "WHERE d.blocked_id = t.id AND b.status IN " + OPEN_SQL + ") AS blocked_by_open, "
    "(SELECT COUNT(*) FROM task_dependencies d JOIN tasks b ON b.id = d.blocker_id "
    "WHERE d.blocked_id = t.id AND b.status IN " + OPEN_SQL + " AND b.due_date IS NOT NULL AND b.due_date < ?) "
    "AS blocked_by_overdue "
    "FROM tasks t WHERE t.project_id = ? AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id)"
)  # parameters: (today, project_id)

HEALTH_ORDER = {"off_track": 0, "at_risk": 1, "on_track": 2, "not_started": 3, "on_hold": 4, "complete": 5}


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


# ------------------------------------------------------------------ metrics & health


def project_metrics(ctx, project):
    today = ctx.today.isoformat()
    soon = (ctx.today + timedelta(days=7)).isoformat()
    m = {
        "leaf_tasks": 0, "done": 0, "open": 0, "cancelled": 0, "overdue": 0, "overdue_critical": 0,
        "due_soon": 0, "blocked": 0, "flagged_blocked": 0, "delayed_by_blockers": 0, "unassigned": 0,
        "unestimated_open": 0,
        "est_total": 0.0, "est_done": 0.0, "est_remaining": 0.0,
    }  # fmt: skip
    for r in ctx.conn.execute(LEAF_SQL, (today, project["id"])):
        status, est = r["status"], r["estimate_hours"]
        if status == "cancelled":
            m["cancelled"] += 1
            continue
        m["leaf_tasks"] += 1
        if est:
            m["est_total"] += est
        if status == "done":
            m["done"] += 1
            m["est_done"] += est or 0
            continue
        m["open"] += 1
        if est is None:
            m["unestimated_open"] += 1
        else:
            m["est_remaining"] += max(est - r["logged"], 0)
        due = r["due_date"]
        if due and due < today:
            m["overdue"] += 1
            if r["priority"] == "critical":
                m["overdue_critical"] += 1
        if due and today <= due <= soon:
            m["due_soon"] += 1
        if status == "blocked" or r["blocked_by_open"] > 0:
            m["blocked"] += 1  # informational: "cannot start yet" (flagged or waiting on a predecessor)
        if status == "blocked":
            m["flagged_blocked"] += 1
        if r["blocked_by_overdue"] > 0:
            m["delayed_by_blockers"] += 1
        if r["assignee_id"] is None:
            m["unassigned"] += 1
    m["tasks_pct"] = _pct(m["done"], m["leaf_tasks"])
    m["hours_pct"] = _pct(m["est_done"], m["est_total"]) if m["est_total"] else None
    return m


def health(project, m, today):
    """Rule-based RAG status; ``reasons`` explains every rule that fired."""
    if project["status"] == "on_hold":
        return {"status": "on_hold", "reasons": ["Project is on hold"]}
    if project["status"] == "completed" or (m["leaf_tasks"] > 0 and m["open"] == 0):
        return {"status": "complete", "reasons": []}
    if m["leaf_tasks"] == 0:
        return {"status": "not_started", "reasons": ["No tasks yet"]}
    off, risk = [], []
    open_n = m["open"]
    due, start = parse_date(project["due_date"]), parse_date(project["start_date"])
    if due and due < today:
        off.append("Project due date (%s) has passed with %d open task(s)" % (due, open_n))
    if m["overdue_critical"]:
        off.append("%d critical task(s) overdue" % m["overdue_critical"])
    if m["overdue"] and m["overdue"] / open_n >= 0.25:
        off.append("%d of %d open tasks are overdue" % (m["overdue"], open_n))
    elif m["overdue"]:
        risk.append("%d task(s) overdue" % m["overdue"])
    # Waiting on a predecessor is normal sequencing, so it is NOT a risk by itself. Risk is a task
    # someone flagged as blocked, or a task stuck behind a predecessor that is already late.
    if m["flagged_blocked"] / open_n >= 0.25:
        risk.append("%d of %d open tasks are flagged as blocked" % (m["flagged_blocked"], open_n))
    if m["delayed_by_blockers"]:
        risk.append("%d task(s) are waiting on overdue predecessors" % m["delayed_by_blockers"])
    progress = m["tasks_pct"]
    if start and due and start < due and start <= today <= due:
        elapsed = 100.0 * (today - start).days / (due - start).days
        if progress < elapsed - 15:
            risk.append("Behind schedule: %d%% complete vs %d%% of time elapsed" % (progress, elapsed))
    if due and today <= due and (due - today).days <= 14 and progress < 70:
        risk.append("Due in %d day(s) but only %d%% complete" % ((due - today).days, progress))
    level = "off_track" if off else "at_risk" if risk else "on_track"
    return {"status": level, "reasons": off + risk}


def _next_milestone(ctx, project_key):
    for ms in milestones.list_milestones(ctx, project_key):
        if ms["status"] != "achieved":
            return ms
    return None


def _group_counts(conn, project_id, column, allowed):
    out = dict.fromkeys(allowed, 0)
    for r in conn.execute("SELECT %s AS k, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY %s" % (column, column), (project_id,)):
        out[r["k"]] = r["n"]
    return out  # fmt: skip


def project_summary(ctx, key):
    project = projects.get_row(ctx.conn, key)
    m = project_metrics(ctx, project)
    by_status = _group_counts(ctx.conn, project["id"], "status", STATUSES)
    logged = ctx.conn.execute(
        "SELECT COALESCE(SUM(e.hours), 0) FROM time_entries e JOIN tasks t ON t.id = e.task_id WHERE t.project_id = ?",
        (project["id"],),
    ).fetchone()[0]
    return {
        "project": {
            "key": project["key"], "name": project["name"], "status": project["status"],
            "start_date": project["start_date"], "due_date": project["due_date"],
        },  # fmt: skip
        "counts": {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_priority": _group_counts(ctx.conn, project["id"], "priority", PRIORITIES),
            "by_type": _group_counts(ctx.conn, project["id"], "type", TASK_TYPES),
        },
        "metrics": {
            k: m[k]
            for k in (
                "leaf_tasks", "done", "open", "cancelled", "overdue", "due_soon", "blocked", "flagged_blocked",
                "delayed_by_blockers", "unassigned", "unestimated_open",
            )
        },  # fmt: skip
        "progress": {"tasks_pct": m["tasks_pct"], "hours_pct": m["hours_pct"]},
        "hours": {
            "estimated": round(m["est_total"], 2),
            "logged": round(logged, 2),
            "remaining_estimate": round(m["est_remaining"], 2),
        },
        "health": health(project, m, ctx.today),
        "next_milestone": _next_milestone(ctx, project["key"]),
    }


def portfolio(ctx):
    items, health_counts = [], defaultdict(int)
    open_tasks = overdue = 0
    rows = ctx.conn.execute(projects.PROJECT_SELECT + "WHERE p.status <> 'archived' ORDER BY p.key").fetchall()
    for row in rows:
        m = project_metrics(ctx, row)
        h = health(row, m, ctx.today)
        due = parse_date(row["due_date"])
        base = projects.to_dict(ctx, row)
        items.append(
            {
                **base,
                "progress": {"tasks_pct": m["tasks_pct"], "hours_pct": m["hours_pct"]},
                "open_tasks": m["open"],
                "overdue": m["overdue"],
                "blocked": m["blocked"],
                "due_soon": m["due_soon"],
                "health": h,
                "next_milestone": _next_milestone(ctx, row["key"]),
                "days_remaining": (due - ctx.today).days if due else None,
            }
        )
        health_counts[h["status"]] += 1
        open_tasks += m["open"]
        overdue += m["overdue"]
    items.sort(key=lambda i: (HEALTH_ORDER[i["health"]["status"]], i["due_date"] or "9999", i["key"]))
    return {
        "as_of": ctx.today.isoformat(),
        "totals": {"projects": len(items), "open_tasks": open_tasks, "overdue_tasks": overdue, "by_health": dict(health_counts)},
        "projects": items,
    }  # fmt: skip


# ----------------------------------------------------------------------------- workload


def workload(ctx, params):
    try:
        weeks = int(params.get("weeks", 2))
    except ValueError:
        raise ValidationError({"weeks": "must be an integer between 1 and 12"}) from None
    if not 1 <= weeks <= 12:
        raise ValidationError({"weeks": "must be an integer between 1 and 12"})
    today = ctx.today
    horizon = (today + timedelta(days=7 * weeks)).isoformat()
    args, extra = [], ""
    project_key = (params.get("project") or "").strip().upper()
    if project_key:
        projects.get_row(ctx.conn, project_key)  # 404 for unknown project
        extra = " AND p.key = ?"
        args.append(project_key)
    rows = ctx.conn.execute(
        "SELECT t.id, t.assignee_id, t.due_date, t.estimate_hours, "
        "(SELECT COALESCE(SUM(te.hours), 0) FROM time_entries te WHERE te.task_id = t.id) AS logged "
        "FROM tasks t JOIN projects p ON p.id = t.project_id "
        "WHERE t.status IN " + OPEN_SQL + " AND t.assignee_id IS NOT NULL AND p.status <> 'archived' "
        "AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id)" + extra,
        args,
    ).fetchall()
    per_user = defaultdict(lambda: {"open": 0, "overdue": 0, "scheduled": 0.0, "unscheduled": 0.0, "unestimated": 0})
    for r in rows:
        u = per_user[r["assignee_id"]]
        u["open"] += 1
        due, est = r["due_date"], r["estimate_hours"]
        if due and due < today.isoformat():
            u["overdue"] += 1
        if est is None:
            u["unestimated"] += 1
            continue
        remaining = max(est - r["logged"], 0)
        if due is None:
            u["unscheduled"] += remaining
        elif due <= horizon:
            u["scheduled"] += remaining

    member_sql = "SELECT DISTINCT pm.user_id FROM project_members pm JOIN projects p ON p.id = pm.project_id WHERE p.status <> 'archived'" + extra
    member_ids = {r[0] for r in ctx.conn.execute(member_sql, args)}
    items = []
    for user in users.list_users(ctx, active=True):
        if user["id"] not in per_user and user["id"] not in member_ids:
            continue
        u = per_user.get(user["id"], {"open": 0, "overdue": 0, "scheduled": 0.0, "unscheduled": 0.0, "unestimated": 0})
        capacity = user["weekly_capacity_hours"] * weeks
        items.append(
            {
                "user": users.ref(user["id"], user["username"], user["display_name"]),
                "role": user["role"],
                "weekly_capacity_hours": user["weekly_capacity_hours"],
                "capacity_hours": round(capacity, 2),
                "open_tasks": u["open"],
                "overdue": u["overdue"],
                "scheduled_hours": round(u["scheduled"], 2),
                "unscheduled_hours": round(u["unscheduled"], 2),
                "unestimated_tasks": u["unestimated"],
                "utilization_pct": _pct(u["scheduled"], capacity) if capacity else None,
                "overallocated": u["scheduled"] > capacity,
            }
        )
    items.sort(key=lambda i: (-(i["utilization_pct"] or 0), i["user"]["username"]))
    return {"as_of": today.isoformat(), "weeks": weeks, "window_end": horizon, "items": items}


# ----------------------------------------------------------------------------- burndown


def _local_date(ts, tz):
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz).date()


def parse_range(params, ctx, default_days=30):
    """Validate ``from``/``to`` (YYYY-MM-DD). Defaults to the trailing ``default_days`` days."""
    errors = {}
    to = parse_date(params["to"]) if params.get("to") else ctx.today
    if to is None:
        errors["to"] = "must be a valid date in YYYY-MM-DD format"
    frm = parse_date(params["from"]) if params.get("from") else (to - timedelta(days=default_days) if to else None)
    if frm is None:
        errors["from"] = "must be a valid date in YYYY-MM-DD format"
    if errors:
        raise ValidationError(errors)
    if frm > to:
        raise ValidationError({"from": "must not be after 'to'"})
    return frm, to


def burndown(ctx, key, params):
    project = projects.get_row(ctx.conn, key)
    metric = params.get("metric", "tasks")
    if metric not in ("tasks", "hours"):
        raise ValidationError({"metric": "must be 'tasks' or 'hours'"})
    tasks = ctx.conn.execute(LEAF_SQL, (ctx.today.isoformat(), project["id"])).fetchall()
    created = {t["id"]: _local_date(t["created_at"], ctx.tz) for t in tasks}

    start = parse_date(params["from"]) if params.get("from") else None
    if params.get("from") and start is None:
        raise ValidationError({"from": "must be a valid date in YYYY-MM-DD format"})
    start = start or parse_date(project["start_date"]) or min(created.values(), default=ctx.today - timedelta(days=13))
    end = parse_date(params["to"]) if params.get("to") else None
    if params.get("to") and end is None:
        raise ValidationError({"to": "must be a valid date in YYYY-MM-DD format"})
    due = parse_date(project["due_date"])
    end = end or (due if due and due >= start else max(ctx.today, start))
    if end < start:
        raise ValidationError({"from": "must not be after 'to'"})
    if (end - start).days > 366:
        raise ValidationError({"to": "range is limited to 367 days"})

    def weight(t):
        return 1 if metric == "tasks" else (t["estimate_hours"] or 0)

    closed = {t["id"]: _local_date(t["completed_at"], ctx.tz) for t in tasks if t["status"] in CLOSED_STATUSES}
    done = {t["id"]: closed[t["id"]] for t in tasks if t["status"] == "done"}
    days, d = [], start
    while d <= end:
        if d > ctx.today:
            days.append({"date": d.isoformat(), "remaining": None, "scope": None, "completed": None})
        else:
            scope = sum(weight(t) for t in tasks if created[t["id"]] <= d)
            gone = sum(weight(t) for t in tasks if created[t["id"]] <= d and closed.get(t["id"]) and closed[t["id"]] <= d)
            comp = sum(weight(t) for t in tasks if done.get(t["id"]) and done[t["id"]] <= d)
            days.append({"date": d.isoformat(), "remaining": round(scope - gone, 2), "scope": round(scope, 2), "completed": round(comp, 2)})
        d += timedelta(days=1)
    first = days[0]["remaining"] or 0
    n = len(days)
    for i, day in enumerate(days):
        day["ideal"] = round(first * (1 - i / (n - 1)), 2) if n > 1 else first
    return {
        "project": project["key"],
        "metric": metric,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "unestimated_tasks": sum(1 for t in tasks if t["estimate_hours"] is None and t["status"] != "cancelled"),
        "days": days,
    }  # fmt: skip


# -------------------------------------------------------------------------- critical path


def critical_path(ctx, key):
    """Classic CPM over open tasks that take part in a dependency.

    Duration = remaining estimate (estimate - logged, min 0); unestimated tasks use
    ``config.default_task_hours``; a parent's duration is the sum of its open leaf
    descendants. Hours are effort-hours, not calendar time.
    """
    conn = ctx.conn
    project = projects.get_row(conn, key)
    default = ctx.config.default_task_hours
    rows = conn.execute(
        "SELECT t.id, t.number, t.title, t.status, t.due_date, t.parent_id, t.estimate_hours, u.username, "
        "(SELECT COALESCE(SUM(te.hours), 0) FROM time_entries te WHERE te.task_id = t.id) AS logged "
        "FROM tasks t LEFT JOIN users u ON u.id = t.assignee_id WHERE t.project_id = ? AND t.status IN " + OPEN_SQL,
        (project["id"],),
    ).fetchall()
    task = {r["id"]: r for r in rows}
    edges = [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT d.blocker_id, d.blocked_id FROM task_dependencies d JOIN tasks t ON t.id = d.blocker_id WHERE t.project_id = ?",
            (project["id"],),
        )
        if r[0] in task and r[1] in task
    ]
    assumptions = {
        "default_task_hours": default,
        "unit": "effort-hours (not calendar time)",
        "notes": "Only open tasks linked by dependencies are analysed. Unestimated tasks use the default duration; "
        "a parent's duration is the sum of its open subtasks.",
    }
    if not edges:
        return {"project": project["key"], "total_hours": 0, "path": [], "tasks": [], "assumptions": assumptions}

    children = defaultdict(list)
    for r in rows:
        if r["parent_id"] in task:
            children[r["parent_id"]].append(r["id"])

    def leaf_hours(tid):
        r = task[tid]
        return max(r["estimate_hours"] - r["logged"], 0) if r["estimate_hours"] is not None else default

    def duration(tid):
        stack, total, seen = [tid], 0.0, set()
        if not children.get(tid):
            return leaf_hours(tid)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if children.get(cur):
                stack.extend(children[cur])
            else:
                total += leaf_hours(cur)
        return total

    nodes = sorted({n for e in edges for n in e})
    dur = {n: duration(n) for n in nodes}
    succ, pred = defaultdict(list), defaultdict(list)
    for u, v in edges:
        succ[u].append(v)
        pred[v].append(u)
    indeg = {n: len(pred[n]) for n in nodes}
    queue = deque(sorted(n for n in nodes if indeg[n] == 0))
    order = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for s in sorted(succ[n]):
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    es, ef = {}, {}
    for n in order:
        es[n] = max((ef[p] for p in pred[n]), default=0.0)
        ef[n] = es[n] + dur[n]
    finish = max(ef.values())
    lf, ls = {}, {}
    for n in reversed(order):
        lf[n] = min((ls[s] for s in succ[n]), default=finish)
        ls[n] = lf[n] - dur[n]
    eps = 1e-9
    critical = {n for n in nodes if abs(ls[n] - es[n]) <= eps}

    path = []
    starts = sorted((n for n in critical if es[n] <= eps), key=lambda n: task[n]["number"])
    current = starts[0] if starts else None
    while current is not None:
        path.append(current)
        nxt = sorted((s for s in succ[current] if s in critical and abs(es[s] - ef[current]) <= eps), key=lambda n: task[n]["number"])
        current = nxt[0] if nxt else None

    def k(n):
        return "%s-%d" % (project["key"], task[n]["number"])

    out = [
        {
            "key": k(n), "title": task[n]["title"], "status": task[n]["status"], "assignee": task[n]["username"],
            "due_date": task[n]["due_date"], "container": bool(children.get(n)),
            "duration_hours": round(dur[n], 2), "earliest_start": round(es[n], 2), "earliest_finish": round(ef[n], 2),
            "latest_start": round(ls[n], 2), "latest_finish": round(lf[n], 2),
            "slack_hours": round(max(ls[n] - es[n], 0), 2), "critical": n in critical,
        }
        for n in sorted(nodes, key=lambda n: (es[n], task[n]["number"]))
    ]  # fmt: skip
    return {"project": project["key"], "total_hours": round(finish, 2), "path": [k(n) for n in path], "tasks": out, "assumptions": assumptions}


# ------------------------------------------------------------------- digest & timesheet


def digest(ctx, params):
    try:
        days = int(params.get("days", 3))
    except ValueError:
        raise ValidationError({"days": "must be an integer between 0 and 30"}) from None
    if not 0 <= days <= 30:
        raise ValidationError({"days": "must be an integer between 0 and 30"})
    today = ctx.today
    horizon = (today + timedelta(days=days)).isoformat()
    args = [horizon]
    extra = ""
    if params.get("project"):
        extra = " AND p.key = ?"
        args.append(params["project"].upper())
    rows = ctx.conn.execute(
        "SELECT t.number, t.title, t.priority, t.due_date, p.key AS project_key, u.id AS uid, u.username, u.display_name "
        "FROM tasks t JOIN projects p ON p.id = t.project_id LEFT JOIN users u ON u.id = t.assignee_id "
        "WHERE t.status IN " + OPEN_SQL + " AND t.due_date IS NOT NULL AND t.due_date <= ? "
        "AND p.status NOT IN ('archived','on_hold','completed')" + extra + " ORDER BY t.due_date, t.number",
        args,
    ).fetchall()
    groups, overdue_total, soon_total = {}, 0, 0
    for r in rows:
        due = parse_date(r["due_date"])
        is_overdue = due < today
        item = {
            "key": "%s-%d" % (r["project_key"], r["number"]), "title": r["title"], "priority": r["priority"],
            "project": r["project_key"], "due_date": r["due_date"],
            "days_overdue" if is_overdue else "days_until_due": abs((today - due).days),
        }  # fmt: skip
        g = groups.setdefault(r["uid"], {"assignee": users.ref(r["uid"], r["username"], r["display_name"]), "overdue": [], "due_soon": []})
        g["overdue" if is_overdue else "due_soon"].append(item)
        overdue_total += is_overdue
        soon_total += not is_overdue
    ordered = sorted(groups.values(), key=lambda g: (g["assignee"] is None, -len(g["overdue"]), -len(g["due_soon"])))
    return {"as_of": today.isoformat(), "days": days, "overdue_count": overdue_total, "due_soon_count": soon_total, "groups": ordered}


def timesheet(ctx, params):
    frm, to = parse_range(params, ctx)
    where = ["e.work_date BETWEEN ? AND ?"]
    args = [frm.isoformat(), to.isoformat()]
    wanted = (params.get("user") or "").strip()
    if not ctx.has_role("manager"):
        if wanted and wanted not in ("me", ctx.user["username"]):
            raise Forbidden("You can only view your own timesheet", code="insufficient_role")
        wanted = "me"
    if wanted:
        user = ctx.user if wanted == "me" and ctx.user is not None else users.find_by_username(ctx.conn, wanted)
        where.append("e.user_id = ?")
        args.append(user["id"] if user else -1)
    if params.get("project"):
        where.append("p.key = ?")
        args.append(params["project"].upper())
    rows = ctx.conn.execute(
        "SELECT e.*, u.username, u.display_name, t.number, p.key AS project_key FROM time_entries e "
        "JOIN users u ON u.id = e.user_id JOIN tasks t ON t.id = e.task_id JOIN projects p ON p.id = t.project_id "
        "WHERE " + " AND ".join(where) + " ORDER BY e.work_date DESC, e.id DESC",
        args,
    ).fetchall()
    by_user, by_project, by_day = defaultdict(float), defaultdict(float), defaultdict(float)
    names = {}
    for r in rows:
        by_user[r["username"]] += r["hours"]
        by_project[r["project_key"]] += r["hours"]
        by_day[r["work_date"]] += r["hours"]
        names[r["username"]] = r["display_name"]
    return {
        "from": frm.isoformat(),
        "to": to.isoformat(),
        "total_hours": round(sum(by_user.values()), 2),
        "by_user": [{"username": u, "display_name": names[u], "hours": round(h, 2)} for u, h in sorted(by_user.items())],
        "by_project": [{"project": p, "hours": round(h, 2)} for p, h in sorted(by_project.items())],
        "by_day": [{"date": d, "hours": round(h, 2)} for d, h in sorted(by_day.items())],
        "entries": [collab.time_dict(r) for r in rows[:1000]],
        "truncated": len(rows) > 1000,
    }  # fmt: skip
