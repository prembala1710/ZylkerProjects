"""Delivery projection: when will the open work really finish, which tasks will miss their dates, and why?

Most trackers tell you a task is late once its due date has passed. This module plays the plan forward instead. It takes
every open leaf task of every project that is being worked on (planning or active), the hours each still needs (estimate
minus time logged; unestimated tasks count ``default_task_hours``), who is assigned, what each task waits for, and how many
hours a week each person can work, and runs the scheduling engine over all of it **at once** - so a person who is shared
between projects is not promised to both. The result says when each task should finish, which ones will miss their due
dates, by how many days, and *why* (it waits for another task, or it waits behind something else in somebody's queue).

It is a projection, not a promise: estimates are taken at face value, work days are Monday to Friday with no holiday
calendar, and nobody is pre-empted. The what-if options (``capacity``, ``overrun``, ``reassign``) show how fragile it is, and
``advise`` turns a warning into a way out: which single reassignment would help most.

Nothing is stored: every number is derived from live data, like the rest of the reports.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace
from fractions import Fraction

from ..domain import OPEN_SQL, OPEN_STATUSES
from ..errors import BadRequest, Conflict, NotFound, ValidationError
from ..util import parse_date, parse_task_key
from . import projects, scheduler, taskrefs, users

HORIZON_DAYS = 1300  # working days (about five years) looked ahead before work counts as "beyond the horizon"
UNASSIGNED_HOURS_PER_DAY = 8  # the pace of a task nobody is assigned to; each such task proceeds on its own
MAX_TASKS = 10000  # open leaf tasks a projection is willing to schedule
MAX_LISTED = 1000  # tasks returned for one project (late ones first)
MAX_ADVISED = 3  # projects in the early warning that get a suggestion (the worst first)
MAX_TRIALS = 60  # reassignments one piece of advice is willing to try
WORK_BUDGET = 30000  # tasks scheduled again, summed over the trials of one piece of advice: a trial in a big tangle of people costs more, and the answer is never slow
PROJECTED_STATUSES = ("planning", "active")
PRIORITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
FAR_FUTURE = "9999-12-31"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
NUMBER_RE = re.compile(r"^\d{1,4}(\.\d{1,2})?$")
VERDICT_RANK = {"will_miss": 0, "unknown": 1, "on_track": 2, "no_due_date": 3}


def _frac(value) -> Fraction:
    return Fraction(str(round(float(value), 4)))


def fmt_date(day, today=None) -> str:
    """``Tue 24 Nov``; the year is added when it is not the current one."""
    text = "%s %d %s" % (WEEKDAYS[day.weekday()], day.day, MONTHS[day.month - 1])
    return text + " %d" % day.year if today is not None and day.year != today.year else text


def plural(n, word) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# ------------------------------------------------------------------------------------------------ what-if


def _reassignment(ctx, item, done):
    """One ``WEB-3:carol`` of the ``reassign`` option: ``(task row, user row)``, or the sentence that says what is wrong with it."""
    ref, _, name = item.partition(":")
    ref, name = ref.strip(), name.strip()
    try:
        task = taskrefs.get_row(ctx.conn, ref)
    except (BadRequest, NotFound):
        return "unknown task %r (use KEY-1:username, e.g. WEB-3:carol)" % ref
    key = taskrefs.key_of(task)
    if task["status"] not in OPEN_STATUSES or task["project_status"] not in PROJECTED_STATUSES:
        return "%s is not open work in a project that is being worked on" % key
    if ctx.conn.execute("SELECT 1 FROM tasks WHERE parent_id = ?", (task["id"],)).fetchone():
        return "%s is a parent task: reassign one of its subtasks instead" % key
    user = users.find_by_username(ctx.conn, name.lower()) if name else None
    if user is None or not user["active"]:
        return "unknown or inactive user %r (use KEY-1:username, e.g. WEB-3:carol)" % name
    if not projects.is_member(ctx.conn, task["project_id"], user["id"]):
        return "%s is not a member of project %s" % (user["username"], task["project_key"])
    if task["id"] in done:
        return "%s is listed twice" % key
    return task, user


def parse_what_if(ctx, params) -> dict:
    """``capacity=bob:50,carol:80`` (percent of normal availability), ``overrun=20`` (percent extra effort on every task) and
    ``reassign=WEB-3:carol`` (somebody else does that task)."""
    errors, capacity, names = {}, {}, {}
    raw = (params.get("capacity") or "").strip()
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        name, _, percent = item.partition(":")
        user = users.find_by_username(ctx.conn, name.strip().lower()) if name.strip() else None
        if user is None or not user["active"]:
            errors["capacity"] = "unknown or inactive user %r (use username:percent, e.g. bob:50)" % name.strip()
        elif not NUMBER_RE.match(percent.strip()) or not 0 <= Fraction(percent.strip()) <= 200:
            errors["capacity"] = "the percentage for %s must be a number from 0 to 200" % user["username"]
        elif user["id"] in capacity:
            errors["capacity"] = "%s is listed twice" % user["username"]
        else:
            capacity[user["id"]] = Fraction(percent.strip()) / 100
            names[user["username"]] = float(Fraction(percent.strip()))
    overrun = Fraction(0)
    text = (params.get("overrun") or "").strip()
    if text:
        if not NUMBER_RE.match(text) or not 0 <= Fraction(text) <= 300:
            errors["overrun"] = "must be a percentage from 0 to 300"
        else:
            overrun = Fraction(text)
    reassign, moved = {}, []
    for item in [x.strip() for x in (params.get("reassign") or "").split(",") if x.strip()]:
        found = _reassignment(ctx, item, reassign)
        if isinstance(found, str):
            errors["reassign"] = found
        else:
            task, user = found
            reassign[task["id"]] = user["id"]
            moved.append({"task": taskrefs.key_of(task), "to": user["username"], "to_name": user["display_name"]})
    if errors:
        raise ValidationError(errors)
    return {"capacity": capacity, "capacity_names": names, "overrun": overrun, "reassign": reassign, "reassign_info": sorted(moved, key=lambda m: parse_task_key(m["task"]))}


NO_WHAT_IF = {"capacity": {}, "capacity_names": {}, "overrun": Fraction(0), "reassign": {}, "reassign_info": []}


# ------------------------------------------------------------------------------------------------ the plan


class Plan:
    """The schedule of every open leaf task, plus what is needed to describe it."""

    def __init__(self):
        self.base = None
        self.today = None
        self.projects = {}  # project id -> row (every project)
        self.tasks = {}  # task id -> row (open leaf tasks that were scheduled)
        self.jobs = {}
        self.placements = {}
        self.people = {}  # user id -> row (active users)
        self.rates = {}
        self.notes = defaultdict(list)  # task id -> notes about the assumptions made for it
        self.what_if = NO_WHAT_IF

    def key(self, tid) -> str:
        row = self.tasks[tid]
        return "%s-%d" % (row["project_key"], row["number"])

    def finish_date(self, tid):
        p = self.placements[tid]
        return scheduler.date_of_time(self.base, p.finish) if p.status == "scheduled" else None

    def start_date(self, tid):
        p = self.placements[tid]
        return scheduler.start_date_of_time(self.base, p.start) if p.status == "scheduled" else None


def _finish_to_start_only(conn) -> str:
    """The SQL that keeps only the links that delay the *start* of work.

    Where dependencies have a type (start-to-start, finish-to-finish: they gate status changes and flag dates), only the
    finish-to-start links say "cannot begin until that is finished" - the same links the critical path follows. Without
    link types every link is a finish-to-start link.
    """
    typed = any(column[1] == "type" for column in conn.execute("PRAGMA table_info(task_dependencies)"))
    return " WHERE d.type = 'finish_to_start'" if typed else ""


def compute(ctx, what_if=NO_WHAT_IF) -> Plan:
    conn = ctx.conn
    plan = Plan()
    plan.what_if = what_if
    plan.today = ctx.today
    plan.base = scheduler.first_workday(ctx.today)
    plan.projects = {r["id"]: r for r in conn.execute("SELECT * FROM projects")}
    in_work = "p.status IN ('planning', 'active')"
    rows = conn.execute(
        "SELECT t.id, t.project_id, t.number, t.title, t.status, t.priority, t.due_date, t.start_date, t.assignee_id, t.estimate_hours, "
        "t.parent_id, t.milestone_id, p.key AS project_key, "
        "(SELECT COALESCE(SUM(te.hours), 0) FROM time_entries te WHERE te.task_id = t.id) AS logged, "
        "EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id) AS is_container "
        "FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.status IN " + OPEN_SQL + " AND " + in_work
    ).fetchall()
    open_rows = {r["id"]: r for r in rows}
    leaves = {tid: r for tid, r in open_rows.items() if not r["is_container"]}
    if len(leaves) > MAX_TASKS:
        raise Conflict(
            "There are %d open tasks; a projection handles at most %d. Archive or complete finished projects first." % (len(leaves), MAX_TASKS),
            code="projection_too_large",
        )
    plan.people = {u["id"]: u for u in conn.execute("SELECT * FROM users WHERE active = 1")}
    for uid, user in plan.people.items():
        factor = what_if["capacity"].get(uid, Fraction(1))
        plan.rates[uid] = _frac(user["weekly_capacity_hours"]) * factor / 5

    children = defaultdict(list)
    for r in rows:
        if r["parent_id"] in open_rows:
            children[r["parent_id"]].append(r["id"])
    open_blockers = defaultdict(list)
    for d in conn.execute("SELECT d.blocker_id, d.blocked_id FROM task_dependencies d%s ORDER BY d.blocker_id, d.blocked_id" % _finish_to_start_only(conn)):
        if d[0] in open_rows and d[1] in open_rows:
            open_blockers[d[1]].append(d[0])

    def leaves_under(tid):
        found, stack = set(), list(children.get(tid, ()))
        while stack:
            cur = stack.pop()
            if cur in leaves:
                found.add(cur)
            stack.extend(children.get(cur, ()))
        return found

    memo: dict = {}

    def resolve(root):
        """The jobs that must finish for task ``root`` to count as finished: itself, or - for a parent - its open subtasks and whatever the parent waits for.

        Walks the chain of parents that wait for parents without recursion, so a very long chain cannot exhaust the interpreter's stack.
        """
        todo, walking = [(root, False)], set()
        while todo:
            tid, back = todo.pop()
            if tid in memo:
                continue
            if tid in leaves:
                memo[tid] = frozenset((tid,))
            elif not back:
                if tid not in walking:  # a loop of dependencies cannot exist (the API refuses them); if one did, it is ignored
                    walking.add(tid)
                    todo.append((tid, True))
                    todo.extend((blocker, False) for blocker in open_blockers.get(tid, ()))
            else:
                found = set(leaves_under(tid))
                for blocker in open_blockers.get(tid, ()):
                    found |= memo.get(blocker, frozenset())
                memo[tid] = frozenset(found)
                walking.discard(tid)
        return memo[root]

    default_hours = _frac(ctx.config.default_task_hours)
    overrun = Fraction(1) + what_if["overrun"] / 100
    for tid, r in leaves.items():
        notes = plan.notes[tid]
        if r["estimate_hours"] is None:
            effort = default_hours
            notes.append("No estimate: assumed %s hours" % ("%g" % float(default_hours)))
        else:
            effort = max(_frac(r["estimate_hours"]) - _frac(r["logged"]), Fraction(0))
            if effort == 0 and r["estimate_hours"] > 0:
                notes.append("Already at or over its estimate: only closing it is left")
        person = r["assignee_id"]
        if tid in what_if["reassign"]:
            person = what_if["reassign"][tid]
            notes.append("Given to %s in this what-if" % plan.people[person]["display_name"])
        elif person is not None and person not in plan.people:
            notes.append("The assignee is inactive: treated as unassigned")
            person = None
        if person is None and r["assignee_id"] is None:
            notes.append("Unassigned: assumed to proceed at %d hours a day without queueing" % UNASSIGNED_HOURS_PER_DAY)
        if r["status"] == "blocked":
            notes.append("Flagged as blocked: this date assumes the blocker clears now")
        blockers = set()
        for b in open_blockers.get(tid, ()):
            blockers |= resolve(b)
        blockers.discard(tid)
        plan.jobs[tid] = scheduler.Job(
            tid, effort * overrun, person, frozenset(blockers),
            scheduler.workday_index(plan.base, parse_date(r["start_date"])) if r["start_date"] else 0,
            (-PRIORITY_RANK[r["priority"]], r["due_date"] or FAR_FUTURE, r["project_key"], r["number"]),
        )  # fmt: skip
        plan.tasks[tid] = r
    plan.placements = scheduler.schedule(plan.jobs, plan.rates, Fraction(UNASSIGNED_HOURS_PER_DAY), HORIZON_DAYS)
    return plan


# ------------------------------------------------------------------------------------------- describing it


def _who(plan, tid):
    person = plan.jobs[tid].person  # the assignee, unless this is a what-if that gives the task to somebody else
    return plan.people[person]["display_name"] if person in plan.people else None


def _root_cause(plan, tid):
    """Follow "it waited for ..." links back to the first job that did not simply wait for another job."""
    seen = set()
    while tid not in seen:
        seen.add(tid)
        why = plan.placements[tid].why
        if why is None or why["kind"] != "blocker":
            return tid
        tid = why["job"]
    return tid


def _pace(plan, tid):
    person = plan.jobs[tid].person
    return plan.rates[person] if person is not None else Fraction(UNASSIGNED_HOURS_PER_DAY)


def explain(plan, tid) -> str:
    """One or two plain sentences: why this task starts when it does and, if it is late, what makes it so."""
    p = plan.placements[tid]
    r = plan.tasks[tid]
    person = _who(plan, tid)
    if p.status == "no_capacity":
        return "%s has no capacity (0 hours a week), so this cannot be scheduled." % (person or "Its assignee")
    if p.status == "waiting_for_unschedulable":
        return "Waits for %s, which cannot be scheduled." % plan.key(p.waits_for)
    if p.status == "beyond_horizon":
        return "Would not finish within %d working days." % HORIZON_DAYS
    why = p.why
    due, finish = parse_date(r["due_date"]), plan.finish_date(tid)
    late = bool(due and finish and finish > due)
    if why["kind"] == "immediately":
        text = "Starts right away: %s is free and nothing blocks it." % person if person else "Starts right away; with nobody assigned this assumes someone is free."
        if late:  # nothing delays it: the work itself runs past the due date
            text += " The work itself (%g hours at %g a day) runs past its due date." % (float(plan.jobs[tid].effort), float(_pace(plan, tid))) if due >= plan.today else " It is already overdue."
        return text
    if why["kind"] == "start_date":
        return "Its start date (%s) holds it back." % fmt_date(parse_date(r["start_date"]), plan.today)
    other = why["job"]
    if why["kind"] == "queue":
        return "%s is busy with %s (%s) until %s, so this starts after it." % (
            person or "Its assignee", plan.key(other), plan.tasks[other]["title"], fmt_date(plan.finish_date(other), plan.today),
        )  # fmt: skip
    text = "Waits for %s (%s), which finishes %s." % (plan.key(other), plan.tasks[other]["title"], fmt_date(plan.finish_date(other), plan.today))
    root = _root_cause(plan, other)
    root_why = plan.placements[root].why
    if root_why and root_why["kind"] == "queue":
        queued = root_why["job"]
        text += " The delay starts in %s's queue: busy with %s until %s." % (
            _who(plan, root) or "its assignee", plan.key(queued), fmt_date(plan.finish_date(queued), plan.today),
        )  # fmt: skip
    elif root_why and root_why["kind"] == "start_date":
        text += " The delay starts with %s, which cannot begin before its start date (%s)." % (plan.key(root), fmt_date(parse_date(plan.tasks[root]["start_date"]), plan.today))
    return text


def _why(plan, tid):
    why = plan.placements[tid].why
    if why is None:
        return None
    out = {"kind": why["kind"]}
    if why["job"] is not None:
        out["task"] = plan.key(why["job"])
        out["until"] = scheduler.date_of_time(plan.base, why["at"]).isoformat()
    return out


def _task_entry(plan, tid, today) -> dict:
    r, job, p = plan.tasks[tid], plan.jobs[tid], plan.placements[tid]
    finish, start = plan.finish_date(tid), plan.start_date(tid)
    due = parse_date(r["due_date"])
    margin = (due - finish).days if due and finish else None
    return {
        "key": plan.key(tid), "title": r["title"], "status": r["status"], "priority": r["priority"],
        "assignee": plan.people[job.person]["username"] if job.person in plan.people else None,
        "due_date": r["due_date"], "overdue": bool(due and due < today),
        "remaining_hours": round(float(job.effort), 2), "state": p.status,
        "projected_start": start.isoformat() if start else None, "projected_finish": finish.isoformat() if finish else None,
        "margin_days": margin, "late_by_days": max(-margin, 0) if margin is not None else None,
        "why": _why(plan, tid), "explanation": explain(plan, tid), "notes": plan.notes[tid],
    }  # fmt: skip


def _headline(state, finish, due, late_days, counts, reason, today):
    if state == "not_projected":
        return reason
    if state == "complete":
        return "No open work: there is nothing to project."
    if counts["unprojectable"]:
        return "Cannot be projected completely: %s cannot be scheduled (see the tasks below)." % plural(counts["unprojectable"], "task")
    text = "Projected to finish %s" % fmt_date(finish, today)
    if not due:
        text += ". The project has no due date, so there is nothing to compare it with."
    elif late_days > 0:
        text += ", %s after the due date (%s)." % (plural(late_days, "day"), fmt_date(due, today))
    else:
        margin = (due - finish).days
        text += ", on the due date (%s)." % fmt_date(due, today) if margin == 0 else ", %s before the due date (%s)." % (plural(margin, "day"), fmt_date(due, today))
    late = counts["late_tasks"]
    if late:  # a project can finish in time while some of its tasks still miss their own dates
        text += " %d of %s will miss %s own due date%s." % (late, plural(counts["open_tasks"], "open task"), "its" if late == 1 else "their", "" if late == 1 else "s")
    return text


def project_view(ctx, plan, row, baseline=None) -> dict:
    """Everything the projection knows about one project."""
    today = ctx.today
    due = parse_date(row["due_date"])
    out = {
        "project": row["key"], "name": row["name"], "as_of": today.isoformat(), "status": row["status"], "state": "projected", "reason": None,
        "finish": None, "due_date": row["due_date"], "late_by_days": None, "verdict": "unknown", "headline": "",
        "counts": {"open_tasks": 0, "late_tasks": 0, "unprojectable": 0, "overdue_tasks": 0},
        "tasks": [], "tasks_truncated": False, "milestones": [], "people": [], "scenario": None,
    }  # fmt: skip
    if row["status"] not in PROJECTED_STATUSES:
        out["state"] = "not_projected"
        out["reason"] = {"on_hold": "Not projected: the project is on hold.", "completed": "Not projected: the project is completed.",
                         "archived": "Not projected: the project is archived."}[row["status"]]  # fmt: skip
        out["headline"] = out["reason"]
        return out
    ids = sorted((tid for tid, r in plan.tasks.items() if r["project_id"] == row["id"]), key=lambda t: plan.tasks[t]["number"])
    entries = {tid: _task_entry(plan, tid, today) for tid in ids}
    counts = out["counts"]
    counts["open_tasks"] = len(ids)
    counts["unprojectable"] = sum(1 for e in entries.values() if e["state"] != "scheduled")
    counts["late_tasks"] = sum(1 for e in entries.values() if e["late_by_days"])
    counts["overdue_tasks"] = sum(1 for e in entries.values() if e["overdue"])
    finishes = [plan.finish_date(t) for t in ids if plan.finish_date(t)]
    if not ids:
        out["state"] = "complete"
        out["verdict"] = "on_track"
    else:
        out["finish"] = max(finishes).isoformat() if finishes else None
        if counts["unprojectable"] or not finishes:
            out["verdict"] = "unknown"
        elif not due:
            out["verdict"] = "no_due_date"
        else:
            out["late_by_days"] = max((max(finishes) - due).days, 0)
            out["verdict"] = "will_miss" if out["late_by_days"] else "on_track"
    out["headline"] = _headline(out["state"], max(finishes) if finishes else None, due, out["late_by_days"] or 0, counts, out["reason"], today)
    listed = sorted(entries.values(), key=lambda e: (-(e["late_by_days"] or 0), e["state"] != "scheduled", e["projected_finish"] or FAR_FUTURE, e["key"]))
    out["tasks"], out["tasks_truncated"] = listed[:MAX_LISTED], len(listed) > MAX_LISTED

    for ms in ctx.conn.execute("SELECT id, name, due_date FROM milestones WHERE project_id = ? ORDER BY due_date, id", (row["id"],)):
        mine = [t for t in ids if plan.tasks[t]["milestone_id"] == ms["id"]]
        done = [plan.finish_date(t) for t in mine if plan.finish_date(t)]
        ms_due = parse_date(ms["due_date"])
        finish = max(done) if done and len(done) == len(mine) else None
        out["milestones"].append({
            "id": ms["id"], "name": ms["name"], "due_date": ms["due_date"], "open_tasks": len(mine),
            "projected_finish": finish.isoformat() if finish else None,
            "late_by_days": max((finish - ms_due).days, 0) if finish and ms_due else None,
        })  # fmt: skip

    per_person = defaultdict(lambda: {"here": 0.0, "late": 0})
    for tid in ids:
        uid = plan.jobs[tid].person
        if uid is not None:
            per_person[uid]["here"] += float(plan.jobs[tid].effort)
            per_person[uid]["late"] += 1 if entries[tid]["late_by_days"] else 0
    for uid in per_person:
        everything = [t for t, j in plan.jobs.items() if j.person == uid]
        ends = [plan.finish_date(t) for t in everything if plan.finish_date(t)]
        user = plan.people[uid]
        out["people"].append({
            "username": user["username"], "display_name": user["display_name"],
            "hours_per_day": round(float(plan.rates[uid]), 2),
            "capacity_pct": plan.what_if["capacity_names"].get(user["username"], 100.0),
            "open_hours": round(sum(float(plan.jobs[t].effort) for t in everything), 2),
            "open_hours_here": round(per_person[uid]["here"], 2), "late_tasks": per_person[uid]["late"],
            "busy_until": max(ends).isoformat() if ends else None,
        })  # fmt: skip
    out["people"].sort(key=lambda p: (-p["late_tasks"], p["busy_until"] or "", p["username"]))
    if baseline is not None:
        out["scenario"] = _scenario(plan, baseline, out)
    return out


def _scenario(plan, baseline, view):
    old = baseline
    moved = (parse_date(view["finish"]) - parse_date(old["finish"])).days if view["finish"] and old["finish"] else None
    return {
        "capacity": plan.what_if["capacity_names"], "overrun": float(plan.what_if["overrun"]), "reassign": plan.what_if["reassign_info"],
        "baseline": {"finish": old["finish"], "late_by_days": old["late_by_days"], "late_tasks": old["counts"]["late_tasks"], "verdict": old["verdict"]},
        "finish_moves_by_days": moved, "extra_late_tasks": view["counts"]["late_tasks"] - old["counts"]["late_tasks"],
    }  # fmt: skip


# ------------------------------------------------------------------------------------------- entry points


def project_projection(ctx, key, params) -> dict:
    row = projects.get_row(ctx.conn, key)
    what_if = parse_what_if(ctx, params)
    plan = compute(ctx, what_if)
    scenario = bool(what_if["capacity"] or what_if["overrun"] or what_if["reassign"])
    baseline = project_view(ctx, compute(ctx), row) if scenario else None
    out = project_view(ctx, plan, row, baseline)
    out["assumptions"] = assumptions(ctx)
    return out


def project_advice(ctx, key, params) -> dict:
    """The HTTP form of :func:`advise`: ``?limit=1..10`` suggestions (default 3)."""
    text = (params.get("limit") or "3").strip()
    if not text.isdigit() or not 1 <= int(text) <= 10:
        raise ValidationError({"limit": "must be a whole number from 1 to 10"})
    return advise(ctx, key, int(text))


def assumptions(ctx) -> dict:
    return {
        "work_days": "Monday to Friday; there is no holiday calendar",
        "starts": "At the start of the first working day on or after today",
        "default_task_hours": float(ctx.config.default_task_hours),
        "unassigned_hours_per_day": UNASSIGNED_HOURS_PER_DAY,
        "horizon_work_days": HORIZON_DAYS,
        "notes": [
            "Each person works their weekly capacity spread over five work days, on one task at a time, "
            "taking the highest priority first and then the earliest due date; nobody is pulled off a task they have started.",
            "Remaining effort is the estimate minus the time logged; tasks without an estimate count the default hours.",
            "Tasks wait for the tasks they are blocked by; a parent counts as finished when its open subtasks and its own blockers are.",
            "Every project that is planning or active competes for the same people; projects on hold are left out.",
            "Estimates are taken at face value. Use the what-if options to see how a slower pace or a smaller team would change the picture.",
        ],
    }


def portfolio_projection(ctx, params) -> dict:
    what_if = parse_what_if(ctx, params)
    plan = compute(ctx, what_if)
    scenario = bool(what_if["capacity"] or what_if["overrun"] or what_if["reassign"])
    base_plan = compute(ctx) if scenario else None
    items = []
    for row in sorted((r for r in plan.projects.values() if r["status"] != "archived"), key=lambda r: r["key"]):
        view = project_view(ctx, plan, row)
        item = {k: view[k] for k in ("project", "name", "status", "state", "finish", "due_date", "late_by_days", "verdict", "headline")}
        item["open_tasks"], item["late_tasks"], item["unprojectable"] = (view["counts"][k] for k in ("open_tasks", "late_tasks", "unprojectable"))
        if base_plan is not None:
            before = project_view(ctx, base_plan, row)
            item["baseline_finish"], item["baseline_late_tasks"] = before["finish"], before["counts"]["late_tasks"]
        items.append(item)
    items.sort(key=lambda i: (VERDICT_RANK.get(i["verdict"], 4) if i["state"] == "projected" else 5, -(i["late_by_days"] or 0), i["project"]))
    return {
        "as_of": ctx.today.isoformat(), "projects": items,
        "totals": {
            "projected": sum(1 for i in items if i["state"] == "projected"), "will_miss": sum(1 for i in items if i["verdict"] == "will_miss" and i["state"] == "projected"),
            "late_tasks": sum(i["late_tasks"] for i in items),
        },  # fmt: skip
        "scenario": {"capacity": what_if["capacity_names"], "overrun": float(what_if["overrun"]), "reassign": what_if["reassign_info"]} if scenario else None,
        "assumptions": assumptions(ctx),
    }


# ------------------------------------------------------------------------------------------------ advice


class _Standing:
    """How one schedule looks for one project: the score the advice tries to improve, and what it must not make worse elsewhere."""

    def __init__(self, plan, placements, project_id, project_due, dues):
        finishes, self.late_tasks, self.unprojectable, self.other_late, self.other_unprojectable = [], 0, 0, 0, 0
        for tid, row in plan.tasks.items():
            mine = row["project_id"] == project_id
            placement = placements[tid]
            if placement.status != "scheduled":
                if mine:
                    self.unprojectable += 1
                else:
                    self.other_unprojectable += 1
                continue
            finish = scheduler.date_of_time(plan.base, placement.finish)
            if mine:
                finishes.append(finish)
            if dues[tid] and finish > dues[tid]:
                if mine:
                    self.late_tasks += 1
                else:
                    self.other_late += 1
        self.finish = max(finishes) if finishes else None
        self.due = project_due
        self.late_days = max((self.finish - project_due).days, 0) if self.finish and project_due else 0
        # smaller is better: tasks that cannot be scheduled, then days late, then late tasks, then how soon it is all done
        self.score = (self.unprojectable, self.late_days, self.late_tasks, self.finish.toordinal() if self.finish else 0)

    def snapshot(self) -> dict:
        """The same four numbers the projection of a project reports (``late_by_days`` is null while a task cannot be scheduled: the verdict is unknown)."""
        known = self.finish and self.due and not self.unprojectable
        return {"finish": self.finish.isoformat() if self.finish else None, "late_by_days": self.late_days if known else None,
                "late_tasks": self.late_tasks, "unprojectable": self.unprojectable}  # fmt: skip

    def sentence(self, today, would=False) -> str:
        """``finishes Tue 24 Nov, 4 days after the due date, and 4 tasks are late`` - or, for a schedule that might be, ``would finish ... would be late``."""
        if self.finish:
            first = "%s %s" % ("would finish" if would else "finishes", fmt_date(self.finish, today))
            if self.due:
                margin = (self.due - self.finish).days
                first += ", on the due date" if margin == 0 else ", %s %s the due date" % (plural(abs(margin), "day"), "before" if margin > 0 else "after")
            parts = [first]
            if not self.late_tasks:
                parts.append("no task would be late" if would else "no task is late")
        else:
            parts = ["could not be projected" if would else "cannot be projected"]
        if self.late_tasks:
            parts.append("%s %s late" % (plural(self.late_tasks, "task"), "would be" if would else ("is" if self.late_tasks == 1 else "are")))
        if self.unprojectable:
            parts.append("%s %s be scheduled" % (plural(self.unprojectable, "task"), "could not" if would else "cannot"))
        return parts[0] + (", and " + parts[1] if len(parts) == 2 else (", " + ", ".join(parts[1:-1]) + " and " + parts[-1] if len(parts) > 2 else ""))


def _components(plan):
    """Which jobs can influence each other: those that wait for one another or share a person. Two jobs in different groups never do."""
    parent = {tid: tid for tid in plan.jobs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    first_of = {}
    for tid, job in plan.jobs.items():
        for blocker in job.blockers:
            if blocker in parent:
                union(tid, blocker)
        if job.person in plan.rates:
            union(tid, first_of.setdefault(job.person, tid))
    groups = defaultdict(list)
    for tid in plan.jobs:
        groups[find(tid)].append(tid)
    return find, groups, first_of


def _critical_tasks(plan, project_id, before, dues) -> set:
    """The tasks of the project whose reassignment could make anything earlier.

    That is what the late tasks, the tasks that end a late project and the tasks that cannot be scheduled had to wait for: follow
    "it waited for ..." back from each of them. Moving any other task cannot help, whoever takes it, so it is not tried.
    """
    todo = []
    for tid, row in plan.tasks.items():
        if row["project_id"] != project_id:
            continue
        placement = plan.placements[tid]
        if placement.status != "scheduled":
            todo.append(tid)
            continue
        finish = scheduler.date_of_time(plan.base, placement.finish)
        if (dues[tid] and finish > dues[tid]) or (before.late_days and finish == before.finish):
            todo.append(tid)
    seen = set()
    while todo:
        tid = todo.pop()
        if tid in seen:
            continue
        seen.add(tid)
        why = plan.placements[tid].why  # none for work that cannot be scheduled: it is a starting point already
        if why and why["kind"] in ("queue", "blocker"):
            todo.append(why["job"])
    return {tid for tid in seen if plan.tasks[tid]["project_id"] == project_id and plan.jobs[tid].person is not None}


def advise(ctx, project_key, limit=3, plan=None) -> dict:
    """Which single reassignment would help a project that is going to slip most?

    The tasks of the project that somebody is assigned to and that the late work had to wait for (see ``_critical_tasks``) are each
    tried with every other member of the project who has capacity: the schedule is played again and compared. A suggestion has to
    improve the project - fewer tasks that cannot be scheduled, or fewer days late, or fewer late tasks - without making a task in
    another project late or unschedulable; among those that do, the one that also finishes everything soonest comes first. At most
    ``MAX_TRIALS`` reassignments are tried, and fewer when each means scheduling a great many tasks again (``WORK_BUDGET``), starting
    with the work that is late or cannot be scheduled. Only the people and tasks that a reassignment can affect are scheduled again,
    so a trial costs far less than a projection.
    """
    row = projects.get_row(ctx.conn, project_key)
    plan = plan or compute(ctx)
    out = {"project": row["key"], "as_of": ctx.today.isoformat(), "suggestions": [], "tried": 0, "limited": False, "reason": None}
    if row["status"] not in PROJECTED_STATUSES:
        out["reason"] = "Not projected: the project is %s." % row["status"].replace("_", " ")
        return out
    dues = {tid: parse_date(r["due_date"]) for tid, r in plan.tasks.items()}
    due = parse_date(row["due_date"])
    before = _Standing(plan, plan.placements, row["id"], due, dues)
    if before.score[:3] == (0, 0, 0):
        out["reason"] = "Nothing is projected to be late."
        return out
    busy = defaultdict(lambda: Fraction(0))  # when each person is done with everything they have, as the plan stands
    for tid, job in plan.jobs.items():
        if job.person is not None and plan.placements[tid].status == "scheduled":
            busy[job.person] = max(busy[job.person], plan.placements[tid].finish)
    members = [r[0] for r in ctx.conn.execute("SELECT user_id FROM project_members WHERE project_id = ?", (row["id"],))]
    receivers = sorted((uid for uid in members if uid in plan.people and plan.rates[uid] > 0), key=lambda uid: (busy[uid], plan.people[uid]["username"]))
    find, groups, first_of = _components(plan)
    candidates = _critical_tasks(plan, row["id"], before, dues)

    def urgency(tid):  # whose work to move first when there is no time to try everything
        placement = plan.placements[tid]
        late = bool(dues[tid] and placement.status == "scheduled" and scheduler.date_of_time(plan.base, placement.finish) > dues[tid])
        return (placement.status == "scheduled", not late, -plan.jobs[tid].effort, plan.key(tid))

    best, tried, work, stop = {}, 0, 0, False
    for tid in sorted(candidates, key=urgency):
        for uid in receivers:
            if uid == plan.jobs[tid].person:
                continue
            affected = {find(tid)} | ({find(first_of[uid])} if uid in first_of else set())  # the groups of the task and of the person who takes it
            jobs = {jid: plan.jobs[jid] for group in affected for jid in groups[group]}
            if tried >= MAX_TRIALS or work + len(jobs) > WORK_BUDGET:
                stop = True
                break
            tried, work = tried + 1, work + len(jobs)
            jobs[tid] = replace(jobs[tid], person=uid)
            placements = dict(plan.placements)
            placements.update(scheduler.schedule(jobs, plan.rates, Fraction(UNASSIGNED_HOURS_PER_DAY), HORIZON_DAYS))
            after = _Standing(plan, placements, row["id"], due, dues)
            if after.score[:3] < before.score[:3] and after.other_late <= before.other_late and after.other_unprojectable <= before.other_unprojectable:
                candidate = (after.score, plan.people[uid]["username"], uid, after)
                if tid not in best or candidate[:2] < best[tid][:2]:
                    best[tid] = candidate
        if stop:
            break
    out["tried"], out["limited"] = tried, stop
    hours = defaultdict(lambda: Fraction(0))
    for job in plan.jobs.values():
        if job.person is not None:
            hours[job.person] += job.effort
    for tid, (_, username, uid, after) in sorted(best.items(), key=lambda kv: (kv[1][0], plan.key(kv[0])))[:limit]:
        old, new, key = plan.people[plan.jobs[tid].person], plan.people[uid], plan.key(tid)
        out["suggestions"].append({
            "task": key, "title": plan.tasks[tid]["title"],
            "from": {"username": old["username"], "display_name": old["display_name"]},
            "to": {"username": username, "display_name": new["display_name"], "open_hours": round(float(hours[uid]), 2),
                   "busy_until": scheduler.date_of_time(plan.base, busy[uid]).isoformat() if busy[uid] else None},
            "before": before.snapshot(), "after": after.snapshot(),
            "summary": "Give %s (%s) to %s instead of %s: the project %s; now it %s." % (
                key, plan.tasks[tid]["title"], new["display_name"], old["display_name"], after.sentence(ctx.today, would=True), before.sentence(ctx.today)),
            "reassign": "%s:%s" % (key, username),
        })  # fmt: skip
    if not out["suggestions"]:
        out["reason"] = ("There are too many open tasks to try reassignments." if stop and not tried
                         else "No single reassignment to another member of the project would help." + (" Only some were tried." if stop else ""))  # fmt: skip
    return out


def early_warning(ctx, project_key=None, limit=5) -> dict:
    """What a person wants to be told on Monday morning: the projects (and tasks) that are going to slip, and why.

    Without ``project_key`` it covers every project that is not archived and mentions only those with something to
    report; with a key that project is always included.
    """
    wanted = projects.get_row(ctx.conn, project_key) if project_key else None
    plan = compute(ctx)
    flagged, calm, late_total = [], [], 0
    for row in sorted((r for r in plan.projects.values() if r["status"] != "archived"), key=lambda r: r["key"]):
        if wanted is not None and row["id"] != wanted["id"]:
            continue
        view = project_view(ctx, plan, row)
        trouble = [t for t in view["tasks"] if t["late_by_days"] or t["state"] != "scheduled"]
        late_total += view["counts"]["late_tasks"]
        if wanted is None and view["state"] != "projected":
            continue  # projects nobody is working on (on hold, finished) have nothing to warn about
        if wanted is None and not trouble and view["verdict"] not in ("will_miss", "unknown"):
            calm.append({"project": view["project"], "name": view["name"], "finish": view["finish"]})
            continue
        flagged.append({
            "project": view["project"], "name": view["name"], "state": view["state"], "verdict": view["verdict"], "headline": view["headline"],
            "finish": view["finish"], "due_date": view["due_date"], "late_by_days": view["late_by_days"],
            "late_tasks": view["counts"]["late_tasks"], "unprojectable": view["counts"]["unprojectable"],
            "tasks": [{k: t[k] for k in ("key", "title", "assignee", "due_date", "projected_finish", "late_by_days", "state", "explanation")} for t in trouble[:limit]],
            "more": max(len(trouble) - limit, 0), "advice": None,
        })  # fmt: skip
    flagged.sort(key=lambda p: (VERDICT_RANK.get(p["verdict"], 4) if p["state"] == "projected" else 5, -(p["late_by_days"] or 0), p["project"]))
    for entry in flagged[:MAX_ADVISED]:  # the worst first; every piece of advice plays the schedule forward again, so only a few are asked for
        if entry["state"] == "projected" and (entry["late_tasks"] or entry["verdict"] in ("will_miss", "unknown")):
            found = advise(ctx, entry["project"], limit=1, plan=plan)["suggestions"]
            entry["advice"] = {k: found[0][k] for k in ("task", "summary", "reassign")} if found else None
    return {"as_of": ctx.today.isoformat(), "projects": flagged, "on_track": calm, "totals": {"flagged": len(flagged), "late_tasks": late_total}}
