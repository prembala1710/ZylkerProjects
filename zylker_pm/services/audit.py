"""The admin log monitor: query, summarise and export the audit trail, and record security events.

The audit trail is the ``activity`` table. Every change to data is written there by the service that makes it
(who, what, when, old and new values). On top of that this module records the events that are *not* data changes
but matter to an administrator: failed sign-ins, permission denials, server errors, the start of a session and
exports of data. Routine reads are deliberately not recorded - they would bury the events that matter.

Nothing here ever stores a token, a request body or a password.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import re
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from ..errors import ValidationError
from ..util import escape_like, parse_date, truncate
from . import users

# An action is "<category>.<what>". The category is everything before the first dot.
CATEGORIES = {
    "task": "Tasks",
    "comment": "Comments",
    "time": "Time tracking",
    "milestone": "Milestones",
    "recurrence": "Recurring tasks",
    "project": "Projects",
    "project_field": "Project fields",
    "user": "Users and access",
    "import": "Imports",
    "auth": "Sign-ins and denials",
    "data": "Data exports",
    "system": "System",
    "audit": "Audit log",
    "webhook": "Webhooks",
}
SEVERITIES = ("info", "notice", "warning", "error")

# Everything not listed here is "info". "notice" marks events an administrator will want to be able to find fast.
ACTION_SEVERITY = {
    "auth.failed": "warning",
    "auth.denied": "warning",
    "auth.throttled": "warning",
    "auth.unblocked": "notice",
    "system.error": "error",
    "user.created": "notice",
    "user.updated": "notice",
    "user.token_rotated": "notice",
    "project.deleted": "notice",
    "recurrence.deleted": "notice",
    "recurrence.skipped": "notice",
    "project_field.deleted": "notice",
    "import.completed": "notice",
    "data.exported": "notice",
    "audit.exported": "notice",
    "webhook.disabled": "warning",
    "webhook.deleted": "notice",
    "webhook.secret_rotated": "notice",
}

MERGE_WINDOW_SECONDS = 300  # repeats of the same security event inside this window become one counted row
MAX_PAGE = 200
EXPORT_LIMIT = 50_000
COUNT_CAP = 10_000  # the filtered total is exact up to this, then reported as "10,000+"

ACTION_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{6,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


def category_of(action: str) -> str:
    return action.split(".", 1)[0]


def severity_of(action: str) -> str:
    return ACTION_SEVERITY.get(action, "info")


def clean(text, limit=200):
    """Strip control characters (they could forge log lines) and bound the length of anything client-supplied."""
    if text is None:
        return None
    return truncate(_CONTROL.sub(" ", str(text)).strip(), limit)


# ------------------------------------------------------------------------------ recording


def record(conn, now_iso, action, *, actor_id=None, ip=None, request_id=None, details=None, merge_on=()):
    """Write one event outside the normal service flow.

    With ``merge_on`` (names of keys inside ``details``), a repeat of the same event from the same actor and
    address within five minutes does not add a row: it raises ``count`` on the existing one. That bounds the
    log even when something hammers an endpoint, while the count still tells the administrator how often.
    """
    details = dict(details or {})
    if merge_on:
        cutoff = _iso(datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - timedelta(seconds=MERGE_WINDOW_SECONDS))
        candidates = conn.execute(
            "SELECT id, details FROM activity WHERE action = ? AND actor_id IS ? AND ip IS ? AND created_at >= ? ORDER BY id DESC LIMIT 25",
            (action, actor_id, ip, cutoff),
        ).fetchall()
        for row in candidates:
            old = json.loads(row["details"] or "{}")
            if all(old.get(k) == details.get(k) for k in merge_on):
                old["count"] = int(old.get("count", 1)) + 1
                old["last_seen"] = now_iso
                conn.execute("UPDATE activity SET details = ? WHERE id = ?", (json.dumps(old), row["id"]))
                return row["id"]
    if merge_on:
        details.setdefault("count", 1)  # only events that can repeat carry a count
    cur = conn.execute(
        "INSERT INTO activity (actor_id, action, details, created_at, ip, request_id) VALUES (?,?,?,?,?,?)",
        (actor_id, action, json.dumps(details), now_iso, ip, request_id),
    )
    from . import webhooks  # local import: webhooks uses this module to record its own events

    webhooks.enqueue(conn, now_iso, event_id=cur.lastrowid, action=action, actor_id=actor_id, details=details)
    return cur.lastrowid


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------------------ querying

_FROM = (
    "FROM activity a LEFT JOIN users u ON u.id = a.actor_id LEFT JOIN projects p ON p.id = a.project_id "
)
_PROJECT_KEY = "COALESCE(p.key, a.project_key)"


def _csv_param(params, name):
    return [x.strip() for x in (params.get(name) or "").split(",") if x.strip()]


def _day_bounds(ctx, text, name, end=False):
    """A YYYY-MM-DD date in the server's timezone as a UTC instant (start of that day, or of the next for ``end``)."""
    day = parse_date(text)
    if day is None:
        raise ValidationError({name: "must be a valid date in YYYY-MM-DD format"})
    if end:
        day += timedelta(days=1)
    return _iso(datetime.combine(day, time.min, tzinfo=ctx.tz).astimezone(timezone.utc))


def _filters(ctx, params):
    where, args = [], []

    q = (params.get("q") or "").strip()
    if q:
        like = "%" + escape_like(q) + "%"
        cols = ["a.action", "a.task_key", _PROJECT_KEY, "u.username", "u.display_name", "a.details", "a.ip", "a.request_id"]
        where.append("(" + " OR ".join("%s LIKE ? ESCAPE '\\'" % c for c in cols) + ")")
        args += [like] * len(cols)

    actor = (params.get("actor") or "").strip()
    if actor:
        if actor.lower() == "system":
            where.append("a.actor_id IS NULL")
        else:
            row = users.find_by_username(ctx.conn, actor)
            where.append("a.actor_id = ?")
            args.append(row["id"] if row else -1)

    categories = _csv_param(params, "category")
    bad = [c for c in categories if c not in CATEGORIES]
    if bad:
        raise ValidationError({"category": "unknown category %s; allowed: %s" % (", ".join(bad), ", ".join(CATEGORIES))})
    if categories:
        clauses = []
        for c in categories:
            clauses.append("substr(a.action, 1, ?) = ?")
            args += [len(c) + 1, c + "."]
        where.append("(" + " OR ".join(clauses) + ")")

    actions = _csv_param(params, "action")
    if any(not ACTION_RE.match(a) for a in actions):
        raise ValidationError({"action": "must look like 'task.created'"})
    if actions:
        where.append("a.action IN (%s)" % ",".join("?" * len(actions)))
        args += actions

    severities = _csv_param(params, "severity")
    bad = [s for s in severities if s not in SEVERITIES]
    if bad:
        raise ValidationError({"severity": "unknown severity %s; allowed: %s" % (", ".join(bad), ", ".join(SEVERITIES))})
    if severities:
        explicit = sorted(ACTION_SEVERITY)
        clauses = []
        for s in severities:
            if s == "info":
                clauses.append("a.action NOT IN (%s)" % ",".join("?" * len(explicit)))
                args += explicit
            else:
                names = sorted(a for a, v in ACTION_SEVERITY.items() if v == s)
                clauses.append("a.action IN (%s)" % ",".join("?" * len(names)))
                args += names
        where.append("(" + " OR ".join(clauses) + ")")

    project = (params.get("project") or "").strip()
    if project:
        where.append(_PROJECT_KEY + " = ?")
        args.append(project.upper())

    ip = (params.get("ip") or "").strip()
    if ip:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            raise ValidationError({"ip": "must be an IPv4 or IPv6 address"}) from None
        where.append("a.ip = ?")
        args.append(ip)

    request_id = (params.get("request_id") or "").strip()
    if request_id:
        if not REQUEST_ID_RE.match(request_id):
            raise ValidationError({"request_id": "is not a valid request id"})
        where.append("a.request_id = ?")
        args.append(request_id)

    if params.get("from"):
        where.append("a.created_at >= ?")
        args.append(_day_bounds(ctx, params["from"], "from"))
    if params.get("to"):
        where.append("a.created_at < ?")
        args.append(_day_bounds(ctx, params["to"], "to", end=True))
    return where, args


def _int(params, name, minimum=None, maximum=None):
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: "must be an integer"}) from None
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValidationError({name: "must be between %s and %s" % (minimum, maximum)})
    return value


def _item(r):
    action = r["action"]
    return {
        "id": r["id"],
        "action": action,
        "category": category_of(action),
        "severity": severity_of(action),
        "actor": {"id": r["actor_id"], "username": r["username"], "display_name": r["display_name"]} if r["actor_id"] else None,
        "project": r["project_key"],
        "task": r["task_key"],
        "details": json.loads(r["details"] or "{}"),
        "ip": r["ip"],
        "request_id": r["request_id"],
        "created_at": r["created_at"],
    }


_SELECT = "SELECT a.*, u.username, u.display_name, %s AS project_key " % _PROJECT_KEY


def query(ctx, params):
    """A page of events, newest first. ``before=<id>`` pages back, ``after=<id>`` fetches what is newer (live tail)."""
    limit = _int(params, "limit", 1, MAX_PAGE) or 50
    before, after = _int(params, "before", 0), _int(params, "after", 0)
    where, args = _filters(ctx, params)
    page_where, page_args = list(where), list(args)
    if before is not None:
        page_where.append("a.id < ?")
        page_args.append(before)
    if after is not None:
        page_where.append("a.id > ?")
        page_args.append(after)
    clause = ("WHERE " + " AND ".join(page_where) + " ") if page_where else ""
    rows = ctx.conn.execute(_SELECT + _FROM + clause + "ORDER BY a.id DESC LIMIT ?", page_args + [limit + 1]).fetchall()
    more = len(rows) > limit
    items = [_item(r) for r in rows[:limit]]
    out = {
        "items": items,
        "next_before": items[-1]["id"] if more and items else None,
        # With ``after``, "more" means newer events were left behind: the client missed some and should reload.
        "gap": more if after is not None else False,
        "newest_id": ctx.conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0],
    }
    if before is None and after is None:
        clause_all = ("WHERE " + " AND ".join(where) + " ") if where else ""
        counted = ctx.conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 " + _FROM + clause_all + "LIMIT ?)", args + [COUNT_CAP + 1]
        ).fetchone()[0]
        out["total"] = min(counted, COUNT_CAP)
        out["total_capped"] = counted > COUNT_CAP
    return out


# ------------------------------------------------------------------------------ summary


def summary(ctx, params):
    days = _int(params, "days", 1, 90) or 7
    today = ctx.today
    first_day = today - timedelta(days=days - 1)
    since = _iso(datetime.combine(first_day, time.min, tzinfo=ctx.tz).astimezone(timezone.utc))
    conn = ctx.conn

    # Bucket by minute in SQL and by local calendar day in Python, so a timezone with a half-hour offset still
    # puts every event on the right day.
    per_day = defaultdict(int)
    for r in conn.execute("SELECT substr(created_at, 1, 16) AS m, COUNT(*) AS n FROM activity WHERE created_at >= ? GROUP BY m", (since,)):
        stamp = datetime.strptime(r["m"], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).astimezone(ctx.tz)
        per_day[stamp.date()] += r["n"]
    by_day = [{"date": (first_day + timedelta(days=i)).isoformat(), "count": per_day[first_day + timedelta(days=i)]} for i in range(days)]

    by_action = {r["action"]: r["n"] for r in conn.execute("SELECT action, COUNT(*) AS n FROM activity WHERE created_at >= ? GROUP BY action", (since,))}
    by_category, by_severity = defaultdict(int), dict.fromkeys(SEVERITIES, 0)
    for action, n in by_action.items():
        by_category[category_of(action)] += n
        by_severity[severity_of(action)] += n

    actors = [
        {"actor": {"id": r["actor_id"], "username": r["username"], "display_name": r["display_name"]} if r["actor_id"] else None, "count": r["n"]}
        for r in conn.execute(
            "SELECT a.actor_id, u.username, u.display_name, COUNT(*) AS n FROM activity a LEFT JOIN users u ON u.id = a.actor_id "
            "WHERE a.created_at >= ? AND a.action NOT LIKE 'auth.%' GROUP BY a.actor_id ORDER BY n DESC, a.actor_id LIMIT 10",
            (since,),
        )
    ]

    failed_attempts, by_ip = 0, defaultdict(int)
    for r in conn.execute("SELECT ip, details FROM activity WHERE action = 'auth.failed' AND created_at >= ?", (since,)):
        n = int(json.loads(r["details"] or "{}").get("count", 1))
        failed_attempts += n
        by_ip[r["ip"] or "unknown"] += n
    denials = sum(
        int(json.loads(r["details"] or "{}").get("count", 1))
        for r in conn.execute("SELECT details FROM activity WHERE action = 'auth.denied' AND created_at >= ?", (since,))
    )
    throttled = sum(
        int(json.loads(r["details"] or "{}").get("count", 1))
        for r in conn.execute("SELECT details FROM activity WHERE action = 'auth.throttled' AND created_at >= ?", (since,))
    )
    return {
        "days": days,
        "from": first_day.isoformat(),
        "to": today.isoformat(),
        "total": sum(by_action.values()),
        "by_day": by_day,
        "by_category": [
            {"category": c, "label": CATEGORIES.get(c, c), "count": n} for c, n in sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "by_severity": by_severity,
        "top_actors": actors,
        "security": {
            "failed_sign_ins": failed_attempts,
            "permission_denials": denials,
            "throttled": throttled,
            "errors": by_severity["error"],
            "top_failing_addresses": [{"ip": ip, "attempts": n} for ip, n in sorted(by_ip.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
        },
        "newest_id": conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0],
    }


# ------------------------------------------------------------------------------ export

EXPORT_COLUMNS = ["id", "time_utc", "severity", "category", "action", "actor", "project", "task", "ip", "request_id", "details"]


def export_csv(ctx, params):
    """Returns ``(csv_text, row_count, truncated)`` for the events matching the filters, newest first."""
    from .csvio import _safe  # the spreadsheet formula neutraliser used for task exports

    where, args = _filters(ctx, params)
    clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
    rows = ctx.conn.execute(_SELECT + _FROM + clause + "ORDER BY a.id DESC LIMIT ?", args + [EXPORT_LIMIT + 1]).fetchall()
    truncated = len(rows) > EXPORT_LIMIT
    rows = rows[:EXPORT_LIMIT]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for r in rows:
        item = _item(r)
        writer.writerow([
            item["id"], item["created_at"], item["severity"], item["category"], item["action"],
            _safe(item["actor"]["username"] if item["actor"] else "system"), _safe(item["project"]), _safe(item["task"]),
            _safe(item["ip"]), _safe(item["request_id"]), _safe(json.dumps(item["details"], ensure_ascii=False, sort_keys=True)),
        ])  # fmt: skip
    return buf.getvalue(), len(rows), truncated
