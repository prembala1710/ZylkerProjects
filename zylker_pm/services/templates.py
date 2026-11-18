"""Project templates: save what a project looks like, then start new projects from it.

A template keeps the *shape* of a project: the milestones, the task tree (titles, descriptions, types, priorities,
estimates, labels), the dependencies between tasks, who does what - as named *slots* such as "designer" that are
filled with real people when a project is started - and every date as a number of days from an *anchor* day. Starting a
project from a template means choosing a start day, and every date moves with it.

Design rules worth knowing:

* Only open work is captured unless ``include_closed`` is set; every captured task starts as ``todo``. A task that is
  left out takes its dependencies with it, and its subtasks move up to the nearest task that is kept.
* Nothing is half-built: a request that cannot be fully satisfied (a taken key, an unknown person, a graph that would
  contain a loop) changes nothing, and ``preview`` runs exactly the same checks and computations without writing.
* A template is a snapshot. Later changes to the project it came from do not reach it.
"""

from __future__ import annotations

import json
from datetime import timedelta

from ..domain import CLOSED_STATUSES
from ..errors import Conflict, NotFound, ValidationError
from ..util import PROJECT_KEY_RE, Checker, parse_date
from . import activity, projects, tasks

MAX_TEMPLATES = 100
MAX_TASKS = 500
MAX_MILESTONES = 50
MAX_OFFSET_DAYS = 3650
MAX_DOCUMENT_BYTES = 2_000_000
MAX_SLOT_NAME = 40
FORMAT = 1


def _get_row(ctx, template_id):
    row = ctx.conn.execute("SELECT * FROM project_templates WHERE id = ?", (template_id,)).fetchone()
    if row is None:
        raise NotFound("Template not found", code="template_not_found")
    return row


def _summary(row):
    return {
        "id": row["id"], "name": row["name"], "description": row["description"], "source_project": row["source_project"],
        "anchor_date": row["anchor_date"], "tasks": row["task_count"], "milestones": row["milestone_count"],
        "dependencies": row["dependency_count"], "times_used": row["times_used"], "last_used_at": row["last_used_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }  # fmt: skip


def to_dict(row):
    data = json.loads(row["data"])
    return {**_summary(row), "slots": data["slots"], "project": data["project"], "milestone_list": data["milestones"], "task_list": data["tasks"], "dependency_list": data["dependencies"]}


def list_templates(ctx):
    rows = ctx.conn.execute("SELECT * FROM project_templates ORDER BY name COLLATE NOCASE").fetchall()
    return [_summary(r) for r in rows]


def get_template(ctx, template_id):
    return to_dict(_get_row(ctx, template_id))


def _name_taken(ctx, name, ignore_id=None):
    if ctx.conn.execute("SELECT 1 FROM project_templates WHERE name = ? AND id IS NOT ?", (name, ignore_id)).fetchone():
        raise Conflict("A template named %r already exists" % name, code="template_name_taken")


# ------------------------------------------------------------------------------ saving


def _offset(day, anchor, field):
    if day is None:
        return None
    n = (parse_date(day) - anchor).days
    if abs(n) > MAX_OFFSET_DAYS:
        raise ValidationError({"anchor_date": "%s is more than %d days from the anchor day" % (field, MAX_OFFSET_DAYS)})
    return n


def _acyclic(parents, deps):
    """No loop in 'blocker before blocked' plus 'child before parent'. Nodes are template refs."""
    out, indegree = {}, {}
    for a, b in list(deps) + [(child, parent) for child, parent in parents.items() if parent]:
        out.setdefault(a, []).append(b)
        indegree[b] = indegree.get(b, 0) + 1
        indegree.setdefault(a, 0)
    ready = [n for n, d in indegree.items() if d == 0]
    seen = 0
    while ready:
        n = ready.pop()
        seen += 1
        for m in out.get(n, ()):
            indegree[m] -= 1
            if indegree[m] == 0:
                ready.append(m)
    return seen == len(indegree)


def save_from_project(ctx, project_key, data):
    project = projects.get_row(ctx.conn, project_key)
    c = Checker(data, {"name", "description", "anchor_date", "include_closed", "slots"})
    c.text("name", required=True, min_len=1, max_len=80)
    c.text("description", max_len=2000)
    c.date("anchor_date", nullable=False)
    c.boolean("include_closed")
    c.raw("slots")
    v = c.result()
    conn = ctx.conn
    if conn.execute("SELECT COUNT(*) FROM project_templates").fetchone()[0] >= MAX_TEMPLATES:
        raise Conflict("At most %d templates. Delete one first." % MAX_TEMPLATES, code="template_limit")
    _name_taken(ctx, v["name"])

    rows = conn.execute(
        "SELECT t.*, u.username, u.display_name FROM tasks t LEFT JOIN users u ON u.id = t.assignee_id WHERE t.project_id = ? ORDER BY t.number",
        (project["id"],),
    ).fetchall()
    kept = {r["id"]: r for r in rows if v.get("include_closed") or r["status"] not in CLOSED_STATUSES}
    milestones = conn.execute("SELECT * FROM milestones WHERE project_id = ? ORDER BY due_date, id", (project["id"],)).fetchall()
    if not kept and not milestones:
        raise Conflict("Project %s has no open tasks or milestones to save" % project["key"], code="template_empty")
    if len(kept) > MAX_TASKS or len(milestones) > MAX_MILESTONES:
        raise Conflict(
            "A template holds at most %d tasks and %d milestones (this project has %d and %d)" % (MAX_TASKS, MAX_MILESTONES, len(kept), len(milestones)),
            code="template_too_large",
        )  # fmt: skip

    anchor = parse_date(v.get("anchor_date")) or parse_date(project["start_date"])
    if anchor is None:
        dates = [d for r in kept.values() for d in (r["start_date"], r["due_date"]) if d] + [m["due_date"] for m in milestones]
        anchor = min((parse_date(d) for d in dates), default=None)
    if anchor is None:
        anchor = parse_date(project["created_at"][:10]) or ctx.today

    labels = {}
    for r in conn.execute("SELECT task_id, label FROM task_labels WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?) ORDER BY label", (project["id"],)):
        labels.setdefault(r["task_id"], []).append(r["label"])

    def kept_ancestor(row):
        pid = row["parent_id"]
        while pid is not None and pid not in kept:
            pid = next((r["parent_id"] for r in rows if r["id"] == pid), None)
        return pid

    depth = {}

    def depth_of(tid):
        if tid not in depth:
            parent = kept_ancestor(kept[tid])
            depth[tid] = 0 if parent is None else depth_of(parent) + 1
        return depth[tid]

    ordered = sorted(kept, key=lambda tid: (depth_of(tid), kept[tid]["number"]))
    ref_of = {tid: "T%d" % (i + 1) for i, tid in enumerate(ordered)}
    mref_of = {m["id"]: "M%d" % (i + 1) for i, m in enumerate(milestones)}

    custom_slots = v.get("slots", {})
    sources = {kept[tid]["username"] for tid in ordered if kept[tid]["username"]}
    if not isinstance(custom_slots, dict) or any(not isinstance(a, str) or not isinstance(b, str) for a, b in custom_slots.items()):
        raise ValidationError({"slots": "must map usernames to slot names, e.g. {\"alice\": \"designer\"}"})
    unknown = sorted(set(custom_slots) - sources)
    if unknown:
        raise ValidationError({"slots": "%s %s no task in what is being saved" % (", ".join(unknown), "has" if len(unknown) == 1 else "have")})
    slot_of, taken = {}, set()
    for username in sorted(sources):
        name = (custom_slots.get(username) or username).strip()
        if not 1 <= len(name) <= MAX_SLOT_NAME or any(ord(ch) < 32 for ch in name):
            raise ValidationError({"slots": "slot names are 1-%d characters without control characters" % MAX_SLOT_NAME})
        if name.lower() in taken:
            raise ValidationError({"slots": "the slot name %r is used twice" % name})
        taken.add(name.lower())
        slot_of[username] = name
    display = {kept[tid]["username"]: kept[tid]["display_name"] for tid in ordered if kept[tid]["username"]}
    slots = [{"name": slot_of[u], "source_user": u, "source_name": display[u]} for u in sorted(sources)]

    task_list = []
    for tid in ordered:
        r = kept[tid]
        parent = kept_ancestor(r)
        task_list.append({
            "ref": ref_of[tid], "title": r["title"], "description": r["description"], "type": r["type"], "priority": r["priority"],
            "estimate_hours": r["estimate_hours"], "labels": labels.get(tid, []), "parent": ref_of[parent] if parent is not None else None,
            "milestone": mref_of.get(r["milestone_id"]), "slot": slot_of.get(r["username"]),
            "start_offset": _offset(r["start_date"], anchor, "a task start date"), "due_offset": _offset(r["due_date"], anchor, "a task due date"),
        })  # fmt: skip
    milestone_list = [
        {"ref": mref_of[m["id"]], "name": m["name"], "description": m["description"], "offset": _offset(m["due_date"], anchor, "a milestone date")}
        for m in milestones
    ]  # fmt: skip
    dependencies = [
        [ref_of[r["blocker_id"]], ref_of[r["blocked_id"]]]
        for r in conn.execute(
            "SELECT d.blocker_id, d.blocked_id FROM task_dependencies d JOIN tasks t ON t.id = d.blocker_id WHERE t.project_id = ? ORDER BY d.blocker_id, d.blocked_id",
            (project["id"],),
        )
        if r["blocker_id"] in kept and r["blocked_id"] in kept
    ]  # fmt: skip
    span = None
    if project["due_date"] and parse_date(project["due_date"]):
        span = _offset(project["due_date"], anchor, "the project due date")
    document = {
        "format": FORMAT, "project": {"span_days": span, "description": project["description"]}, "slots": slots,
        "milestones": milestone_list, "tasks": task_list, "dependencies": dependencies,
    }  # fmt: skip
    if not _acyclic({t["ref"]: t["parent"] for t in task_list}, dependencies):
        raise Conflict("The tasks being saved contain a dependency loop", code="template_cycle")
    blob = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if len(blob.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise Conflict("The template would be larger than %d bytes" % MAX_DOCUMENT_BYTES, code="template_too_large")
    cur = conn.execute(
        "INSERT INTO project_templates (name, description, source_project, anchor_date, data, task_count, milestone_count, dependency_count, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (v["name"], v.get("description", ""), project["key"], anchor.isoformat(), blob, len(task_list), len(milestone_list), len(dependencies), ctx.actor_id, ctx.now_iso, ctx.now_iso),
    )
    activity.log(
        ctx, "template.created", project_id=project["id"],
        details={"id": cur.lastrowid, "name": v["name"], "source": project["key"], "tasks": len(task_list), "milestones": len(milestone_list), "dependencies": len(dependencies), "skipped_closed": len(rows) - len(kept)},
    )  # fmt: skip
    return get_template(ctx, cur.lastrowid)


def update_template(ctx, template_id, data):
    row = _get_row(ctx, template_id)
    c = Checker(data, {"name", "description"}, partial=True)
    c.text("name", min_len=1, max_len=80)
    c.text("description", max_len=2000)
    v = c.result()
    if "name" in v:
        _name_taken(ctx, v["name"], row["id"])
    changes = {f: [row[f], val] for f, val in v.items() if row[f] != val}
    if changes:
        ctx.conn.execute(
            "UPDATE project_templates SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % f for f in changes),
            [c_[1] for c_ in changes.values()] + [ctx.now_iso, row["id"]],
        )
        activity.log(ctx, "template.updated", details={"id": row["id"], "name": row["name"], "changes": changes})
    return get_template(ctx, row["id"])


def delete_template(ctx, template_id):
    row = _get_row(ctx, template_id)
    ctx.conn.execute("DELETE FROM project_templates WHERE id = ?", (row["id"],))
    activity.log(ctx, "template.deleted", details={"id": row["id"], "name": row["name"], "times_used": row["times_used"]})


# ------------------------------------------------------------------------------ starting a project


def _roll(day, roll_weekends):
    """Saturday and Sunday become Monday; the map never reorders two dates."""
    if roll_weekends and day.weekday() >= 5:
        day += timedelta(days=7 - day.weekday())
    return day


def plan(ctx, row, data):
    """Validate a request to start a project from this template and work out everything that would be created."""
    doc = json.loads(row["data"])
    c = Checker(data, {"key", "name", "start_date", "description", "owner", "members", "assignments", "custom_fields", "roll_weekends"})
    c.text("key", required=True, min_len=2, max_len=10)
    c.text("name", required=True, min_len=1, max_len=100)
    c.date("start_date", required=True, nullable=False)
    c.text("description", max_len=5000)
    c.raw("owner")
    c.raw("members")
    c.raw("assignments")
    c.raw("custom_fields")
    c.boolean("roll_weekends")
    errors = {}
    try:
        v = c.result()
    except ValidationError as exc:
        errors.update(exc.details)
        v = c.out
    if "key" in v:
        key = v["key"].upper()
        if not PROJECT_KEY_RE.match(key):
            errors["key"] = "2-10 characters, start with a letter, letters and digits only"
        elif ctx.conn.execute("SELECT 1 FROM projects WHERE key = ?", (key,)).fetchone():
            raise Conflict("Project key %s is already in use" % key, code="key_taken")
        v["key"] = key

    def person(name, field):
        try:
            return projects._active_user(ctx.conn, name, field)
        except ValidationError as exc:
            errors.update(exc.details)

    owner = person(v["owner"], "owner") if v.get("owner") is not None else None
    members = v.get("members") or []
    if not isinstance(members, list):
        errors["members"] = "must be a list of usernames"
        members = []
    assignments = v.get("assignments") or {}
    slot_names = {s["name"]: s for s in doc["slots"]}
    if not isinstance(assignments, dict):
        errors["assignments"] = "must map slot names to usernames"
        assignments = {}
    unknown = sorted(set(assignments) - set(slot_names))
    if unknown:
        errors["assignments"] = "unknown slot%s: %s (this template has: %s)" % ("s" if len(unknown) > 1 else "", ", ".join(map(str, unknown)), ", ".join(slot_names) or "none")
    assigned = {}
    for slot, username in assignments.items():
        if slot in slot_names and username is not None:
            user = person(username, "assignments")
            if user is not None:
                assigned[slot] = user
    if errors:
        raise ValidationError(errors)

    day0 = parse_date(v["start_date"])
    if not 2000 <= day0.year <= 2100:  # keeps every shifted date (at most ~10 years either side) inside the calendar
        raise ValidationError({"start_date": "must be a date between 2000 and 2100"})
    roll = bool(v.get("roll_weekends"))

    def shift(offset):
        return None if offset is None else _roll(day0 + timedelta(days=offset), roll).isoformat()

    due = shift(doc["project"]["span_days"])
    try:
        # the same date rule as creating a project by hand
        projects._check_dates(v["start_date"], due)
    except ValidationError as exc:
        raise ValidationError({"start_date": exc.details.get("due_date", "inconsistent dates")}) from None
    if not _acyclic({t["ref"]: t["parent"] for t in doc["tasks"]}, [tuple(d) for d in doc["dependencies"]]):
        raise Conflict("The template contains a dependency loop", code="template_cycle")

    member_names = []
    for name in members:
        user = person(name, "members")
        if user is not None and user["username"] not in member_names:
            member_names.append(user["username"])
    if errors:
        raise ValidationError(errors)
    for user in assigned.values():
        if user["username"] not in member_names:
            member_names.append(user["username"])
    project_body = {"key": v["key"], "name": v["name"], "description": v.get("description", doc["project"]["description"]), "start_date": v["start_date"], "members": member_names}
    if due:
        project_body["due_date"] = due
    if owner is not None:
        project_body["owner"] = owner["username"]
    if v.get("custom_fields") is not None:
        project_body["custom_fields"] = v["custom_fields"]
    milestones = [{"ref": m["ref"], "name": m["name"], "description": m["description"], "due_date": shift(m["offset"])} for m in doc["milestones"]]
    task_plan = [
        {
            "ref": t["ref"], "title": t["title"], "description": t["description"], "type": t["type"], "priority": t["priority"],
            "estimate_hours": t["estimate_hours"], "labels": t["labels"], "parent": t["parent"], "milestone": t["milestone"],
            "slot": t["slot"], "assignee": assigned[t["slot"]]["username"] if t["slot"] in assigned else None,
            "start_date": shift(t["start_offset"]), "due_date": shift(t["due_offset"]),
        }
        for t in doc["tasks"]
    ]  # fmt: skip
    return {
        "project": project_body, "milestones": milestones, "tasks": task_plan, "dependencies": doc["dependencies"],
        "unassigned_slots": sorted(s for s in slot_names if s not in assigned),
        "counts": {"tasks": len(task_plan), "milestones": len(milestones), "dependencies": len(doc["dependencies"]), "assigned": sum(1 for t in task_plan if t["assignee"])},
    }  # fmt: skip


def preview(ctx, template_id, data):
    row = _get_row(ctx, template_id)
    return {"template": row["id"], "dry_run": True, **plan(ctx, row, data)}


def instantiate(ctx, template_id, data):
    row = _get_row(ctx, template_id)
    made = plan(ctx, row, data)
    project = projects.create_project(ctx, made["project"])
    prow = projects.get_row(ctx.conn, project["key"])
    reporter = ctx.actor_id or prow["owner_id"]
    milestone_ids = {}
    for m in made["milestones"]:
        cur = ctx.conn.execute(
            "INSERT INTO milestones (project_id, name, description, due_date, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (prow["id"], m["name"], m["description"], m["due_date"], ctx.now_iso, ctx.now_iso),
        )
        milestone_ids[m["ref"]] = cur.lastrowid
        activity.log(ctx, "milestone.created", project_id=prow["id"], details={"name": m["name"], "due_date": m["due_date"], "from_template": row["name"]})
    ids, keys = {}, {}
    for t in made["tasks"]:  # parents come first
        body = {
            "title": t["title"], "description": t["description"], "type": t["type"], "priority": t["priority"], "labels": t["labels"],
            "estimate_hours": t["estimate_hours"], "assignee": t["assignee"], "start_date": t["start_date"], "due_date": t["due_date"],
            "parent": keys[t["parent"]] if t["parent"] else None, "milestone": milestone_ids[t["milestone"]] if t["milestone"] else None,
        }  # fmt: skip
        task_id, key = tasks.create_in_project(ctx, prow, body, notify=False, reporter_id=reporter)
        ids[t["ref"]], keys[t["ref"]] = task_id, key
    for blocker, blocked in made["dependencies"]:
        ctx.conn.execute("INSERT INTO task_dependencies (blocker_id, blocked_id, created_at) VALUES (?,?,?)", (ids[blocker], ids[blocked], ctx.now_iso))
        activity.log(
            ctx, "task.dependency_added", project_id=prow["id"], task_id=ids[blocked], task_key=keys[blocked],
            details={"blocker": keys[blocker], "blocked": keys[blocked]},
        )  # fmt: skip
    ctx.conn.execute("UPDATE project_templates SET times_used = times_used + 1, last_used_at = ? WHERE id = ?", (ctx.now_iso, row["id"]))
    activity.log(
        ctx, "template.instantiated", project_id=prow["id"],
        details={"id": row["id"], "template": row["name"], "project": prow["key"], **made["counts"], "unassigned_slots": made["unassigned_slots"]},
    )  # fmt: skip
    return {"project": projects.get_project(ctx, prow["key"]), "template": row["id"], "created": made["counts"], "unassigned_slots": made["unassigned_slots"], "tasks": keys}
