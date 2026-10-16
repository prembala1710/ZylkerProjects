"""Comments (with @mentions) and time tracking."""

from __future__ import annotations

import re
from datetime import timedelta

from ..errors import Forbidden, NotFound, ValidationError
from ..util import Checker, truncate
from . import activity, projects, taskrefs, users

MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9._-]{1,31})", re.IGNORECASE)


def _task_and_project(ctx, key):
    row = taskrefs.get_row(ctx.conn, key)
    project = projects.get_row(ctx.conn, row["project_key"])
    return row, project


# ------------------------------------------------------------------------- comments


def _comment_dict(row):
    return {
        "id": row["id"],
        "task": "%s-%d" % (row["project_key"], row["number"]),
        "author": users.ref(row["author_id"], row["username"], row["display_name"]),
        "body": row["body"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
    }


COMMENT_SELECT = (
    "SELECT c.*, u.username, u.display_name, t.number, p.key AS project_key FROM comments c "
    "JOIN users u ON u.id = c.author_id JOIN tasks t ON t.id = c.task_id JOIN projects p ON p.id = t.project_id "
)


def list_comments(ctx, key):
    row = taskrefs.get_row(ctx.conn, key)
    rows = ctx.conn.execute(COMMENT_SELECT + "WHERE c.task_id = ? ORDER BY c.id", (row["id"],))
    return [_comment_dict(r) for r in rows]


def _body(data, partial=False):
    c = Checker(data, {"body"}, partial=partial)
    c.text("body", required=True, min_len=1, max_len=10000)
    return c.result()["body"]


def mentioned_users(conn, body):
    found = {}
    for name in MENTION_RE.findall(body):
        user = users.find_by_username(conn, name)
        if user is not None and user["active"]:
            found[user["id"]] = user
    return list(found.values())


def add_comment(ctx, key, data):
    row, project = _task_and_project(ctx, key)
    projects.require_write(ctx, project)
    body = _body(data)
    cur = ctx.conn.execute(
        "INSERT INTO comments (task_id, author_id, body, created_at) VALUES (?,?,?,?)",
        (row["id"], ctx.actor_id, body, ctx.now_iso),
    )
    mentions = [u for u in mentioned_users(ctx.conn, body) if u["id"] != ctx.actor_id]
    activity.log(
        ctx, "comment.added", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
        details={"comment_id": cur.lastrowid, "mentions": [u["username"] for u in mentions]},
    )  # fmt: skip
    for user in mentions:
        ctx.emit(
            "mention", key=taskrefs.key_of(row), title=row["title"], mentioned=user["display_name"],
            actor=ctx.actor_name, excerpt=truncate(body, 140),
        )  # fmt: skip
    comment = ctx.conn.execute(COMMENT_SELECT + "WHERE c.id = ?", (cur.lastrowid,)).fetchone()
    return _comment_dict(comment)


def _comment_row(conn, comment_id):
    row = conn.execute(COMMENT_SELECT + "WHERE c.id = ?", (comment_id,)).fetchone()
    if row is None:
        raise NotFound("Comment not found", code="comment_not_found")
    return row


def edit_comment(ctx, comment_id, data):
    row = _comment_row(ctx.conn, comment_id)
    if ctx.user is not None and ctx.user["id"] != row["author_id"]:
        raise Forbidden("Only the author can edit a comment", code="not_author")
    task = taskrefs.get_row(ctx.conn, "%s-%d" % (row["project_key"], row["number"]))
    projects.require_write(ctx, projects.get_row(ctx.conn, row["project_key"]))
    body = _body(data)
    if body != row["body"]:
        ctx.conn.execute("UPDATE comments SET body = ?, edited_at = ? WHERE id = ?", (body, ctx.now_iso, row["id"]))
        activity.log(
            ctx, "comment.edited", project_id=task["project_id"], task_id=task["id"],
            task_key=taskrefs.key_of(task), details={"comment_id": row["id"]},
        )  # fmt: skip
    return _comment_dict(_comment_row(ctx.conn, comment_id))


def delete_comment(ctx, comment_id):
    row = _comment_row(ctx.conn, comment_id)
    if ctx.user is not None and ctx.user["id"] != row["author_id"] and not ctx.has_role("manager"):
        raise Forbidden("Only the author or a manager can delete a comment", code="not_author")
    task = taskrefs.get_row(ctx.conn, "%s-%d" % (row["project_key"], row["number"]))
    projects.require_write(ctx, projects.get_row(ctx.conn, row["project_key"]))
    ctx.conn.execute("DELETE FROM comments WHERE id = ?", (row["id"],))
    activity.log(
        ctx, "comment.deleted", project_id=task["project_id"], task_id=task["id"],
        task_key=taskrefs.key_of(task), details={"comment_id": row["id"]},
    )  # fmt: skip


# ----------------------------------------------------------------------- time entries

TIME_SELECT = (
    "SELECT e.*, u.username, u.display_name, t.number, p.key AS project_key FROM time_entries e "
    "JOIN users u ON u.id = e.user_id JOIN tasks t ON t.id = e.task_id JOIN projects p ON p.id = t.project_id "
)


def time_dict(row):
    return {
        "id": row["id"],
        "task": "%s-%d" % (row["project_key"], row["number"]),
        "user": users.ref(row["user_id"], row["username"], row["display_name"]),
        "hours": row["hours"],
        "work_date": row["work_date"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def list_time(ctx, key):
    row = taskrefs.get_row(ctx.conn, key)
    rows = ctx.conn.execute(TIME_SELECT + "WHERE e.task_id = ? ORDER BY e.work_date DESC, e.id DESC", (row["id"],))
    return [time_dict(r) for r in rows]


def log_time(ctx, key, data):
    row, project = _task_and_project(ctx, key)
    projects.require_write(ctx, project)
    if ctx.user is None:
        raise ValidationError({"user": "time can only be logged by a signed-in user"})
    c = Checker(data, {"hours", "work_date", "note"})
    c.number("hours", required=True, minimum=0.01, maximum=24)
    c.date("work_date", nullable=False)
    c.text("note", max_len=500)
    v = c.result()
    work_date = v.get("work_date", ctx.today.isoformat())
    latest = (ctx.today + timedelta(days=1)).isoformat()
    if work_date > latest or work_date < "2000-01-01":
        raise ValidationError({"work_date": "must not be in the future"})
    cur = ctx.conn.execute(
        "INSERT INTO time_entries (task_id, user_id, hours, work_date, note, created_at) VALUES (?,?,?,?,?,?)",
        (row["id"], ctx.actor_id, v["hours"], work_date, v.get("note", ""), ctx.now_iso),
    )
    activity.log(
        ctx, "time.logged", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
        details={"hours": v["hours"], "work_date": work_date},
    )  # fmt: skip
    return time_dict(ctx.conn.execute(TIME_SELECT + "WHERE e.id = ?", (cur.lastrowid,)).fetchone())


def delete_time(ctx, entry_id):
    row = ctx.conn.execute(TIME_SELECT + "WHERE e.id = ?", (entry_id,)).fetchone()
    if row is None:
        raise NotFound("Time entry not found", code="time_entry_not_found")
    if ctx.user is not None and ctx.user["id"] != row["user_id"] and not ctx.has_role("manager"):
        raise Forbidden("Only the owner of a time entry or a manager can delete it", code="not_author")
    task = taskrefs.get_row(ctx.conn, "%s-%d" % (row["project_key"], row["number"]))
    projects.require_write(ctx, projects.get_row(ctx.conn, row["project_key"]))
    ctx.conn.execute("DELETE FROM time_entries WHERE id = ?", (row["id"],))
    activity.log(
        ctx, "time.deleted", project_id=task["project_id"], task_id=task["id"], task_key=taskrefs.key_of(task),
        details={"hours": row["hours"], "work_date": row["work_date"]},
    )  # fmt: skip
