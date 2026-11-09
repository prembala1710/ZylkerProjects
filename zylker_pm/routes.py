"""HTTP handlers. Each one is a thin adapter over a service function."""

from __future__ import annotations

import re

from . import __version__
from .app import Raw, route
from .domain import (
    BULK_MAX_TASKS,
    CLOSED_STATUSES,
    DEFAULT_DEPENDENCY_TYPE,
    DEPENDENCY_TYPES,
    LINK_TYPES,
    MAX_CHECKLIST_ITEMS,
    MAX_CHECKLIST_TEXT,
    MAX_LAG_DAYS,
    MAX_TASK_DEPTH,
    MAX_WATCHERS,
    OPEN_STATUSES,
    PRIORITIES,
    PROJECT_STATUSES,
    ROLES,
    STATUSES,
    TASK_TYPES,
    TRANSITIONS,
)
from .errors import NotFound, ValidationError
from .services import (
    activity,
    bulk,
    checklist,
    collab,
    csvio,
    deps,
    links,
    milestones,
    project_fields,
    projects,
    reports,
    taskrefs,
    tasks,
    users,
    watchers,
)
from .util import truthy


def _id(value, what):
    try:
        return int(value)
    except ValueError:
        raise NotFound("%s not found" % what.capitalize(), code="%s_not_found" % what) from None


def _int_param(req, name, default, minimum, maximum):
    raw = req.query.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: "must be an integer"}) from None
    if not minimum <= value <= maximum:
        raise ValidationError({name: "must be between %d and %d" % (minimum, maximum)})
    return value


# --------------------------------------------------------------------------- system


@route("GET", "/api/health", public=True)
def health(ctx, req):
    ctx.conn.execute("SELECT 1")
    return {"status": "ok", "version": __version__}


@route("GET", "/api/meta")
def meta(ctx, req):
    return {
        "version": __version__,
        "timezone": ctx.config.timezone,
        "today": ctx.today.isoformat(),
        "statuses": list(STATUSES),
        "open_statuses": list(OPEN_STATUSES),
        "closed_statuses": list(CLOSED_STATUSES),
        "priorities": list(PRIORITIES),
        "task_types": list(TASK_TYPES),
        "project_statuses": list(PROJECT_STATUSES),
        "roles": list(ROLES),
        "transitions": {k: list(v) for k, v in TRANSITIONS.items()},
        "max_task_depth": MAX_TASK_DEPTH,
        "project_field_types": list(project_fields.FIELD_TYPES),
        "project_field_limits": project_fields.LIMITS,
        "dependency_types": list(DEPENDENCY_TYPES),
        "default_dependency_type": DEFAULT_DEPENDENCY_TYPE,
        "max_lag_days": MAX_LAG_DAYS,
        "link_types": list(LINK_TYPES),
        "max_checklist_items": MAX_CHECKLIST_ITEMS,
        "max_checklist_text": MAX_CHECKLIST_TEXT,
        "max_watchers": MAX_WATCHERS,
        "bulk_max_tasks": BULK_MAX_TASKS,
        "bulk_fields": list(bulk.CHANGE_FIELDS),
    }


@route("GET", "/api/me")
def me(ctx, req):
    return users.public(ctx.user)


# ---------------------------------------------------------------------------- users


@route("GET", "/api/users")
def list_users(ctx, req):
    everyone = truthy(req.query.get("include_inactive")) and ctx.has_role("admin")
    return {"items": users.list_users(ctx, active=None if everyone else True)}


@route("POST", "/api/users", role="admin")
def create_user(ctx, req):
    user, token = users.create_user(ctx, req.json())
    return 201, {"user": user, "token": token}


@route("PATCH", "/api/users/{user_id}")
def update_user(ctx, req, user_id):
    return users.update_user(ctx, _id(user_id, "user"), req.json())


@route("POST", "/api/users/{user_id}/rotate-token")
def rotate_token(ctx, req, user_id):
    return {"token": users.rotate_token(ctx, _id(user_id, "user"))}


@route("GET", "/api/activity", role="admin")
def global_activity(ctx, req):
    return activity.list_activity(ctx, limit=req.query.get("limit", 50), before=req.query.get("before"))


# ------------------------------------------------------------------------- projects


@route("GET", "/api/projects")
def list_projects(ctx, req):
    return {"items": projects.list_projects(ctx, req.query)}


@route("POST", "/api/projects", role="manager")
def create_project(ctx, req):
    return 201, projects.create_project(ctx, req.json())


@route("GET", "/api/projects/{key}")
def get_project(ctx, req, key):
    return projects.get_project(ctx, key)


@route("PATCH", "/api/projects/{key}", role="manager")
def update_project(ctx, req, key):
    return projects.update_project(ctx, key, req.json())


@route("DELETE", "/api/projects/{key}", role="admin")
def delete_project(ctx, req, key):
    projects.delete_project(ctx, key, req.query.get("confirm"))
    return 204, None


@route("POST", "/api/projects/{key}/members", role="manager")
def add_member(ctx, req, key):
    body = req.json()
    return projects.add_member(ctx, key, body.get("username") if isinstance(body, dict) else None)


@route("DELETE", "/api/projects/{key}/members/{username}", role="manager")
def remove_member(ctx, req, key, username):
    return projects.remove_member(ctx, key, username)


@route("GET", "/api/projects/{key}/summary")
def project_summary(ctx, req, key):
    return reports.project_summary(ctx, key)


@route("GET", "/api/projects/{key}/board")
def project_board(ctx, req, key):
    return tasks.board(ctx, key, _int_param(req, "closed_days", 14, 0, 3650))


@route("GET", "/api/projects/{key}/critical-path")
def project_critical_path(ctx, req, key):
    return reports.critical_path(ctx, key)


@route("GET", "/api/projects/{key}/burndown")
def project_burndown(ctx, req, key):
    return reports.burndown(ctx, key, req.query)


@route("GET", "/api/projects/{key}/activity")
def project_activity(ctx, req, key):
    row = projects.get_row(ctx.conn, key)
    return activity.list_activity(ctx, project_id=row["id"], limit=_int_param(req, "limit", 50, 1, 200), before=req.query.get("before"))


@route("GET", "/api/projects/{key}/milestones")
def list_milestones(ctx, req, key):
    return {"items": milestones.list_milestones(ctx, key)}


@route("POST", "/api/projects/{key}/milestones", role="manager")
def create_milestone(ctx, req, key):
    return 201, milestones.create_milestone(ctx, key, req.json())


@route("GET", "/api/projects/{key}/export.csv")
def export_csv(ctx, req, key):
    text = csvio.export_csv(ctx, key)
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", key.upper()) + "-tasks.csv"
    return Raw(
        text.encode("utf-8-sig"),
        "text/csv; charset=utf-8",
        headers=[("Content-Disposition", 'attachment; filename="%s"' % filename)],
    )


@route("POST", "/api/projects/{key}/import", role="manager", body_limit="max_import_bytes")
def import_csv(ctx, req, key):
    return csvio.import_csv(
        ctx,
        key,
        req.text(),
        dry_run=truthy(req.query.get("dry_run")),
        add_missing_members=truthy(req.query.get("add_missing_members")),
    )


@route("POST", "/api/projects/{key}/tasks", role="member")
def create_task(ctx, req, key):
    return 201, tasks.create_task(ctx, key, req.json())


# ------------------------------------------------------------------- project fields


@route("GET", "/api/project-fields")
def list_project_fields(ctx, req):
    return {"items": project_fields.list_fields(ctx)}


@route("POST", "/api/project-fields", role="admin")
def create_project_field(ctx, req):
    return 201, project_fields.create_field(ctx, req.json())


@route("PATCH", "/api/project-fields/{field_key}", role="admin")
def update_project_field(ctx, req, field_key):
    return project_fields.update_field(ctx, field_key, req.json())


@route("DELETE", "/api/project-fields/{field_key}", role="admin")
def delete_project_field(ctx, req, field_key):
    project_fields.delete_field(ctx, field_key, req.query.get("confirm"))
    return 204, None


# ---------------------------------------------------------------------------- tasks


@route("GET", "/api/tasks")
def list_tasks(ctx, req):
    return tasks.list_tasks(ctx, req.query)


@route("POST", "/api/tasks/bulk", role="member")
def bulk_update(ctx, req):
    return bulk.bulk_update(ctx, req.json())


@route("GET", "/api/tasks/{key}")
def get_task(ctx, req, key):
    return tasks.get_task(ctx, key)


@route("PATCH", "/api/tasks/{key}", role="member")
def update_task(ctx, req, key):
    return tasks.update_task(ctx, key, req.json())


@route("DELETE", "/api/tasks/{key}", role="manager")
def delete_task(ctx, req, key):
    tasks.delete_task(ctx, key, cascade=truthy(req.query.get("cascade")))
    return 204, None


@route("GET", "/api/tasks/{key}/activity")
def task_activity(ctx, req, key):
    row = taskrefs.get_row(ctx.conn, key)
    return activity.list_activity(ctx, task_id=row["id"], limit=_int_param(req, "limit", 50, 1, 200), before=req.query.get("before"))


@route("POST", "/api/tasks/{key}/dependencies", role="member")
def add_dependency(ctx, req, key):
    return deps.add_dependency(ctx, key, req.json())


@route("PATCH", "/api/tasks/{key}/dependencies/{other}", role="member")
def update_dependency(ctx, req, key, other):
    return deps.update_dependency(ctx, key, other, req.json())


@route("DELETE", "/api/tasks/{key}/dependencies/{other}", role="member")
def remove_dependency(ctx, req, key, other):
    return deps.remove_dependency(ctx, key, other)


@route("POST", "/api/tasks/{key}/links", role="member")
def add_link(ctx, req, key):
    links.add_link(ctx, key, req.json())
    return 201, tasks.get_task(ctx, key)


@route("DELETE", "/api/tasks/{key}/links/{other}", role="member")
def remove_link(ctx, req, key, other):
    links.remove_link(ctx, key, other)
    return tasks.get_task(ctx, key)


@route("GET", "/api/tasks/{key}/watchers")
def list_watchers(ctx, req, key):
    return watchers.list_watchers(ctx, key)


@route("POST", "/api/tasks/{key}/watchers")
def add_watcher(ctx, req, key):
    return watchers.add_watcher(ctx, key, req.json())


@route("DELETE", "/api/tasks/{key}/watchers/{username}")
def remove_watcher(ctx, req, key, username):
    watchers.remove_watcher(ctx, key, username)
    return 204, None


@route("GET", "/api/tasks/{key}/checklist")
def list_checklist(ctx, req, key):
    return {"items": checklist.list_items(ctx, key)}


@route("POST", "/api/tasks/{key}/checklist", role="member")
def add_checklist_item(ctx, req, key):
    return 201, checklist.add_item(ctx, key, req.json())


@route("PATCH", "/api/checklist/{item_id}", role="member")
def update_checklist_item(ctx, req, item_id):
    return checklist.update_item(ctx, _id(item_id, "checklist_item"), req.json())


@route("DELETE", "/api/checklist/{item_id}", role="member")
def delete_checklist_item(ctx, req, item_id):
    checklist.delete_item(ctx, _id(item_id, "checklist_item"))
    return 204, None


@route("GET", "/api/tasks/{key}/comments")
def list_comments(ctx, req, key):
    return {"items": collab.list_comments(ctx, key)}


@route("POST", "/api/tasks/{key}/comments", role="member")
def add_comment(ctx, req, key):
    return 201, collab.add_comment(ctx, key, req.json())


@route("PATCH", "/api/comments/{comment_id}", role="member")
def edit_comment(ctx, req, comment_id):
    return collab.edit_comment(ctx, _id(comment_id, "comment"), req.json())


@route("DELETE", "/api/comments/{comment_id}", role="member")
def delete_comment(ctx, req, comment_id):
    collab.delete_comment(ctx, _id(comment_id, "comment"))
    return 204, None


@route("GET", "/api/tasks/{key}/time")
def list_time(ctx, req, key):
    return {"items": collab.list_time(ctx, key)}


@route("POST", "/api/tasks/{key}/time", role="member")
def log_time(ctx, req, key):
    return 201, collab.log_time(ctx, key, req.json())


@route("DELETE", "/api/time/{entry_id}", role="member")
def delete_time(ctx, req, entry_id):
    collab.delete_time(ctx, _id(entry_id, "time_entry"))
    return 204, None


# ----------------------------------------------------------------------- milestones


@route("PATCH", "/api/milestones/{milestone_id}", role="manager")
def update_milestone(ctx, req, milestone_id):
    return milestones.update_milestone(ctx, _id(milestone_id, "milestone"), req.json())


@route("DELETE", "/api/milestones/{milestone_id}", role="manager")
def delete_milestone(ctx, req, milestone_id):
    milestones.delete_milestone(ctx, _id(milestone_id, "milestone"))
    return 204, None


# -------------------------------------------------------------------------- reports


@route("GET", "/api/reports/portfolio")
def report_portfolio(ctx, req):
    return reports.portfolio(ctx)


@route("GET", "/api/reports/workload")
def report_workload(ctx, req):
    return reports.workload(ctx, req.query)


@route("GET", "/api/reports/timesheet")
def report_timesheet(ctx, req):
    return reports.timesheet(ctx, req.query)


@route("GET", "/api/reports/digest")
def report_digest(ctx, req):
    return reports.digest(ctx, req.query)
