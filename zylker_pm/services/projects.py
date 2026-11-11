"""Projects, membership and the write-permission gate used by task-level services."""

from __future__ import annotations

from ..domain import OPEN_SQL, PROJECT_STATUSES
from ..errors import Conflict, Forbidden, NotFound, ValidationError
from ..util import PROJECT_KEY_RE, Checker, escape_like
from . import activity, project_fields, users

PROJECT_SELECT = (
    "SELECT p.*, o.username AS o_username, o.display_name AS o_name, "
    "(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS n_total, "
    "(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status IN %s) AS n_open, "
    "(SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'done') AS n_done "
    "FROM projects p LEFT JOIN users o ON o.id = p.owner_id " % OPEN_SQL
)


def get_row(conn, key):
    key = (key or "").upper()
    row = conn.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise NotFound("Project %s not found" % key, code="project_not_found")
    return row


def is_member(conn, project_id, user_id) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?", (project_id, user_id)
        ).fetchone()
        is not None
    )


def require_write(ctx, project):
    """Writers must have the right role, belong to the project, and the project must not be archived."""
    if ctx.user is not None:
        role = ctx.user["role"]
        if role == "viewer":
            raise Forbidden("Viewers have read-only access", code="insufficient_role")
        if role == "member" and not is_member(ctx.conn, project["id"], ctx.user["id"]):
            raise Forbidden("You are not a member of project %s" % project["key"], code="not_project_member")
    if project["status"] == "archived":
        raise Conflict(
            "Project %s is archived and read-only. Change its status to edit it." % project["key"],
            code="project_archived",
        )


def member_rows(conn, project_id):
    return conn.execute(
        "SELECT u.* FROM project_members pm JOIN users u ON u.id = pm.user_id "
        "WHERE pm.project_id = ? ORDER BY u.display_name COLLATE NOCASE",
        (project_id,),
    ).fetchall()


def to_dict(ctx, row, detail=False, custom=None):
    """``custom`` is the project's ``{field_key: value}`` map; pass it when serialising many rows at once."""
    if custom is None:
        custom = project_fields.values_map(ctx.conn, [row["id"]])[row["id"]]
    data = {
        "key": row["key"],
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
        "owner": users.ref(row["owner_id"], row["o_username"], row["o_name"]),
        "start_date": row["start_date"],
        "due_date": row["due_date"],
        "task_counts": {"total": row["n_total"], "open": row["n_open"], "done": row["n_done"]},
        "custom_fields": custom,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if detail:
        data["members"] = [users.public(m) for m in member_rows(ctx.conn, row["id"])]
    return data


def get_project(ctx, key, detail=True):
    get_row(ctx.conn, key)  # 404 early
    row = ctx.conn.execute(PROJECT_SELECT + "WHERE p.key = ?", (key.upper(),)).fetchone()
    return to_dict(ctx, row, detail)


def _active_user(conn, username, field):
    if not isinstance(username, str):
        raise ValidationError({field: "must be a username"})
    user = users.find_by_username(conn, username)
    if user is None or not user["active"]:
        raise ValidationError({field: "unknown or inactive user %r" % username})
    if users.is_client_contact(conn, user["id"]):
        raise ValidationError({field: "%r is a client contact and cannot take part in projects" % username})
    return user


def _check_dates(start, due):
    if start and due and due < start:
        raise ValidationError({"due_date": "must not be before start_date"})


def _validated(ctx, checker, *, creating):
    """Run the field checker and the custom-field validation together, so one 422 lists every problem."""
    errors, v = {}, {}
    try:
        v = checker.result()
    except ValidationError as exc:
        errors.update(exc.details)
    custom = {}
    if "custom_fields" not in errors:
        try:
            custom = project_fields.validate_input(ctx.conn, checker.data.get("custom_fields"), creating=creating)
        except ValidationError as exc:
            errors.update(exc.details)
    if errors:
        raise ValidationError(errors)
    v.pop("custom_fields", None)
    return v, custom


def list_projects(ctx, params):
    where, args = [], []
    statuses = [s for s in (params.get("status") or "").split(",") if s]
    for s in statuses:
        if s not in PROJECT_STATUSES:
            raise ValidationError({"status": "unknown project status %r" % s})
    if statuses:
        where.append("p.status IN (%s)" % ",".join("?" * len(statuses)))
        args += statuses
    elif params.get("include_archived") not in ("1", "true", "yes"):
        where.append("p.status <> 'archived'")
    q = (params.get("q") or "").strip()
    if q:
        where.append(
            "(p.name LIKE ? ESCAPE '\\' OR p.key LIKE ? ESCAPE '\\' OR p.id IN "
            "(SELECT v.project_id FROM project_field_values v WHERE v.text_value LIKE ? ESCAPE '\\'))"
        )
        args += ["%" + escape_like(q) + "%"] * 3
    member = params.get("member")
    if member:
        if member == "me" and ctx.user is not None:
            uid = ctx.user["id"]
        else:
            row = users.find_by_username(ctx.conn, member)
            uid = row["id"] if row else -1
        where.append("p.id IN (SELECT project_id FROM project_members WHERE user_id = ?)")
        args.append(uid)
    sql = PROJECT_SELECT + ("WHERE " + " AND ".join(where) + " " if where else "") + "ORDER BY p.key"
    rows = ctx.conn.execute(sql, args).fetchall()
    custom = project_fields.values_map(ctx.conn, [r["id"] for r in rows])
    return [to_dict(ctx, r, custom=custom[r["id"]]) for r in rows]


def create_project(ctx, data):
    c = Checker(data, {"key", "name", "description", "status", "owner", "start_date", "due_date", "members", "custom_fields"})
    c.text("key", required=True, min_len=2, max_len=10)
    c.text("name", required=True, min_len=1, max_len=100)
    c.text("description", max_len=5000)
    c.choice("status", PROJECT_STATUSES)
    c.raw("owner")
    c.date("start_date")
    c.date("due_date")
    c.raw("members")
    c.raw("custom_fields")
    v, custom = _validated(ctx, c, creating=True)
    key = v["key"].upper()
    if not PROJECT_KEY_RE.match(key):
        raise ValidationError({"key": "2-10 characters, start with a letter, letters and digits only"})
    _check_dates(v.get("start_date"), v.get("due_date"))
    if ctx.conn.execute("SELECT 1 FROM projects WHERE key = ?", (key,)).fetchone():
        raise Conflict("Project key %s is already in use" % key, code="key_taken")

    owner = None
    if v.get("owner") is not None:
        owner = _active_user(ctx.conn, v["owner"], "owner")
    elif ctx.user is not None:
        owner = ctx.user
    members = {}
    if owner is not None:
        members[owner["id"]] = owner
    raw_members = v.get("members") or []
    if not isinstance(raw_members, list):
        raise ValidationError({"members": "must be a list of usernames"})
    for name in raw_members:
        member = _active_user(ctx.conn, name, "members")
        members[member["id"]] = member

    cur = ctx.conn.execute(
        "INSERT INTO projects (key, name, description, status, owner_id, start_date, due_date, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            key,
            v["name"],
            v.get("description", ""),
            v.get("status", "active"),
            owner["id"] if owner else None,
            v.get("start_date"),
            v.get("due_date"),
            ctx.now_iso,
            ctx.now_iso,
        ),
    )
    for uid in members:
        ctx.conn.execute("INSERT INTO project_members (project_id, user_id) VALUES (?,?)", (cur.lastrowid, uid))
    stored = project_fields.store(ctx, cur.lastrowid, custom)
    details = {"key": key, "name": v["name"]}
    if stored:
        details["custom_fields"] = {k.split(".", 1)[1]: new for k, (_, new) in stored.items()}
    activity.log(ctx, "project.created", project_id=cur.lastrowid, details=details)
    return get_project(ctx, key)


def update_project(ctx, key, data):
    row = get_row(ctx.conn, key)
    c = Checker(data, {"name", "description", "status", "owner", "start_date", "due_date", "custom_fields"}, partial=True)
    c.text("name", min_len=1, max_len=100)
    c.text("description", max_len=5000)
    c.choice("status", PROJECT_STATUSES)
    c.raw("owner")
    c.date("start_date")
    c.date("due_date")
    c.raw("custom_fields")
    v, custom = _validated(ctx, c, creating=False)
    _check_dates(v.get("start_date", row["start_date"]), v.get("due_date", row["due_date"]))

    if "owner" in v:
        if v["owner"] is None:
            v["owner_id"] = None
        else:
            owner = _active_user(ctx.conn, v["owner"], "owner")
            v["owner_id"] = owner["id"]
            if not is_member(ctx.conn, row["id"], owner["id"]):
                ctx.conn.execute(
                    "INSERT INTO project_members (project_id, user_id) VALUES (?,?)", (row["id"], owner["id"])
                )
        del v["owner"]

    if v.get("status") == "completed" and row["status"] != "completed":
        open_count = ctx.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND status IN %s" % OPEN_SQL, (row["id"],)
        ).fetchone()[0]
        if open_count:
            raise Conflict(
                "Cannot complete project: %d task(s) are still open. Close or cancel them first." % open_count,
                code="open_tasks_remaining",
                details={"open_tasks": open_count},
            )

    changes = {f: [row[f], val] for f, val in v.items() if row[f] != val}
    custom_changes = project_fields.store(ctx, row["id"], custom)
    if changes or custom_changes:
        if changes:
            sets = ", ".join("%s = ?" % f for f in changes)
            ctx.conn.execute(
                "UPDATE projects SET %s, updated_at = ? WHERE id = ?" % sets,
                [c_[1] for c_ in changes.values()] + [ctx.now_iso, row["id"]],
            )
        else:
            ctx.conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (ctx.now_iso, row["id"]))
        activity.log(ctx, "project.updated", project_id=row["id"], details={"changes": {**changes, **custom_changes}})
        if "status" in changes:
            ctx.emit(
                "project_status_changed",
                project=row["key"],
                name=row["name"],
                old=changes["status"][0],
                new=changes["status"][1],
                actor=ctx.actor_name,
            )
    return get_project(ctx, key)


def delete_project(ctx, key, confirm):
    row = get_row(ctx.conn, key)
    if confirm != row["key"]:
        raise Conflict(
            "Deleting a project is permanent. Repeat the request with ?confirm=%s" % row["key"],
            code="confirmation_required",
        )
    ctx.conn.execute("DELETE FROM projects WHERE id = ?", (row["id"],))
    activity.log(ctx, "project.deleted", details={"key": row["key"], "name": row["name"]})


def add_member(ctx, key, username):
    row = get_row(ctx.conn, key)
    user = _active_user(ctx.conn, username, "username")
    if not is_member(ctx.conn, row["id"], user["id"]):
        ctx.conn.execute("INSERT INTO project_members (project_id, user_id) VALUES (?,?)", (row["id"], user["id"]))
        activity.log(ctx, "project.member_added", project_id=row["id"], details={"user": user["username"]})
    return get_project(ctx, key)


def remove_member(ctx, key, username):
    row = get_row(ctx.conn, key)
    user = users.find_by_username(ctx.conn, username)
    if user is None or not is_member(ctx.conn, row["id"], user["id"]):
        raise NotFound("%s is not a member of %s" % (username, row["key"]), code="member_not_found")
    if row["owner_id"] == user["id"]:
        raise Conflict("The project owner cannot be removed. Change the owner first.", code="owner_is_member")
    open_assigned = ctx.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE project_id = ? AND assignee_id = ? AND status IN %s" % OPEN_SQL,
        (row["id"], user["id"]),
    ).fetchone()[0]
    if open_assigned:
        raise Conflict(
            "%s still has %d open task(s) in this project. Reassign them first." % (user["username"], open_assigned),
            code="member_has_open_tasks",
            details={"open_tasks": open_assigned},
        )
    ctx.conn.execute("DELETE FROM project_members WHERE project_id = ? AND user_id = ?", (row["id"], user["id"]))
    activity.log(ctx, "project.member_removed", project_id=row["id"], details={"user": user["username"]})
    return get_project(ctx, key)
