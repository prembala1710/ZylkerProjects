"""Timesheet approval: people submit a week of logged time, a manager approves or sends it back, and an
approved week can no longer change.

A *timesheet* is one person's Monday-to-Sunday week. It has no row until it is first submitted; a week without a row
is simply *open*. The states are:

* ``submitted``  - waiting for a decision. Locked: time cannot be added to or removed from the week.
* ``approved``   - decided. Locked, and an administrator is needed to open it again.
* ``rejected``   - sent back with a reason. Open again; the person fixes it and submits once more.
* ``withdrawn``  - taken back by its owner before anyone decided. Open.
* ``reopened``   - an approved week an administrator opened up again (with a reason). Open.

Rules that matter:

* Nobody decides on their own timesheet, and nobody on the sheet of someone with a higher role than their own.
* Submitting records a digest of the entries. Approving checks the entries still match it, so an approved week can be
  proven unchanged (``integrity``), even against a change made behind the application's back.
* Every locking rule is enforced in the one place that writes time (``collab``) and where time could vanish
  (deleting a task or a project), not only in the endpoints of this module.
* A decision can name the ``revision`` the approver looked at; if the person resubmitted in the meantime, the decision
  is refused instead of approving something nobody read.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, timedelta

from ..domain import ROLE_RANK
from ..errors import ApiError, Conflict, Forbidden, NotFound, ValidationError
from ..util import Checker, parse_date
from . import activity, users

LOCKED = ("submitted", "approved")
STATES = ("open", "submitted", "approved", "rejected", "withdrawn", "reopened")
LONG_DAY_HOURS = 12.0
DAY_LIMIT_HOURS = 24.0
MAX_BULK = 100
MAX_WEEKS_BACK = 26
DEFAULT_WEEKS_BACK = 4
MAX_PAGE = 200
EARLIEST = date(2000, 1, 3)  # a Monday

# The Monday of the week an entry belongs to: step back six days, then forward to the next Monday.
WEEK_OF = "date(e.work_date, '-6 days', 'weekday 1')"
_LOCKED_SQL = "(" + ",".join("'%s'" % s for s in LOCKED) + ")"
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _week_end(monday: date) -> date:
    return monday + timedelta(days=6)


# ------------------------------------------------------------------------------ the lock


def lock_on(conn, user_id, work_date):
    """The submitted or approved timesheet that contains this person's day, or None."""
    monday = week_start(parse_date(work_date)).isoformat()
    return conn.execute(
        "SELECT * FROM timesheets WHERE user_id = ? AND week_start = ? AND status IN " + _LOCKED_SQL, (user_id, monday)
    ).fetchone()


def assert_unlocked(ctx, user_id, work_date):
    """Raise 409 ``timesheet_locked`` if the day belongs to a week that is waiting for, or has had, approval."""
    sheet = lock_on(ctx.conn, user_id, work_date)
    if sheet is None:
        return
    if sheet["status"] == "submitted":
        hint = "withdraw it first, or ask a manager to send it back"
    else:
        hint = "an administrator can reopen it"
    raise Conflict(
        "The timesheet for the week of %s is %s; %s" % (sheet["week_start"], sheet["status"], hint),
        code="timesheet_locked",
        details={"timesheet": sheet["id"], "week_start": sheet["week_start"], "status": sheet["status"]},
    )


def assert_tasks_have_no_locked_time(ctx, task_ids):
    """Refuse to delete tasks whose hours sit in a locked week: deleting would silently change what was submitted
    or approved."""
    ids = list(task_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        _refuse_locked_time(ctx, "t.id IN (%s)" % ",".join("?" * len(chunk)), chunk)


def assert_project_has_no_locked_time(ctx, project_id):
    _refuse_locked_time(ctx, "t.project_id = ?", [project_id])


def _refuse_locked_time(ctx, where, args):
    rows = ctx.conn.execute(
        "SELECT s.id, s.week_start, s.status, u.username FROM time_entries e "
        "JOIN tasks t ON t.id = e.task_id "
        "JOIN timesheets s ON s.user_id = e.user_id AND s.week_start = " + WEEK_OF + " AND s.status IN " + _LOCKED_SQL + " "
        "JOIN users u ON u.id = e.user_id WHERE " + where + " GROUP BY s.id ORDER BY s.week_start LIMIT 5",
        args,
    ).fetchall()
    if rows:
        raise Conflict(
            "Hours logged on this work are part of submitted or approved timesheets (%s); withdraw or reopen those first"
            % ", ".join("%s, week of %s" % (r["username"], r["week_start"]) for r in rows),
            code="timesheet_locked",
            details={"timesheets": [{"id": r["id"], "user": r["username"], "week_start": r["week_start"], "status": r["status"]} for r in rows]},
        )


# ------------------------------------------------------------------------------ reading a week


def _digest(rows):
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["id"]):
        h.update(("%d|%d|%.4f|%s|%s\n" % (r["id"], r["task_id"], r["hours"], r["work_date"], r["note"])).encode())
    return h.hexdigest()


def _entries(conn, user_id, monday):
    return conn.execute(
        "SELECT e.id, e.task_id, e.hours, e.work_date, e.note, e.created_at, t.number, p.key AS project_key, t.title "
        "FROM time_entries e JOIN tasks t ON t.id = e.task_id JOIN projects p ON p.id = t.project_id "
        "WHERE e.user_id = ? AND e.work_date BETWEEN ? AND ? ORDER BY e.work_date, e.id",
        (user_id, monday.isoformat(), _week_end(monday).isoformat()),
    ).fetchall()


def _sheet(conn, user_id, monday):
    return conn.execute("SELECT * FROM timesheets WHERE user_id = ? AND week_start = ?", (user_id, monday.isoformat())).fetchone()


def _ref(conn, user_id):
    if user_id is None:
        return None
    row = users.get_row(conn, user_id)
    return users.ref(row["id"], row["username"], row["display_name"])


def _analyse(entries, monday, capacity):
    """Per-day and per-project totals, plus what would stop a submission and what is merely worth a look."""
    by_day = defaultdict(float)
    by_project = defaultdict(float)
    for e in entries:
        by_day[e["work_date"]] += e["hours"]
        by_project[e["project_key"]] += e["hours"]
    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        days.append({"date": d.isoformat(), "weekday": _DAY_NAMES[i], "hours": round(by_day.get(d.isoformat(), 0.0), 2)})
    total = round(sum(e["hours"] for e in entries), 2)
    blocking, warnings = [], []
    if not entries:
        blocking.append({"code": "no_time", "message": "There is no time logged in this week"})
    for d in days:
        if d["hours"] > DAY_LIMIT_HOURS:
            blocking.append({"code": "day_over_24", "message": "%s has %s hours logged, more than a day holds" % (d["date"], d["hours"]), "date": d["date"]})
        elif d["hours"] > LONG_DAY_HOURS:
            warnings.append({"code": "long_day", "message": "%s has %s hours logged" % (d["date"], d["hours"]), "date": d["date"]})
    weekend = round(days[5]["hours"] + days[6]["hours"], 2)
    if weekend > 0:
        warnings.append({"code": "weekend_work", "message": "%s hours are logged on the weekend" % weekend, "hours": weekend})
    if capacity and total > capacity:
        warnings.append({"code": "over_capacity", "message": "%s hours is more than the weekly capacity of %s" % (total, capacity), "hours": total})
    projects = [{"project": k, "hours": round(h, 2)} for k, h in sorted(by_project.items())]
    return days, projects, total, blocking, warnings


def _entry_dict(e):
    return {
        "id": e["id"], "task": "%s-%d" % (e["project_key"], e["number"]), "title": e["title"], "hours": e["hours"],
        "work_date": e["work_date"], "note": e["note"],
    }  # fmt: skip


def _events(conn, sheet_id):
    rows = conn.execute("SELECT * FROM timesheet_events WHERE timesheet_id = ? ORDER BY id DESC LIMIT 50", (sheet_id,)).fetchall()
    return [
        {"action": r["action"], "actor": _ref(conn, r["actor_id"]), "revision": r["revision"], "hours": r["hours"], "note": r["note"], "at": r["created_at"]}
        for r in rows
    ]  # fmt: skip


def _view(ctx, owner, monday):
    conn = ctx.conn
    sheet = _sheet(conn, owner["id"], monday)
    entries = _entries(conn, owner["id"], monday)
    days, project_totals, total, blocking, warnings = _analyse(entries, monday, owner["weekly_capacity_hours"])
    state = sheet["status"] if sheet else "open"
    locked = state in LOCKED
    integrity = None
    if locked:
        integrity = "ok" if _digest(entries) == sheet["digest"] else "changed"
    is_owner = ctx.user is not None and ctx.user["id"] == owner["id"]
    problems = list(blocking)
    if monday > ctx.today:
        problems.append({"code": "future_week", "message": "That week has not started yet"})
    return {
        "id": sheet["id"] if sheet else None,
        "user": users.ref(owner["id"], owner["username"], owner["display_name"]),
        "week_start": monday.isoformat(),
        "week_end": _week_end(monday).isoformat(),
        "status": state,
        "locked": locked,
        "revision": sheet["revision"] if sheet else 0,
        "total_hours": total,
        "capacity_hours": owner["weekly_capacity_hours"],
        "by_day": days,
        "by_project": project_totals,
        "entries": [_entry_dict(e) for e in entries],
        "warnings": warnings,
        "blocking": problems,
        "can_submit": bool(is_owner and not locked and not problems and ctx.has_role("member")),
        "note": sheet["note"] if sheet else "",
        "submitted_at": sheet["submitted_at"] if sheet else None,
        "decided_by": _ref(conn, sheet["decided_by"]) if sheet else None,
        "decided_at": sheet["decided_at"] if sheet else None,
        "decision_note": sheet["decision_note"] if sheet else "",
        "integrity": integrity,
        "submitted": {"hours": sheet["total_hours"], "entries": sheet["entry_count"]} if sheet else None,
        "events": _events(conn, sheet["id"]) if sheet else [],
    }  # fmt: skip


def _may_view(ctx, owner):
    if ctx.user is not None and ctx.user["id"] != owner["id"] and not ctx.has_role("manager"):
        raise Forbidden("You can only view your own timesheet", code="insufficient_role")


def _owner_from_param(ctx, wanted, field="user"):
    """``me`` (or nothing) is the caller; a username needs the manager role."""
    wanted = (wanted or "").strip()
    if ctx.user is None and wanted in ("", "me"):
        raise ValidationError({field: "a user is required"})
    if wanted in ("", "me") or (ctx.user is not None and wanted.lower() == ctx.user["username"].lower()):
        return ctx.user
    owner = users.find_by_username(ctx.conn, wanted)
    if owner is None:
        raise NotFound("User not found", code="user_not_found")
    _may_view(ctx, owner)
    return owner


def _week_param(ctx, value, field="week"):
    if value in (None, ""):
        return week_start(ctx.today)
    day = parse_date(value)
    if day is None:
        raise ValidationError({field: "must be a valid date in YYYY-MM-DD format (any day of the week)"})
    if day < EARLIEST:
        raise ValidationError({field: "is too far in the past"})
    return week_start(day)


def week_view(ctx, params):
    """One person's week - open or not - as they would see it before submitting."""
    monday = _week_param(ctx, params.get("week"))
    owner = _owner_from_param(ctx, params.get("user"))
    return _view(ctx, owner, monday)


def _row_of(ctx, sheet_id):
    row = ctx.conn.execute("SELECT * FROM timesheets WHERE id = ?", (sheet_id,)).fetchone()
    if row is None:
        raise NotFound("Timesheet not found", code="timesheet_not_found")
    return row


def get_sheet(ctx, sheet_id):
    row = _row_of(ctx, sheet_id)
    owner = users.get_row(ctx.conn, row["user_id"])
    _may_view(ctx, owner)
    return _view(ctx, owner, parse_date(row["week_start"]))


def _summary(conn, r):
    return {
        "id": r["id"], "user": users.ref(r["user_id"], r["username"], r["display_name"]), "week_start": r["week_start"],
        "week_end": _week_end(parse_date(r["week_start"])).isoformat(), "status": r["status"], "total_hours": r["total_hours"],
        "entry_count": r["entry_count"], "revision": r["revision"], "note": r["note"], "submitted_at": r["submitted_at"],
        "decided_by": _ref(conn, r["decided_by"]), "decided_at": r["decided_at"], "decision_note": r["decision_note"],
    }  # fmt: skip


def _int_param(params, name, default, low, high):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: "must be an integer"}) from None
    if not low <= value <= high:
        raise ValidationError({name: "must be between %d and %d" % (low, high)})
    return value


def list_sheets(ctx, params):
    where, args = [], []
    statuses = [s.strip() for s in (params.get("status") or "").split(",") if s.strip()]
    bad = [s for s in statuses if s not in STATES or s == "open"]
    if bad:
        raise ValidationError({"status": "unknown status %s; allowed: %s" % (", ".join(bad), ", ".join(s for s in STATES if s != "open"))})
    if statuses:
        where.append("s.status IN (%s)" % ",".join("?" * len(statuses)))
        args += statuses
    wanted = (params.get("user") or "").strip()
    if ctx.user is not None and not ctx.has_role("manager"):  # everyone else sees their own sheets and nobody else's
        if wanted and wanted != "me" and wanted.lower() != ctx.user["username"].lower():
            raise Forbidden("You can only view your own timesheets", code="insufficient_role")
        where.append("s.user_id = ?")
        args.append(ctx.user["id"])
    elif wanted:
        where.append("s.user_id = ?")
        args.append(_owner_from_param(ctx, wanted)["id"])
    for name, op in (("from", ">="), ("to", "<=")):
        if params.get(name):
            day = parse_date(params[name])
            if day is None:
                raise ValidationError({name: "must be a valid date in YYYY-MM-DD format"})
            where.append("s.week_start %s ?" % op)
            args.append(week_start(day).isoformat())
    limit = _int_param(params, "limit", 50, 1, MAX_PAGE)
    offset = _int_param(params, "offset", 0, 0, 1_000_000)
    clause = "WHERE " + " AND ".join(where) if where else ""
    total = ctx.conn.execute("SELECT COUNT(*) FROM timesheets s " + clause, args).fetchone()[0]
    queue = statuses == ["submitted"]  # a queue of work to do: oldest first
    order = "s.submitted_at ASC, s.id ASC" if queue else "s.week_start DESC, s.id DESC"
    rows = ctx.conn.execute(
        "SELECT s.*, u.username, u.display_name FROM timesheets s JOIN users u ON u.id = s.user_id " + clause + " ORDER BY " + order + " LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    return {"items": [_summary(ctx.conn, r) for r in rows], "total": total, "limit": limit, "offset": offset}


# ------------------------------------------------------------------------------ submitting


def _record(ctx, sheet_id, action, revision, hours, note=""):
    ctx.conn.execute(
        "INSERT INTO timesheet_events (timesheet_id, action, actor_id, revision, hours, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (sheet_id, action, ctx.actor_id, revision, hours, note, ctx.now_iso),
    )


def _details(sheet, owner, **extra):
    return {"id": sheet["id"], "user": owner["username"], "week_start": sheet["week_start"], "hours": sheet["total_hours"], **extra}


def submit(ctx, data):
    if ctx.user is None:
        raise ValidationError({"user": "a timesheet can only be submitted by a signed-in user"})
    c = Checker(data, {"week", "note"})
    c.date("week", nullable=False)
    c.text("note", max_len=500)
    v = c.result()
    monday = _week_param(ctx, v.get("week"))
    if monday > ctx.today:
        raise ValidationError({"week": "that week has not started yet"})
    owner = ctx.user
    existing = _sheet(ctx.conn, owner["id"], monday)
    if existing is not None and existing["status"] in LOCKED:
        raise Conflict(
            "The timesheet for the week of %s is already %s" % (monday.isoformat(), existing["status"]),
            code="timesheet_locked",
            details={"timesheet": existing["id"], "week_start": existing["week_start"], "status": existing["status"]},
        )
    entries = _entries(ctx.conn, owner["id"], monday)
    _, _, total, blocking, _ = _analyse(entries, monday, owner["weekly_capacity_hours"])
    if blocking:
        code = "timesheet_empty" if blocking[0]["code"] == "no_time" else "timesheet_invalid"
        raise Conflict(blocking[0]["message"], code=code, details={"problems": blocking})
    digest = _digest(entries)
    note = v.get("note", "")
    if existing is None:
        cur = ctx.conn.execute(
            "INSERT INTO timesheets (user_id, week_start, status, total_hours, entry_count, digest, note, revision, submitted_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (owner["id"], monday.isoformat(), "submitted", total, len(entries), digest, note, 1, ctx.now_iso),
        )
        sheet_id, revision = cur.lastrowid, 1
    else:
        sheet_id, revision = existing["id"], existing["revision"] + 1
        ctx.conn.execute(
            "UPDATE timesheets SET status = 'submitted', total_hours = ?, entry_count = ?, digest = ?, note = ?, revision = ?, "
            "submitted_at = ?, decided_by = NULL, decided_at = NULL, decision_note = '' WHERE id = ?",
            (total, len(entries), digest, note, revision, ctx.now_iso, sheet_id),
        )
    _record(ctx, sheet_id, "submitted", revision, total, note)
    sheet = _row_of(ctx, sheet_id)
    activity.log(ctx, "timesheet.submitted", details=_details(sheet, owner, revision=revision, entries=len(entries)))
    ctx.emit("timesheet_submitted", user=owner["display_name"], week=monday.isoformat(), hours=total)
    return _view(ctx, owner, monday)


def withdraw(ctx, sheet_id):
    sheet = _row_of(ctx, sheet_id)
    owner = users.get_row(ctx.conn, sheet["user_id"])
    if ctx.user is None or ctx.user["id"] != owner["id"]:
        raise Forbidden("Only the owner of a timesheet can withdraw it", code="not_owner")
    if sheet["status"] != "submitted":
        raise Conflict("Only a submitted timesheet can be withdrawn (this one is %s)" % sheet["status"], code="timesheet_not_submitted", details={"status": sheet["status"]})
    changed = ctx.conn.execute("UPDATE timesheets SET status = 'withdrawn' WHERE id = ? AND status = 'submitted'", (sheet_id,)).rowcount
    if changed != 1:
        raise Conflict("The timesheet changed while you were withdrawing it", code="timesheet_changed")
    _record(ctx, sheet_id, "withdrawn", sheet["revision"], sheet["total_hours"])
    activity.log(ctx, "timesheet.withdrawn", details=_details(sheet, owner, revision=sheet["revision"]))
    return _view(ctx, owner, parse_date(sheet["week_start"]))


# ------------------------------------------------------------------------------ deciding


def _decider(ctx, sheet, owner):
    if ctx.user is None:
        raise ValidationError({"user": "a decision can only be made by a signed-in user"})
    if ctx.user["id"] == owner["id"]:
        raise Forbidden("You cannot decide on your own timesheet", code="own_timesheet")
    if ROLE_RANK[ctx.user["role"]] < ROLE_RANK[owner["role"]]:
        raise Forbidden(
            "A %s's timesheet needs someone of the %s role or higher" % (owner["role"], owner["role"]), code="approver_role_too_low"
        )


def _decide(ctx, sheet_id, action, data, *, announce=True):
    c = Checker(data, {"note", "revision"})
    c.text("note", max_len=500, min_len=1 if action in ("rejected", "reopened") else 0)
    if action in ("rejected", "reopened") and "note" not in data:
        c.errors["note"] = "a reason is required"
    c.integer("revision", minimum=1)
    v = c.result()
    sheet = _row_of(ctx, sheet_id)
    owner = users.get_row(ctx.conn, sheet["user_id"])
    _decider(ctx, sheet, owner)
    needed = "approved" if action == "reopened" else "submitted"
    if sheet["status"] != needed:
        raise Conflict(
            "Only a %s timesheet can be %s (this one is %s)" % (needed, action, sheet["status"]),
            code="timesheet_not_submitted" if needed == "submitted" else "timesheet_not_approved",
            details={"status": sheet["status"]},
        )
    if "revision" in v and v["revision"] != sheet["revision"]:
        raise Conflict(
            "The timesheet was resubmitted (revision %d, not %d); look at it again" % (sheet["revision"], v["revision"]),
            code="timesheet_changed", details={"revision": sheet["revision"]},
        )  # fmt: skip
    monday = parse_date(sheet["week_start"])
    if action == "approved":
        if _digest(_entries(ctx.conn, owner["id"], monday)) != sheet["digest"]:
            raise Conflict(
                "The time in this week no longer matches what was submitted; ask %s to withdraw and submit again" % owner["display_name"],
                code="timesheet_integrity",
            )
    note = v.get("note", "")
    status = {"approved": "approved", "rejected": "rejected", "reopened": "reopened"}[action]
    changed = ctx.conn.execute(
        "UPDATE timesheets SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ? AND status = ? AND revision = ?",
        (status, ctx.actor_id, ctx.now_iso, note, sheet_id, needed, sheet["revision"]),
    ).rowcount
    if changed != 1:
        raise Conflict("The timesheet changed while you were deciding on it", code="timesheet_changed")
    _record(ctx, sheet_id, action, sheet["revision"], sheet["total_hours"], note)
    activity.log(ctx, "timesheet." + action, details=_details(sheet, owner, revision=sheet["revision"], note=note))
    if announce:
        ctx.emit(
            "timesheet_decided", decision=action, user=owner["display_name"], approver=ctx.actor_name,
            week=sheet["week_start"], hours=sheet["total_hours"], note=note,
        )  # fmt: skip
    return _view(ctx, owner, monday)


def approve(ctx, sheet_id, data):
    return _decide(ctx, sheet_id, "approved", data)


def reject(ctx, sheet_id, data):
    return _decide(ctx, sheet_id, "rejected", data)


def reopen(ctx, sheet_id, data):
    return _decide(ctx, sheet_id, "reopened", data)


def approve_many(ctx, data):
    """Approve several timesheets at once. Each is judged on its own: the ones that cannot be approved are listed
    with the reason, the rest are approved. One chat message summarises the lot."""
    c = Checker(data, {"ids", "note"})
    c.text("note", max_len=500)
    c.raw("ids", required=True)
    v = c.result()
    ids = v["ids"]
    if not isinstance(ids, list) or not ids or len(ids) > MAX_BULK or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValidationError({"ids": "must be a list of 1 to %d timesheet ids" % MAX_BULK})
    approved, skipped, hours = [], [], 0.0
    for sheet_id in dict.fromkeys(ids):
        ctx.conn.execute("SAVEPOINT approve_one")
        try:
            result = _decide(ctx, sheet_id, "approved", {"note": v["note"]} if "note" in v else {}, announce=False)
        except ApiError as exc:
            ctx.conn.execute("ROLLBACK TO approve_one")
            skipped.append({"id": sheet_id, "code": exc.code, "message": exc.message})
        else:
            approved.append(sheet_id)
            hours += result["total_hours"]
        finally:
            ctx.conn.execute("RELEASE approve_one")
    if approved:
        ctx.emit("timesheets_approved", approver=ctx.actor_name, count=len(approved), hours=round(hours, 2))
    return {"approved": approved, "skipped": skipped, "hours": round(hours, 2)}


# ------------------------------------------------------------------------------ reports


def missing(ctx, params):
    """Time that was logged in a finished week but is not waiting for, or past, approval: who to chase."""
    weeks = _int_param(params, "weeks", DEFAULT_WEEKS_BACK, 1, MAX_WEEKS_BACK)
    this_monday = week_start(ctx.today)
    first = this_monday - timedelta(days=7 * weeks)
    rows = ctx.conn.execute(
        "SELECT u.id, u.username, u.display_name, " + WEEK_OF + " AS wk, ROUND(SUM(e.hours), 2) AS hours, COUNT(*) AS entries, s.status "
        "FROM time_entries e JOIN users u ON u.id = e.user_id "
        "LEFT JOIN timesheets s ON s.user_id = e.user_id AND s.week_start = " + WEEK_OF + " "
        "WHERE e.work_date >= ? AND e.work_date < ? AND u.active = 1 "
        "GROUP BY e.user_id, wk ORDER BY u.username COLLATE NOCASE, wk",
        (first.isoformat(), this_monday.isoformat()),
    ).fetchall()
    items = []
    for r in rows:
        if r["status"] in LOCKED:
            continue
        monday = parse_date(r["wk"])
        items.append({
            "user": users.ref(r["id"], r["username"], r["display_name"]), "week_start": r["wk"],
            "week_end": _week_end(monday).isoformat(), "hours": r["hours"], "entries": r["entries"],
            "status": r["status"] or "open", "days_since_week_end": (ctx.today - _week_end(monday)).days,
        })  # fmt: skip
    return {"from": first.isoformat(), "weeks": weeks, "items": items, "people": len({i["user"]["id"] for i in items}), "hours": round(sum(i["hours"] for i in items), 2)}


def summary(ctx, params):
    """Hours by approval state, per project and per person. Each entry counts under the state of the week it falls in."""
    from .reports import parse_range  # local: reports imports collab, which imports this module

    frm, to = parse_range(params, ctx)
    where = ["e.work_date BETWEEN ? AND ?"]
    args = [frm.isoformat(), to.isoformat()]
    if params.get("project"):
        where.append("p.key = ?")
        args.append(params["project"].upper())
    if params.get("user"):
        owner = users.find_by_username(ctx.conn, params["user"])
        where.append("e.user_id = ?")
        args.append(owner["id"] if owner else -1)
    rows = ctx.conn.execute(
        "SELECT CASE WHEN s.status IN " + _LOCKED_SQL + " THEN s.status ELSE 'open' END AS state, p.key AS project, "
        "u.username, u.display_name, SUM(e.hours) AS hours FROM time_entries e "
        "JOIN tasks t ON t.id = e.task_id JOIN projects p ON p.id = t.project_id JOIN users u ON u.id = e.user_id "
        "LEFT JOIN timesheets s ON s.user_id = e.user_id AND s.week_start = " + WEEK_OF + " "
        "WHERE " + " AND ".join(where) + " GROUP BY state, p.key, u.id",
        args,
    ).fetchall()

    def zeros():
        return dict.fromkeys(("approved", "submitted", "open"), 0.0)

    totals = zeros()
    by_project, by_user, names = defaultdict(zeros), defaultdict(zeros), {}
    for r in rows:
        totals[r["state"]] += r["hours"]
        by_project[r["project"]][r["state"]] += r["hours"]
        by_user[r["username"]][r["state"]] += r["hours"]
        names[r["username"]] = r["display_name"]

    def shaped(d):
        out = {k: round(v, 2) for k, v in d.items()}
        out["total"] = round(sum(d.values()), 2)
        return out

    return {
        "from": frm.isoformat(), "to": to.isoformat(), "totals": shaped(totals),
        "by_project": [{"project": k, **shaped(v)} for k, v in sorted(by_project.items())],
        "by_user": [{"username": k, "display_name": names[k], **shaped(v)} for k, v in sorted(by_user.items())],
    }  # fmt: skip
