"""Tasks: validation, workflow rules, filtering and the Kanban board."""

from __future__ import annotations

from datetime import timedelta

from ..domain import (
    BLOCKER_GATED_STATUSES,
    CLOSED_SQL,
    CLOSED_STATUSES,
    MAX_TASK_DEPTH,
    OPEN_SQL,
    OPEN_STATUSES,
    PRIORITIES,
    PRIORITY_RANK_SQL,
    STATUS_RANK_SQL,
    STATUSES,
    TASK_TYPES,
    TRANSITIONS,
)
from ..errors import Conflict, ValidationError
from ..util import Checker, escape_like, parse_date, parse_task_key, truthy
from . import activity, graph, projects, taskrefs, users

TASK_FIELDS = {
    "title", "description", "type", "status", "priority", "assignee", "parent", "milestone",
    "start_date", "due_date", "estimate_hours", "labels", "external_ref",
}  # fmt: skip

TASK_SELECT = (
    "SELECT t.*, p.key AS project_key, p.status AS project_status, "
    "a.username AS a_username, a.display_name AS a_name, "
    "r.username AS r_username, r.display_name AS r_name, "
    "par.number AS parent_number, m.name AS milestone_name, "
    "(SELECT COALESCE(SUM(te.hours), 0) FROM time_entries te WHERE te.task_id = t.id) AS logged_hours, "
    "(SELECT COUNT(*) FROM tasks c WHERE c.parent_id = t.id) AS child_total, "
    "(SELECT COUNT(*) FROM tasks c WHERE c.parent_id = t.id AND c.status = 'done') AS child_done, "
    "(SELECT COUNT(*) FROM tasks c WHERE c.parent_id = t.id AND c.status IN " + OPEN_SQL + ") AS child_open, "
    "(SELECT COUNT(*) FROM task_dependencies d JOIN tasks b ON b.id = d.blocker_id "
    "WHERE d.blocked_id = t.id AND b.status IN " + OPEN_SQL + ") AS blocked_by_open, "
    "(SELECT group_concat(l.label, ',') FROM task_labels l WHERE l.task_id = t.id) AS labels_csv "
    "FROM tasks t JOIN projects p ON p.id = t.project_id "
    "LEFT JOIN users a ON a.id = t.assignee_id "
    "LEFT JOIN users r ON r.id = t.reporter_id "
    "LEFT JOIN tasks par ON par.id = t.parent_id "
    "LEFT JOIN milestones m ON m.id = t.milestone_id "
)

SORT_FIELDS = {
    "key": (("p.key", False), ("t.number", False)),
    "number": (("t.number", False),),
    "title": (("LOWER(t.title)", False),),
    "status": ((STATUS_RANK_SQL, False),),
    "priority": ((PRIORITY_RANK_SQL, False),),
    "due_date": (("t.due_date", True),),
    "start_date": (("t.start_date", True),),
    "created_at": (("t.created_at", False),),
    "updated_at": (("t.updated_at", False),),
    "estimate_hours": (("t.estimate_hours", True),),
    "assignee": (("LOWER(a.display_name)", True),),
}


# ----------------------------------------------------------------------------- output


def to_dict(row, today_iso):
    due = row["due_date"]
    key = "%s-%d" % (row["project_key"], row["number"])
    return {
        "key": key,
        "project": row["project_key"],
        "number": row["number"],
        "title": row["title"],
        "description": row["description"],
        "type": row["type"],
        "status": row["status"],
        "priority": row["priority"],
        "assignee": users.ref(row["assignee_id"], row["a_username"], row["a_name"]),
        "reporter": users.ref(row["reporter_id"], row["r_username"], row["r_name"]),
        "parent": "%s-%d" % (row["project_key"], row["parent_number"]) if row["parent_id"] else None,
        "milestone": {"id": row["milestone_id"], "name": row["milestone_name"]} if row["milestone_id"] else None,
        "start_date": row["start_date"],
        "due_date": due,
        "estimate_hours": row["estimate_hours"],
        "logged_hours": round(row["logged_hours"], 2),
        "labels": sorted(x for x in (row["labels_csv"] or "").split(",") if x),
        "external_ref": row["external_ref"],
        "subtasks": {"total": row["child_total"], "done": row["child_done"], "open": row["child_open"]},
        "blocked_by_open": row["blocked_by_open"],
        "is_overdue": bool(due) and row["status"] in OPEN_STATUSES and due < today_iso,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def _fetch(ctx, ids):
    """Fetch enriched rows for ``ids`` preserving the given order."""
    ids = list(ids)
    if not ids:
        return []
    rows = ctx.conn.execute(TASK_SELECT + "WHERE t.id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
    by_id = {r["id"]: r for r in rows}
    today = ctx.today.isoformat()
    return [to_dict(by_id[i], today) for i in ids if i in by_id]


def get_task(ctx, key, detail=True):
    row = taskrefs.get_row(ctx.conn, key)
    task = _fetch(ctx, [row["id"]])[0]
    if not detail:
        return task
    conn = ctx.conn

    def link_rows(sql):
        return [
            {"key": "%s-%d" % (r["project_key"], r["number"]), "title": r["title"], "status": r["status"]}
            for r in conn.execute(sql, (row["id"],))
        ]

    task["blocked_by"] = link_rows(
        "SELECT b.number, b.title, b.status, p.key AS project_key FROM task_dependencies d "
        "JOIN tasks b ON b.id = d.blocker_id JOIN projects p ON p.id = b.project_id "
        "WHERE d.blocked_id = ? ORDER BY b.number"
    )
    task["blocks"] = link_rows(
        "SELECT b.number, b.title, b.status, p.key AS project_key FROM task_dependencies d "
        "JOIN tasks b ON b.id = d.blocked_id JOIN projects p ON p.id = b.project_id "
        "WHERE d.blocker_id = ? ORDER BY b.number"
    )
    task["subtask_list"] = [
        {
            "key": "%s-%d" % (row["project_key"], r["number"]),
            "title": r["title"],
            "status": r["status"],
            "priority": r["priority"],
            "assignee": r["username"],
            "due_date": r["due_date"],
        }
        for r in conn.execute(
            "SELECT c.number, c.title, c.status, c.priority, c.due_date, u.username FROM tasks c "
            "LEFT JOIN users u ON u.id = c.assignee_id WHERE c.parent_id = ? ORDER BY c.number",
            (row["id"],),
        )
    ]
    task["allowed_transitions"] = list(TRANSITIONS[row["status"]])
    task["project_status"] = row["project_status"]
    return task


# -------------------------------------------------------------------------- validation


def _validate(data, partial):
    c = Checker(data, TASK_FIELDS, partial=partial)
    c.text("title", required=True, min_len=1, max_len=200)
    c.text("description", max_len=20000)
    c.choice("type", TASK_TYPES)
    c.choice("status", STATUSES)
    c.choice("priority", PRIORITIES)
    c.raw("assignee")
    c.raw("parent")
    c.raw("milestone")
    c.date("start_date")
    c.date("due_date")
    c.number("estimate_hours", minimum=0, maximum=10000, nullable=True)
    c.labels()
    c.text("external_ref", max_len=100, nullable=True)
    v = c.result()
    if "external_ref" in v:
        v["external_ref"] = v["external_ref"] or None
    return v


def _check_dates(start, due):
    if start and due and due < start:
        raise ValidationError({"due_date": "must not be before start_date"})


def _resolve_assignee(ctx, project, username):
    if not isinstance(username, str):
        raise ValidationError({"assignee": "must be a username or null"})
    user = users.find_by_username(ctx.conn, username)
    if user is None or not user["active"]:
        raise ValidationError({"assignee": "unknown or inactive user %r" % username})
    if not projects.is_member(ctx.conn, project["id"], user["id"]):
        raise ValidationError(
            {"assignee": "%s is not a member of project %s (add them first)" % (user["username"], project["key"])}
        )
    return user


def _resolve_milestone(ctx, project, milestone_id):
    if isinstance(milestone_id, bool) or not isinstance(milestone_id, int):
        raise ValidationError({"milestone": "must be a milestone id or null"})
    row = ctx.conn.execute(
        "SELECT * FROM milestones WHERE id = ? AND project_id = ?", (milestone_id, project["id"])
    ).fetchone()
    if row is None:
        raise ValidationError({"milestone": "milestone %s does not exist in this project" % milestone_id})
    return row


def _resolve_parent(ctx, project, key):
    if not isinstance(key, str):
        raise ValidationError({"parent": "must be a task key or null"})
    try:
        project_key, number = parse_task_key(key)
    except Exception:
        raise ValidationError({"parent": "invalid task key %r" % key}) from None
    row = ctx.conn.execute(
        taskrefs.BASE + "WHERE p.key = ? AND t.number = ?", (project_key, number)
    ).fetchone()
    if row is None:
        raise ValidationError({"parent": "task %s does not exist" % key})
    if row["project_id"] != project["id"]:
        raise ValidationError({"parent": "must belong to the same project"})
    return row


def _check_depth(ctx, parent, extra_levels):
    """Reject hierarchies deeper than MAX_TASK_DEPTH (``extra_levels`` = levels below the moved task)."""
    depth = len(graph.ancestors(ctx.conn, parent["id"])) + 2 + extra_levels
    if depth > MAX_TASK_DEPTH:
        raise Conflict(
            "Tasks can be nested at most %d levels deep" % MAX_TASK_DEPTH, code="max_depth_exceeded"
        )


def _cycle_error(ctx, cycle):
    keys = taskrefs.keys_for_ids(ctx.conn, cycle)
    path = " \u2192 ".join(keys.get(i, "?") for i in cycle)
    return Conflict(
        "This would create a circular wait: %s. (A parent task cannot finish before its subtasks.)" % path,
        code="dependency_cycle",
        details={"cycle": [keys.get(i) for i in cycle]},
    )


def _check_transition(ctx, row, new):
    old = row["status"]
    if new not in TRANSITIONS[old]:
        raise Conflict(
            "A task cannot move from %s to %s" % (old, new),
            code="invalid_transition",
            details={"from": old, "to": new, "allowed": list(TRANSITIONS[old])},
        )
    if new in BLOCKER_GATED_STATUSES:
        blockers = graph.open_blockers(ctx.conn, row["id"])
        if blockers:
            keys = ["%s-%d" % (b["project_key"], b["number"]) for b in blockers]
            raise Conflict(
                "%s is blocked by unfinished task(s): %s" % (taskrefs.key_of(row), ", ".join(keys)),
                code="blocked_by_open_tasks",
                details={"blockers": keys},
            )
    if new in CLOSED_STATUSES:
        open_children = ctx.conn.execute(
            "SELECT number FROM tasks WHERE parent_id = ? AND status IN %s ORDER BY number" % OPEN_SQL,
            (row["id"],),
        ).fetchall()
        if open_children:
            keys = ["%s-%d" % (row["project_key"], c["number"]) for c in open_children]
            raise Conflict(
                "%s still has open subtask(s): %s" % (taskrefs.key_of(row), ", ".join(keys)),
                code="open_subtasks",
                details={"subtasks": keys},
            )
    if old in CLOSED_STATUSES and new in OPEN_STATUSES and row["parent_id"]:
        parent = taskrefs.get_row_by_id(ctx.conn, row["parent_id"])
        if parent["status"] in CLOSED_STATUSES:
            raise Conflict(
                "Reopen the parent task %s first" % taskrefs.key_of(parent), code="parent_closed"
            )


def _milestone_name(conn, milestone_id):
    if milestone_id is None:
        return None
    row = conn.execute("SELECT name FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    return row["name"] if row else None


def _set_labels(conn, task_id, labels):
    conn.execute("DELETE FROM task_labels WHERE task_id = ?", (task_id,))
    conn.executemany("INSERT INTO task_labels (task_id, label) VALUES (?,?)", [(task_id, x) for x in labels])


# ------------------------------------------------------------------------------ create


def create_in_project(ctx, project, data, *, notify=True, log=True, reporter_id=None):
    """Create a task and return ``(task_id, key)``. Used by the API and the CSV importer."""
    conn = ctx.conn
    v = _validate(data, partial=False)
    _check_dates(v.get("start_date"), v.get("due_date"))
    status = v.get("status", "todo")
    assignee = _resolve_assignee(ctx, project, v["assignee"]) if v.get("assignee") is not None else None
    milestone = _resolve_milestone(ctx, project, v["milestone"]) if v.get("milestone") is not None else None
    parent = None
    if v.get("parent") is not None:
        parent = _resolve_parent(ctx, project, v["parent"])
        if status in OPEN_STATUSES and parent["status"] in CLOSED_STATUSES:
            raise Conflict(
                "Parent %s is closed. Reopen it before adding open subtasks." % taskrefs.key_of(parent),
                code="parent_closed",
            )
        _check_depth(ctx, parent, 0)

    reporter = reporter_id or ctx.actor_id or project["owner_id"]
    if reporter is None:
        raise ValidationError({"reporter": "no acting user and the project has no owner"})

    number = conn.execute("SELECT next_task_number FROM projects WHERE id = ?", (project["id"],)).fetchone()[0]
    conn.execute("UPDATE projects SET next_task_number = ? WHERE id = ?", (number + 1, project["id"]))
    cur = conn.execute(
        "INSERT INTO tasks (project_id, number, title, description, type, status, priority, assignee_id, "
        "reporter_id, parent_id, milestone_id, start_date, due_date, estimate_hours, external_ref, "
        "created_at, updated_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            project["id"],
            number,
            v["title"],
            v.get("description", ""),
            v.get("type", "task"),
            status,
            v.get("priority", "medium"),
            assignee["id"] if assignee else None,
            reporter,
            parent["id"] if parent else None,
            milestone["id"] if milestone else None,
            v.get("start_date"),
            v.get("due_date"),
            v.get("estimate_hours"),
            v.get("external_ref"),
            ctx.now_iso,
            ctx.now_iso,
            ctx.now_iso if status in CLOSED_STATUSES else None,
        ),
    )
    task_id = cur.lastrowid
    key = "%s-%d" % (project["key"], number)
    _set_labels(conn, task_id, v.get("labels", []))
    if log:
        activity.log(
            ctx,
            "task.created",
            project_id=project["id"],
            task_id=task_id,
            task_key=key,
            details={
                "title": v["title"],
                "status": status,
                "assignee": assignee["username"] if assignee else None,
            },
        )
    if notify and assignee and assignee["id"] != ctx.actor_id:
        _emit_assigned(ctx, key, v["title"], assignee, v.get("priority", "medium"), v.get("due_date"))
    return task_id, key


def create_task(ctx, project_key, data):
    project = projects.get_row(ctx.conn, project_key)
    projects.require_write(ctx, project)
    _, key = create_in_project(ctx, project, data)
    return get_task(ctx, key)


def _emit_assigned(ctx, key, title, assignee, priority, due_date):
    ctx.emit(
        "task_assigned",
        key=key,
        title=title,
        assignee=assignee["display_name"],
        actor=ctx.actor_name,
        priority=priority,
        due_date=due_date,
    )


# ------------------------------------------------------------------------------ update


def update_task(ctx, key, data):
    conn = ctx.conn
    row = taskrefs.get_row(conn, key)
    project = projects.get_row(conn, row["project_key"])
    projects.require_write(ctx, project)
    v = _validate(data, partial=True)
    task_key = taskrefs.key_of(row)

    updates, changes = {}, {}
    for field in ("title", "description", "type", "priority", "start_date", "due_date", "estimate_hours", "external_ref"):
        if field in v and v[field] != row[field]:
            updates[field] = v[field]
            changes[field] = [activity.diff_value(row[field]), activity.diff_value(v[field])]
    _check_dates(updates.get("start_date", row["start_date"]), updates.get("due_date", row["due_date"]))

    new_assignee = None
    if "assignee" in v:
        new_assignee = None if v["assignee"] is None else _resolve_assignee(ctx, project, v["assignee"])
        new_id = new_assignee["id"] if new_assignee else None
        if new_id != row["assignee_id"]:
            old = users.get_row(conn, row["assignee_id"]) if row["assignee_id"] else None
            updates["assignee_id"] = new_id
            changes["assignee"] = [old["username"] if old else None, new_assignee["username"] if new_assignee else None]
        else:
            new_assignee = None

    if "milestone" in v:
        new_ms = None if v["milestone"] is None else _resolve_milestone(ctx, project, v["milestone"])["id"]
        if new_ms != row["milestone_id"]:
            updates["milestone_id"] = new_ms
            changes["milestone"] = [_milestone_name(conn, row["milestone_id"]), _milestone_name(conn, new_ms)]

    final_status = v.get("status", row["status"])
    if "parent" in v:
        new_parent = None if v["parent"] is None else _resolve_parent(ctx, project, v["parent"])
        new_parent_id = new_parent["id"] if new_parent else None
        if new_parent_id != row["parent_id"]:
            if new_parent is not None:
                if new_parent["id"] == row["id"]:
                    raise ValidationError({"parent": "a task cannot be its own parent"})
                if final_status in OPEN_STATUSES and new_parent["status"] in CLOSED_STATUSES:
                    raise Conflict(
                        "Parent %s is closed. Reopen it first." % taskrefs.key_of(new_parent), code="parent_closed"
                    )
                _check_depth(ctx, new_parent, graph.subtree_height(conn, row["id"]))
                remove = [(row["id"], row["parent_id"])] if row["parent_id"] else []
                cycle = graph.find_cycle(conn, row["project_id"], [(row["id"], new_parent["id"])], remove)
                if cycle:
                    raise _cycle_error(ctx, cycle)
            old_parent = taskrefs.get_row_by_id(conn, row["parent_id"]) if row["parent_id"] else None
            updates["parent_id"] = new_parent_id
            changes["parent"] = [
                taskrefs.key_of(old_parent) if old_parent else None,
                taskrefs.key_of(new_parent) if new_parent else None,
            ]

    if "status" in v and v["status"] != row["status"]:
        _check_transition(ctx, row, v["status"])
        updates["status"] = v["status"]
        updates["completed_at"] = ctx.now_iso if v["status"] in CLOSED_STATUSES else None
        changes["status"] = [row["status"], v["status"]]

    if "labels" in v:
        current = sorted(r[0] for r in conn.execute("SELECT label FROM task_labels WHERE task_id = ?", (row["id"],)))
        if current != sorted(v["labels"]):
            _set_labels(conn, row["id"], v["labels"])
            changes["labels"] = [current, sorted(v["labels"])]

    if not changes:
        return get_task(ctx, key)

    updates["updated_at"] = ctx.now_iso
    conn.execute(
        "UPDATE tasks SET %s WHERE id = ?" % ", ".join("%s = ?" % c for c in updates),
        list(updates.values()) + [row["id"]],
    )
    action = "task.status_changed" if "status" in changes else "task.updated"
    activity.log(
        ctx, action, project_id=row["project_id"], task_id=row["id"], task_key=task_key, details={"changes": changes}
    )
    title = updates.get("title", row["title"])
    if new_assignee is not None and new_assignee["id"] != ctx.actor_id:
        _emit_assigned(
            ctx, task_key, title, new_assignee,
            updates.get("priority", row["priority"]), updates.get("due_date", row["due_date"]),
        )  # fmt: skip
    if "status" in changes and changes["status"][1] in ("done", "blocked"):
        ctx.emit(
            "task_status", key=task_key, title=title, old=changes["status"][0],
            new=changes["status"][1], actor=ctx.actor_name,
        )  # fmt: skip
    return get_task(ctx, key)


# ------------------------------------------------------------------------------ delete


def delete_task(ctx, key, cascade=False):
    conn = ctx.conn
    row = taskrefs.get_row(conn, key)
    project = projects.get_row(conn, row["project_key"])
    projects.require_write(ctx, project)
    descendants, frontier = 0, [row["id"]]
    while frontier and descendants < 100000:
        kids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM tasks WHERE parent_id IN (%s)" % ",".join("?" * len(frontier)), frontier
            )
        ]
        descendants += len(kids)
        frontier = kids
    if descendants and not cascade:
        raise Conflict(
            "%s has %d subtask(s). Delete them too with ?cascade=true." % (taskrefs.key_of(row), descendants),
            code="has_subtasks",
            details={"subtasks": descendants},
        )
    conn.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
    activity.log(
        ctx, "task.deleted", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
        details={"title": row["title"], "subtasks_deleted": descendants},
    )  # fmt: skip


# ------------------------------------------------------------------------------ listing


def _csv(params, name):
    return [x.strip() for x in (params.get(name) or "").split(",") if x.strip()]


def _enum_list(params, name, allowed):
    values = _csv(params, name)
    bad = [x for x in values if x not in allowed]
    if bad:
        raise ValidationError({name: "unknown value(s) %s; allowed: %s" % (", ".join(bad), ", ".join(allowed))})
    return values


def _user_condition(ctx, params, name, where, args):
    """Filter by assignee/reporter: usernames, ``me``, or (assignee only) ``none``."""
    alias = "a" if name == "assignee" else "r"
    conds = []
    for item in _csv(params, name):
        if item == "me" and ctx.user is not None:
            conds.append("t.%s_id = ?" % name)
            args.append(ctx.user["id"])
        elif item in ("none", "unassigned") and name == "assignee":
            conds.append("t.assignee_id IS NULL")
        else:
            conds.append("%s.username = ?" % alias)
            args.append(item)
    if conds:
        where.append("(" + " OR ".join(conds) + ")")


def build_filters(ctx, params):
    where, args = [], []
    keys = _csv(params, "project")
    if keys:
        where.append("p.key IN (%s)" % ",".join("?" * len(keys)))
        args += [k.upper() for k in keys]
    for name, allowed in (("status", STATUSES), ("priority", PRIORITIES), ("type", TASK_TYPES)):
        values = _enum_list(params, name, allowed)
        if values:
            where.append("t.%s IN (%s)" % (name, ",".join("?" * len(values))))
            args += values
    if truthy(params.get("open")):
        where.append("t.status IN " + OPEN_SQL)
    if truthy(params.get("closed")):
        where.append("t.status IN " + CLOSED_SQL)
    _user_condition(ctx, params, "assignee", where, args)
    _user_condition(ctx, params, "reporter", where, args)
    labels = [x.lower() for x in _csv(params, "label")]
    if labels:
        where.append(
            "EXISTS (SELECT 1 FROM task_labels l WHERE l.task_id = t.id AND l.label IN (%s))"
            % ",".join("?" * len(labels))
        )
        args += labels
    milestone = (params.get("milestone") or "").strip()
    if milestone == "none":
        where.append("t.milestone_id IS NULL")
    elif milestone:
        try:
            args.append(int(milestone))
        except ValueError:
            raise ValidationError({"milestone": "must be a milestone id or 'none'"}) from None
        where.append("t.milestone_id = ?")
    parent = (params.get("parent") or "").strip()
    if parent.lower() == "none":
        where.append("t.parent_id IS NULL")
    elif parent:
        try:
            project_key, number = parse_task_key(parent)
        except Exception:
            raise ValidationError({"parent": "must be a task key or 'none'"}) from None
        where.append(
            "t.parent_id = COALESCE((SELECT t2.id FROM tasks t2 JOIN projects p2 ON p2.id = t2.project_id "
            "WHERE p2.key = ? AND t2.number = ?), -1)"
        )
        args += [project_key, number]
    if truthy(params.get("leaf")):
        where.append("NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = t.id)")
    q = (params.get("q") or "").strip()
    if q:
        like = "%" + escape_like(q) + "%"
        where.append(
            "(t.title LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\' "
            "OR (p.key || '-' || t.number) = ? COLLATE NOCASE)"
        )
        args += [like, like, q]
    ref = (params.get("external_ref") or "").strip()
    if ref:
        where.append("t.external_ref = ?")
        args.append(ref)
    for name, op in (("due_before", "<="), ("due_after", ">=")):
        if params.get(name):
            if parse_date(params[name]) is None:
                raise ValidationError({name: "must be a valid date in YYYY-MM-DD format"})
            where.append("t.due_date IS NOT NULL AND t.due_date %s ?" % op)
            args.append(params[name])
    if truthy(params.get("overdue")):
        where.append("t.status IN %s AND t.due_date IS NOT NULL AND t.due_date < ?" % OPEN_SQL)
        args.append(ctx.today.isoformat())
    return (" WHERE " + " AND ".join(where)) if where else "", args


def order_sql(sort):
    parts = []
    for raw in [s.strip() for s in (sort or "key").split(",") if s.strip()]:
        name = raw.lstrip("-+ ")
        spec = SORT_FIELDS.get(name)
        if spec is None:
            raise ValidationError({"sort": "unknown sort field %r; use one of: %s" % (name, ", ".join(SORT_FIELDS))})
        direction = "DESC" if raw.startswith("-") else "ASC"
        for expr, nulls_last in spec:
            if nulls_last:
                parts.append("(%s IS NULL) ASC" % expr)
            parts.append("%s %s" % (expr, direction))
    parts.append("t.id ASC")
    return " ORDER BY " + ", ".join(parts)


def _paging(params):
    try:
        limit = int(params.get("limit", 50))
        offset = int(params.get("offset", 0))
    except ValueError:
        raise ValidationError({"limit": "limit and offset must be integers"}) from None
    if limit < 1 or offset < 0:
        raise ValidationError({"limit": "limit must be >= 1 and offset >= 0"})
    return min(limit, 200), offset


FROM_FOR_IDS = (
    " FROM tasks t JOIN projects p ON p.id = t.project_id "
    "LEFT JOIN users a ON a.id = t.assignee_id LEFT JOIN users r ON r.id = t.reporter_id"
)


def list_tasks(ctx, params):
    where, args = build_filters(ctx, params)
    order = order_sql(params.get("sort"))
    limit, offset = _paging(params)
    total = ctx.conn.execute("SELECT COUNT(*)" + FROM_FOR_IDS + where, args).fetchone()[0]
    ids = [
        r[0]
        for r in ctx.conn.execute(
            "SELECT t.id" + FROM_FOR_IDS + where + order + " LIMIT ? OFFSET ?", args + [limit, offset]
        )
    ]
    return {"items": _fetch(ctx, ids), "total": total, "limit": limit, "offset": offset}


def board(ctx, project_key, closed_days=14):
    project = projects.get_row(ctx.conn, project_key)
    where = "WHERE t.project_id = ?"
    args = [project["id"]]
    hidden = 0
    if closed_days > 0:
        cutoff = (ctx.now - timedelta(days=closed_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hidden_sql = "t.status IN %s AND t.completed_at < ?" % CLOSED_SQL
        hidden = ctx.conn.execute(
            "SELECT COUNT(*) FROM tasks t WHERE t.project_id = ? AND " + hidden_sql, (project["id"], cutoff)
        ).fetchone()[0]
        where += " AND NOT (%s)" % hidden_sql
        args.append(cutoff)
    limit = 500
    ids = [
        r[0]
        for r in ctx.conn.execute(
            "SELECT t.id FROM tasks t " + where + order_sql("-priority,due_date,number") + " LIMIT ?",
            args + [limit + 1],
        )
    ]  # fmt: skip
    truncated = len(ids) > limit
    tasks = _fetch(ctx, ids[:limit])
    columns = [{"status": s, "tasks": [t for t in tasks if t["status"] == s]} for s in STATUSES]
    return {"project": project["key"], "columns": columns, "truncated": truncated, "hidden_closed": hidden}
