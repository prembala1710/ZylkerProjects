"""Watchers: people who follow a task without being its assignee.

Anyone signed in may follow or unfollow *themselves* (even a viewer - following changes nothing about the task itself).
Adding somebody else needs write access to the project. Archived projects are read-only, so nobody can start following
a task there, although anyone can still stop.

Watchers are named in the Zoho Cliq message about the change they asked to follow (see ``names_to_notify``): the Cliq
webhook posts to one channel, so this is a mention-by-name, not a private message.
"""

from __future__ import annotations

from ..domain import MAX_WATCHERS
from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker
from . import activity, projects, taskrefs, users

_ACTIVE_WATCHERS = (
    "SELECT u.id, u.username, u.display_name FROM task_watchers w JOIN users u ON u.id = w.user_id "
    "WHERE w.task_id = ? AND u.active = 1 ORDER BY u.display_name COLLATE NOCASE, u.id"
)


def list_for(conn, task_id):
    return [users.ref(r["id"], r["username"], r["display_name"]) for r in conn.execute(_ACTIVE_WATCHERS, (task_id,))]


def is_watching(conn, task_id, user_id):
    return conn.execute("SELECT 1 FROM task_watchers WHERE task_id = ? AND user_id = ?", (task_id, user_id)).fetchone() is not None


def payload(ctx, task_id):
    return {
        "items": list_for(ctx.conn, task_id),
        "watching": ctx.user is not None and is_watching(ctx.conn, task_id, ctx.user["id"]),
    }


def names_to_notify(ctx, task_id, exclude=()):
    """Display names of active watchers, leaving out the person acting and anyone already told another way."""
    skip = {ctx.actor_id, *exclude}
    return [r["display_name"] for r in ctx.conn.execute(_ACTIVE_WATCHERS, (task_id,)) if r["id"] not in skip]


def list_watchers(ctx, key):
    return payload(ctx, taskrefs.get_row(ctx.conn, key)["id"])


def _target(ctx, data):
    c = Checker(data, {"username"})
    c.text("username", min_len=1, max_len=32)
    name = c.result().get("username")
    if name is None:
        if ctx.user is None:
            raise ValidationError({"username": "required when there is no signed-in user"})
        return ctx.user
    user = users.find_by_username(ctx.conn, name)
    if user is None or not user["active"]:
        raise ValidationError({"username": "unknown or inactive user %r" % name})
    return user


def add_watcher(ctx, key, data):
    """Body: ``{"username": "bob"}``, or ``{}`` to follow the task yourself. Idempotent."""
    conn = ctx.conn
    row = taskrefs.get_row(conn, key)
    project = projects.get_row(conn, row["project_key"])
    target = _target(ctx, data)
    myself = ctx.user is not None and target["id"] == ctx.user["id"]
    if not myself:
        projects.require_write(ctx, project)
    elif project["status"] == "archived":
        raise Conflict("Project %s is archived and read-only." % project["key"], code="project_archived")
    if not is_watching(conn, row["id"], target["id"]):
        count = conn.execute("SELECT COUNT(*) FROM task_watchers WHERE task_id = ?", (row["id"],)).fetchone()[0]
        if count >= MAX_WATCHERS:
            raise Conflict("A task can have at most %d watchers" % MAX_WATCHERS, code="too_many_watchers")
        conn.execute(
            "INSERT INTO task_watchers (task_id, user_id, added_by, created_at) VALUES (?,?,?,?)",
            (row["id"], target["id"], ctx.actor_id, ctx.now_iso),
        )
        activity.log(
            ctx, "watcher.added", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
            details={"user": target["username"], "self": myself},
        )  # fmt: skip
    return payload(ctx, row["id"])


def remove_watcher(ctx, key, username):
    conn = ctx.conn
    row = taskrefs.get_row(conn, key)
    project = projects.get_row(conn, row["project_key"])
    target = users.find_by_username(conn, username)
    if target is None or not is_watching(conn, row["id"], target["id"]):
        raise NotFound("%s is not watching %s" % (username, taskrefs.key_of(row)), code="watcher_not_found")
    myself = ctx.user is not None and target["id"] == ctx.user["id"]
    if not myself:
        projects.require_write(ctx, project)
    conn.execute("DELETE FROM task_watchers WHERE task_id = ? AND user_id = ?", (row["id"], target["id"]))
    activity.log(
        ctx, "watcher.removed", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
        details={"user": target["username"], "self": myself},
    )  # fmt: skip
