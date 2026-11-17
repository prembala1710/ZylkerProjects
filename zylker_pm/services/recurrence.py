"""Recurring tasks: a schedule plus a task template, and a generator that turns due dates into real tasks.

Design points that matter:

* **Idempotent.** Every date a rule produces is written once to ``recurrence_occurrences`` (primary key
  ``(recurrence_id, occurrence_date)``) in the same transaction as the task, so running the generator twice - or two
  generators at once - never creates the same task twice.
* **Never a flood.** A rule that was not run for a long time (server down, project archived) creates only the latest
  ``MAX_CATCH_UP`` missed tasks; the older dates are recorded as skipped. A new rule, a resumed rule and an edited
  schedule never backfill the past either.
* **One bad date does not stop the rest.** Each task is created inside a savepoint; a failure records a skipped
  occurrence with the reason and the run carries on.
* **Pure schedule maths.** ``dates()`` has no database access, so the rules (month ends, leap years, every-other-week)
  are tested directly.
"""

from __future__ import annotations

import calendar
import json
from datetime import date, timedelta

from ..domain import PRIORITIES, TASK_TYPES
from ..errors import ApiError, NotFound, ValidationError
from ..util import Checker, parse_date
from . import activity, projects, tasks, users

FREQUENCIES = ("daily", "weekly", "monthly")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_CATCH_UP = 5
MAX_RULES_PER_PROJECT = 50
HORIZON_DAYS = 366 * 30  # a rule is never evaluated further than this past its start
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2100, 12, 31)

SCHEDULE_FIELDS = {"frequency", "interval", "weekdays", "month_day", "start_date", "end_date", "max_occurrences"}
TEMPLATE_FIELDS = {"title", "description", "type", "priority", "assignee", "estimate_hours", "labels", "due_offset_days"}
ALL_FIELDS = SCHEDULE_FIELDS | TEMPLATE_FIELDS | {"active"}


# ------------------------------------------------------------------------------ schedule maths (pure)


def _raw_dates(frequency, interval, weekdays, month_day, start):
    if frequency == "daily":
        d = start
        while True:
            yield d
            d += timedelta(days=interval)
    elif frequency == "weekly":
        week = start - timedelta(days=start.weekday())  # weeks are counted from the Monday of the start week
        days = sorted(weekdays)
        while True:
            for wd in days:
                d = week + timedelta(days=wd)
                if d >= start:
                    yield d
            week += timedelta(weeks=interval)
    else:
        year, month = start.year, start.month
        while True:
            last = calendar.monthrange(year, month)[1]
            d = date(year, month, last if month_day == -1 else min(month_day, last))  # the 31st means "the last day" in short months
            if d >= start:
                yield d
            month += interval
            year += (month - 1) // 12
            month = (month - 1) % 12 + 1


def dates(rule):
    """Yield every occurrence date of a rule in order, honouring ``end``, ``max`` and the horizon.

    ``rule`` is a dict with: frequency, interval, weekdays (indexes 0=Mon), month_day, start (date), end (date|None),
    max (int|None).
    """
    limit = rule["start"] + timedelta(days=HORIZON_DAYS)
    count = 0
    for d in _raw_dates(rule["frequency"], rule["interval"], rule["weekdays"], rule["month_day"], rule["start"]):
        if d > limit or (rule["end"] is not None and d > rule["end"]):
            return
        if rule["max"] is not None and count >= rule["max"]:
            return
        count += 1
        yield d


def dates_between(rule, after, upto):
    """Occurrence dates ``d`` with ``after < d <= upto`` (``after`` may be None)."""
    out = []
    for d in dates(rule):
        if d > upto:
            break
        if after is None or d > after:
            out.append(d)
    return out


def upcoming(rule, on_or_after, count):
    out = []
    for d in dates(rule):
        if d >= on_or_after:
            out.append(d)
            if len(out) >= count:
                break
    return out


def describe(rule):
    n = rule["interval"]
    if rule["frequency"] == "daily":
        return "Every day" if n == 1 else "Every %d days" % n
    if rule["frequency"] == "weekly":
        days = ", ".join(WEEKDAYS[i].capitalize() for i in sorted(rule["weekdays"]))
        return "Every %s on %s" % ("week" if n == 1 else "%d weeks" % n, days)
    day = "the last day" if rule["month_day"] == -1 else "day %d" % rule["month_day"]
    return "Every %s on %s" % ("month" if n == 1 else "%d months" % n, day)


def expand_title(template, on):
    return template.replace("{date}", on.isoformat())


# ------------------------------------------------------------------------------ row <-> rule


def rule_of(row):
    return {
        "frequency": row["frequency"],
        "interval": row["interval_count"],
        "weekdays": json.loads(row["weekdays"] or "[]"),
        "month_day": row["month_day"],
        "start": parse_date(row["start_date"]),
        "end": parse_date(row["end_date"]) if row["end_date"] else None,
        "max": row["max_occurrences"],
    }


SELECT = (
    "SELECT r.*, p.key AS project_key, p.status AS project_status, a.username AS assignee_username, "
    "c.username AS created_by_username, "
    "(SELECT COUNT(*) FROM recurrence_occurrences o WHERE o.recurrence_id = r.id AND o.outcome = 'created') AS created_count "
    "FROM task_recurrences r JOIN projects p ON p.id = r.project_id "
    "LEFT JOIN users a ON a.id = r.assignee_id LEFT JOIN users c ON c.id = r.created_by "
)


def to_dict(row, today):
    rule = rule_of(row)
    nxt = upcoming(rule, today, 5) if row["active"] else []
    if not row["active"]:
        status = "paused"
    elif not upcoming(rule, today, 1):
        status = "finished"
    else:
        status = "active"
    return {
        "id": row["id"],
        "project": row["project_key"],
        "title": row["title"],
        "description": row["description"],
        "type": row["type"],
        "priority": row["priority"],
        "assignee": row["assignee_username"],
        "estimate_hours": row["estimate_hours"],
        "labels": json.loads(row["labels"] or "[]"),
        "due_offset_days": row["due_offset_days"],
        "frequency": row["frequency"],
        "interval": row["interval_count"],
        "weekdays": [WEEKDAYS[i] for i in sorted(rule["weekdays"])],
        "month_day": row["month_day"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "max_occurrences": row["max_occurrences"],
        "active": bool(row["active"]),
        "status": status,
        "schedule": describe(rule),
        "next_dates": [d.isoformat() for d in nxt],
        "generated_through": row["generated_through"],
        "tasks_created": row["created_count"],
        "created_by": row["created_by_username"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get(conn, recurrence_id):
    row = conn.execute(SELECT + "WHERE r.id = ?", (recurrence_id,)).fetchone()
    if row is None:
        raise NotFound("Recurring task not found", code="recurrence_not_found")
    return row


# ------------------------------------------------------------------------------ validation


def _weekday_indexes(value):
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValidationError({"weekdays": "must be a list of weekday names (%s)" % ", ".join(WEEKDAYS)})
    bad = [x for x in value if x.lower() not in WEEKDAYS]
    if bad:
        raise ValidationError({"weekdays": "unknown weekday %r (use %s)" % (bad[0], ", ".join(WEEKDAYS))})
    return sorted({WEEKDAYS.index(x.lower()) for x in value})


def _validate(data, *, partial, allowed=ALL_FIELDS):
    c = Checker(data, allowed, partial=partial)
    c.text("title", required=True, min_len=1, max_len=200)
    c.text("description", max_len=20000)
    c.choice("type", TASK_TYPES)
    c.choice("priority", PRIORITIES)
    c.raw("assignee")
    c.number("estimate_hours", minimum=0, maximum=10000, nullable=True)
    c.labels()
    c.integer("due_offset_days", minimum=0, maximum=365)
    c.choice("frequency", FREQUENCIES, required=True)
    c.integer("interval", minimum=1, maximum=365)
    c.integer("month_day", minimum=-1, maximum=31, nullable=True)
    c.date("start_date", required=True, nullable=False)
    c.date("end_date")
    c.integer("max_occurrences", minimum=1, maximum=1000, nullable=True)
    c.boolean("active")
    out = c.out
    if "weekdays" in data:
        try:
            out["weekdays"] = _weekday_indexes(data["weekdays"])
        except ValidationError as exc:
            c.errors.update(exc.details)
    if "month_day" in out and out["month_day"] == 0:
        c.errors["month_day"] = "must be 1-31, or -1 for the last day of the month"
    for name in ("start_date", "end_date"):
        d = parse_date(out.get(name)) if out.get(name) else None
        if d is not None and not MIN_DATE <= d <= MAX_DATE:
            c.errors[name] = "must be between %s and %s" % (MIN_DATE, MAX_DATE)
    return c.result()


def _assemble(base, v, today):
    """Combine the stored values (``base``, may be empty) with validated changes and check the schedule as a whole."""
    merged = {**base, **v}
    errors = {}
    freq = merged["frequency"]
    start = parse_date(merged["start_date"])
    end = parse_date(merged["end_date"]) if merged.get("end_date") else None
    merged.setdefault("interval", 1)
    # settings that do not belong to the chosen frequency are dropped, so a changed frequency never leaves junk behind
    if freq == "weekly":
        if not merged.get("weekdays"):
            merged["weekdays"] = [start.weekday()] if "weekdays" not in v else []
            if not merged["weekdays"]:
                errors["weekdays"] = "choose at least one weekday"
        merged["month_day"] = None
    elif freq == "monthly":
        if merged.get("month_day") is None:
            merged["month_day"] = start.day
        merged["weekdays"] = []
    else:
        merged["weekdays"], merged["month_day"] = [], None
    if end is not None and end < start:
        errors["end_date"] = "must not be before the start date"
    if errors:
        raise ValidationError(errors)
    rule = {
        "frequency": freq, "interval": merged["interval"], "weekdays": merged["weekdays"], "month_day": merged["month_day"],
        "start": start, "end": end, "max": merged.get("max_occurrences"),
    }  # fmt: skip
    first = next(dates(rule), None)
    if first is None:
        raise ValidationError({"end_date": "this schedule never produces a date"})
    sample = expand_title(merged["title"], first)
    if len(sample) > 200:
        raise ValidationError({"title": "is longer than 200 characters once {date} is filled in"})
    return merged, rule


# ------------------------------------------------------------------------------ queries


def list_recurrences(ctx, project_key):
    project = projects.get_row(ctx.conn, project_key)
    rows = ctx.conn.execute(SELECT + "WHERE r.project_id = ? ORDER BY r.id", (project["id"],)).fetchall()
    today = ctx.today
    return [to_dict(r, today) for r in rows]


def get_recurrence(ctx, recurrence_id):
    return to_dict(_get(ctx.conn, recurrence_id), ctx.today)


def occurrences_of(ctx, recurrence_id, limit=50):
    _get(ctx.conn, recurrence_id)
    rows = ctx.conn.execute(
        "SELECT occurrence_date, outcome, reason, task_key, created_at FROM recurrence_occurrences "
        "WHERE recurrence_id = ? ORDER BY occurrence_date DESC LIMIT ?",
        (recurrence_id, limit),
    ).fetchall()
    return [
        {"date": r["occurrence_date"], "outcome": r["outcome"], "reason": r["reason"], "task": r["task_key"], "recorded_at": r["created_at"]}
        for r in rows
    ]


def preview(ctx, data, count=10):
    """Show the dates a schedule would produce, without saving anything."""
    if isinstance(data, dict):
        data = {"title": "preview", **data}
    v = _validate(data, partial=False, allowed=SCHEDULE_FIELDS | {"title"})
    merged, rule = _assemble({}, v, ctx.today)
    return {
        "schedule": describe(rule),
        "dates": [d.isoformat() for d in upcoming(rule, max(rule["start"], ctx.today), count)],
    }


# ------------------------------------------------------------------------------ writes


def _check_assignee(ctx, project, v):
    if v.get("assignee") is None:
        return None
    return tasks._resolve_assignee(ctx, project, v["assignee"])["id"]


def _row_values(merged, assignee_id):
    return {
        "title": merged["title"], "description": merged.get("description", ""), "type": merged.get("type", "task"),
        "priority": merged.get("priority", "medium"), "assignee_id": assignee_id, "estimate_hours": merged.get("estimate_hours"),
        "labels": json.dumps(merged.get("labels", [])), "due_offset_days": merged.get("due_offset_days", 0),
        "frequency": merged["frequency"], "interval_count": merged["interval"], "weekdays": json.dumps(merged["weekdays"]),
        "month_day": merged["month_day"], "start_date": merged["start_date"], "end_date": merged.get("end_date"),
        "max_occurrences": merged.get("max_occurrences"),
    }  # fmt: skip


def create_recurrence(ctx, project_key, data):
    project = projects.get_row(ctx.conn, project_key)
    projects.require_write(ctx, project)
    v = _validate(data, partial=False)
    merged, rule = _assemble({}, v, ctx.today)
    count = ctx.conn.execute("SELECT COUNT(*) FROM task_recurrences WHERE project_id = ?", (project["id"],)).fetchone()[0]
    if count >= MAX_RULES_PER_PROJECT:
        raise ValidationError({"project": "at most %d recurring tasks per project" % MAX_RULES_PER_PROJECT})
    assignee_id = _check_assignee(ctx, project, v)
    vals = _row_values(merged, assignee_id)
    yesterday = (ctx.today - timedelta(days=1)).isoformat()
    # a rule that starts in the past begins producing tasks from today; history is never backfilled
    through = yesterday if rule["start"] <= ctx.today else None
    cols = list(vals)
    cur = ctx.conn.execute(
        "INSERT INTO task_recurrences (project_id, %s, active, generated_through, created_by, created_at, updated_at) VALUES (?,%s,?,?,?,?,?)"
        % (", ".join(cols), ",".join("?" * len(cols))),
        [project["id"], *vals.values(), 1 if merged.get("active", True) else 0, through, ctx.actor_id, ctx.now_iso, ctx.now_iso],
    )
    activity.log(
        ctx, "recurrence.created", project_id=project["id"],
        details={"id": cur.lastrowid, "title": merged["title"], "schedule": describe(rule)},
    )  # fmt: skip
    return get_recurrence(ctx, cur.lastrowid)


def update_recurrence(ctx, recurrence_id, data):
    row = _get(ctx.conn, recurrence_id)
    project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
    projects.require_write(ctx, project)
    v = _validate(data, partial=True)
    before = to_dict(row, ctx.today)
    base = {
        "title": row["title"], "description": row["description"], "type": row["type"], "priority": row["priority"],
        "estimate_hours": row["estimate_hours"], "labels": json.loads(row["labels"]), "due_offset_days": row["due_offset_days"],
        "frequency": row["frequency"], "interval": row["interval_count"], "weekdays": json.loads(row["weekdays"]),
        "month_day": row["month_day"], "start_date": row["start_date"], "end_date": row["end_date"],
        "max_occurrences": row["max_occurrences"], "active": bool(row["active"]),
    }  # fmt: skip
    if v.get("frequency") and v["frequency"] != row["frequency"]:  # a new frequency starts from its own defaults
        base["weekdays"], base["month_day"] = [], None
    merged, rule = _assemble(base, v, ctx.today)
    assignee_id = _check_assignee(ctx, project, v) if "assignee" in v else row["assignee_id"]
    vals = _row_values(merged, assignee_id)
    active = 1 if merged.get("active", True) else 0
    through = row["generated_through"]
    yesterday = (ctx.today - timedelta(days=1)).isoformat()
    schedule_changed = any(k in v for k in SCHEDULE_FIELDS)
    resumed = active and not row["active"]
    if (schedule_changed or resumed) and (through is None or through < yesterday) and rule["start"] <= ctx.today:
        through = yesterday  # editing or resuming a rule never backfills the time in between
    ctx.conn.execute(
        "UPDATE task_recurrences SET %s, active = ?, generated_through = ?, updated_at = ? WHERE id = ?"
        % ", ".join("%s = ?" % c for c in vals),
        [*vals.values(), active, through, ctx.now_iso, row["id"]],
    )
    after = to_dict(_get(ctx.conn, row["id"]), ctx.today)
    changes = {k: [before[k], after[k]] for k in (
        "title", "description", "type", "priority", "assignee", "estimate_hours", "labels", "due_offset_days", "schedule",
        "start_date", "end_date", "max_occurrences", "active",
    ) if before[k] != after[k]}  # fmt: skip
    if changes:
        activity.log(
            ctx, "recurrence.updated", project_id=project["id"],
            details={"id": row["id"], "title": after["title"], "changes": {k: [activity.diff_value(a), activity.diff_value(b)] for k, (a, b) in changes.items()}},
        )  # fmt: skip
    return after


def delete_recurrence(ctx, recurrence_id):
    row = _get(ctx.conn, recurrence_id)
    project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
    projects.require_write(ctx, project)
    ctx.conn.execute("DELETE FROM task_recurrences WHERE id = ?", (row["id"],))  # tasks it already created stay
    activity.log(
        ctx, "recurrence.deleted", project_id=project["id"],
        details={"id": row["id"], "title": row["title"], "tasks_created": row["created_count"]},
    )  # fmt: skip


# ------------------------------------------------------------------------------ the generator


def _reporter(ctx, row, project):
    for uid in (row["created_by"], project["owner_id"]):
        if uid is not None:
            u = users.get_row(ctx.conn, uid)
            if u["active"]:
                return u["id"]
    return None


def _record(ctx, recurrence_id, day, outcome, reason=None, task_id=None, task_key=None):
    cur = ctx.conn.execute(
        "INSERT OR IGNORE INTO recurrence_occurrences (recurrence_id, occurrence_date, outcome, reason, task_id, task_key, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (recurrence_id, day.isoformat(), outcome, reason, task_id, task_key, ctx.now_iso),
    )
    return cur.rowcount == 1


def _create_one(ctx, row, project, day, reporter_id):
    """Create the task for one date inside a savepoint. Returns (task_id, task_key, note) or raises ApiError."""
    assignee, note = None, None
    if row["assignee_id"] is not None:
        a = users.get_row(ctx.conn, row["assignee_id"])
        if a["active"] and projects.is_member(ctx.conn, project["id"], a["id"]):
            assignee = a["username"]
        else:
            note = "assignee_unavailable"
    data = {
        "title": expand_title(row["title"], day), "description": row["description"], "type": row["type"], "priority": row["priority"],
        "labels": json.loads(row["labels"]), "start_date": day.isoformat(),
        "due_date": (day + timedelta(days=row["due_offset_days"])).isoformat(),
    }  # fmt: skip
    if row["estimate_hours"] is not None:
        data["estimate_hours"] = row["estimate_hours"]
    if assignee:
        data["assignee"] = assignee
    task_id, key = tasks.create_in_project(ctx, project, data, reporter_id=reporter_id)
    return task_id, key, note


def run(ctx, *, project_key=None, dry_run=False):
    """Create the tasks that are due. Safe to run as often as you like."""
    today = ctx.today
    sql = SELECT + "WHERE r.active = 1 "
    args = []
    if project_key:
        sql += "AND r.project_id = ? "
        args.append(projects.get_row(ctx.conn, project_key)["id"])
    rows = ctx.conn.execute(sql + "ORDER BY r.id", args).fetchall()
    report = {"as_of": today.isoformat(), "dry_run": bool(dry_run), "checked": len(rows), "created": [], "skipped": []}
    for row in rows:
        project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
        after = parse_date(row["generated_through"]) if row["generated_through"] else None
        due = dates_between(rule_of(row), after, today)
        if not due:
            continue
        if project["status"] in ("archived", "completed"):
            report["skipped"].append({"recurrence": row["id"], "project": project["key"], "dates": [d.isoformat() for d in due], "reason": "project_" + project["status"], "kept_for_later": True})
            continue  # not advanced: the dates are still due once the project is open again (subject to the catch-up limit)
        missed, chosen = due[:-MAX_CATCH_UP], due[-MAX_CATCH_UP:]
        reporter_id = _reporter(ctx, row, project)
        skipped_dates, skip_reason = [d.isoformat() for d in missed], "catch_up_limit" if missed else None
        if not dry_run:
            for d in missed:
                _record(ctx, row["id"], d, "skipped", "catch_up_limit")
        for d in chosen:
            entry = {"recurrence": row["id"], "project": project["key"], "date": d.isoformat(), "task": None}
            if reporter_id is None:
                skipped_dates.append(d.isoformat())
                skip_reason = "no_reporter"
                if not dry_run:
                    _record(ctx, row["id"], d, "skipped", "no_reporter")
                continue
            if dry_run:
                report["created"].append(entry)
                continue
            if not _record(ctx, row["id"], d, "created"):
                continue  # already handled by an earlier or concurrent run
            ctx.conn.execute("SAVEPOINT occurrence")
            try:
                task_id, key, note = _create_one(ctx, row, project, d, reporter_id)
            except ApiError as exc:
                ctx.conn.execute("ROLLBACK TO occurrence")
                ctx.conn.execute("RELEASE occurrence")
                reason = "error: " + (next(iter(exc.details.values())) if exc.details else exc.message)[:150]
                ctx.conn.execute(
                    "UPDATE recurrence_occurrences SET outcome = 'skipped', reason = ? WHERE recurrence_id = ? AND occurrence_date = ?",
                    (reason, row["id"], d.isoformat()),
                )
                skipped_dates.append(d.isoformat())
                skip_reason = reason
                continue
            ctx.conn.execute("RELEASE occurrence")
            ctx.conn.execute(
                "UPDATE recurrence_occurrences SET task_id = ?, task_key = ?, reason = ? WHERE recurrence_id = ? AND occurrence_date = ?",
                (task_id, key, note, row["id"], d.isoformat()),
            )
            activity.log(
                ctx, "recurrence.generated", project_id=project["id"], task_id=task_id, task_key=key,
                details={"id": row["id"], "title": row["title"], "occurrence": d.isoformat(), **({"note": note} if note else {})},
            )  # fmt: skip
            entry["task"] = key
            if note:
                entry["note"] = note
            report["created"].append(entry)
        if skipped_dates:
            report["skipped"].append({"recurrence": row["id"], "project": project["key"], "dates": skipped_dates, "reason": skip_reason})
            if not dry_run:
                activity.log(
                    ctx, "recurrence.skipped", project_id=project["id"],
                    details={"id": row["id"], "title": row["title"], "dates": skipped_dates[:20], "count": len(skipped_dates), "reason": skip_reason},
                )  # fmt: skip
        if not dry_run:
            ctx.conn.execute("UPDATE task_recurrences SET generated_through = ? WHERE id = ?", (due[-1].isoformat(), row["id"]))
    return report
