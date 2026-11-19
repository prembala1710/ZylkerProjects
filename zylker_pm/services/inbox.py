"""The notification inbox: what happened that concerns *you*, kept per person and read at your own pace.

Every change to data already writes an audit event (``activity.log``). This module turns those events into
notifications: ``fan_out`` is called from there, inside the same transaction, so a notification exists if and only if
the change was committed, whichever code path made it (API, CSV import, recurring tasks, templates, the CLI).

Who hears about an event:

* **Watchers.** A person *watches* a task - on purpose, or automatically because they created it, were assigned to it,
  commented on it or were mentioned in it - and then hears about its comments, status changes and edits. They can
  *ignore* a task instead, which silences everything about it except being mentioned. A task they never touched and
  never chose is silent. A person can also watch a whole project (``all`` events, or only the ``important`` ones).
* **Directly.** The person mentioned in a comment, the person a task is assigned to, and the owner of a timesheet
  that was approved, sent back or reopened are told whatever they watch.
* Never the person who did it, unless they ask for that (``self_notify``).

Rules that keep the inbox usable:

* One event reaches a person at most once, as the most important reason that applies (mention, then assignment, ...).
* A burst does not become a pile: comments, status changes and edits on the same task - and tasks created in the same
  project - merge into one *unread* item with a count while it stays unread and recent. Mentions, assignments and
  decisions are never merged.
* An inbox is bounded: read items go after 30 days, and no one keeps more than 500 items (read ones go first).
* A failure in here never fails the change that caused it.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker, iso, truncate, truthy
from . import taskrefs, users

log = logging.getLogger("zylker.inbox")

# Most important first: when one event qualifies for several reasons, the first kind in this list wins.
KINDS = ("mention", "assigned", "timesheet", "status_changed", "comment", "updated", "task_created", "sprint", "project")
_RANK = {kind: i for i, kind in enumerate(KINDS)}
KIND_LABELS = {
    "mention": "You are mentioned", "assigned": "A task is assigned to you", "timesheet": "A decision on your timesheet",
    "status_changed": "A watched task changes status", "comment": "A watched task gets a comment",
    "updated": "A watched task is edited", "task_created": "A task is created in a watched project",
    "sprint": "A sprint starts or closes in a watched project", "project": "A watched project changes status",
}  # fmt: skip
MERGEABLE = ("assigned", "comment", "status_changed", "updated", "task_created")
IMPORTANT_STATUSES = ("done", "blocked")
PROJECT_LEVELS = ("all", "important")
WATCH_STATES = ("watching", "ignoring")

COALESCE_SECONDS = 600
MAX_ITEMS = 500
READ_KEEP_DAYS = 30
MAX_RECIPIENTS = 500
MAX_WATCHES = 1000
MAX_PAGE = 100
MAX_MARK = 200
MAX_ACTORS = 5
MAX_LISTED_WATCHERS = 200

DEFAULT_SETTINGS = {"kinds": dict.fromkeys(KINDS, True), "self_notify": False}


# ------------------------------------------------------------------------------ settings


def _settings_for(conn, user_ids):
    """``{user_id: settings}`` with the defaults filled in."""
    out = {uid: {"kinds": dict(DEFAULT_SETTINGS["kinds"]), "self_notify": False} for uid in user_ids}
    ids = list(out)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        for r in conn.execute("SELECT user_id, settings FROM notification_settings WHERE user_id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            stored = json.loads(r["settings"] or "{}")
            out[r["user_id"]]["kinds"].update({k: bool(v) for k, v in stored.get("kinds", {}).items() if k in _RANK})
            out[r["user_id"]]["self_notify"] = bool(stored.get("self_notify", False))
    return out


def get_settings(ctx):
    user = _me(ctx)
    return {**_settings_for(ctx.conn, [user["id"]])[user["id"]], "labels": KIND_LABELS}


def update_settings(ctx, data):
    user = _me(ctx)
    c = Checker(data, {"kinds", "self_notify"})
    c.boolean("self_notify")
    c.raw("kinds")
    v = c.result()
    errors = {}
    kinds = v.get("kinds")
    if "kinds" in v:
        if not isinstance(kinds, dict):
            errors["kinds"] = "must be an object of kind: true|false"
        else:
            for name, value in kinds.items():
                if name not in _RANK:
                    errors["kinds.%s" % name] = "unknown kind; allowed: %s" % ", ".join(KINDS)
                elif not isinstance(value, bool):
                    errors["kinds.%s" % name] = "must be true or false"
    if errors:
        raise ValidationError(errors)
    current = _settings_for(ctx.conn, [user["id"]])[user["id"]]
    if kinds:
        current["kinds"].update(kinds)
    if "self_notify" in v:
        current["self_notify"] = v["self_notify"]
    ctx.conn.execute(
        "INSERT INTO notification_settings (user_id, settings, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET settings = excluded.settings, updated_at = excluded.updated_at",
        (user["id"], json.dumps(current), ctx.now_iso),
    )
    return get_settings(ctx)


# ------------------------------------------------------------------------------ fan-out


def fan_out(ctx, event_id, action, project_id, project_key, task_id, task_key, details):
    """Called by ``activity.log`` for every event. Never raises: notifications are never worth failing a change for."""
    if action not in _ACTIONS:
        return
    conn = ctx.conn
    conn.execute("SAVEPOINT inbox_fan_out")
    try:
        _fan_out(ctx, event_id, action, project_id, project_key, task_id, task_key, details or {})
    except Exception:  # noqa: BLE001 - see the docstring
        conn.execute("ROLLBACK TO inbox_fan_out")
        log.exception("could not create notifications for %s", action)
    finally:
        conn.execute("RELEASE inbox_fan_out")


def _fan_out(ctx, event_id, action, project_id, project_key, task_id, task_key, d):
    conn = ctx.conn
    reasons = {}  # user id -> (kind, reason)
    extra = {}
    title = None
    excerpt = ""

    def add(uid, kind, reason):
        if uid is None:
            return
        current = reasons.get(uid)
        if current is None or _RANK[kind] < _RANK[current[0]]:
            reasons[uid] = (kind, reason)

    def named(username):
        row = users.find_by_username(conn, username) if isinstance(username, str) else None
        return row["id"] if row is not None else None

    watching_task, ignoring, project_watchers = set(), set(), {}
    if task_id is not None:
        for r in conn.execute("SELECT user_id, state FROM task_watches WHERE task_id = ? LIMIT ?", (task_id, MAX_RECIPIENTS * 2)):
            (watching_task if r["state"] == "watching" else ignoring).add(r["user_id"])
    if project_id is not None:
        for r in conn.execute("SELECT user_id, level FROM project_watches WHERE project_id = ? LIMIT ?", (project_id, MAX_RECIPIENTS * 2)):
            project_watchers[r["user_id"]] = r["level"]

    def project_audience(kind, important):
        for uid, level in project_watchers.items():
            if level == "all" or important:
                add(uid, kind, "project")

    auto_watch = []
    if action == "comment.added":
        for name in d.get("mentions") or []:
            uid = named(name)
            add(uid, "mention", "mentioned")
            auto_watch.append(uid)
        auto_watch.append(ctx.actor_id)
        for uid in watching_task:
            add(uid, "comment", "watching")
        project_audience("comment", False)
        row = conn.execute("SELECT body FROM comments WHERE id = ?", (d.get("comment_id"),)).fetchone()
        excerpt = truncate(row["body"], 140) if row else ""
    elif action == "task.created":
        assignee = named(d.get("assignee"))
        add(assignee, "assigned", "assigned")
        auto_watch += [ctx.actor_id, assignee]
        project_audience("task_created", False)
        title = d.get("title")
    elif action in ("task.updated", "task.status_changed"):
        changes = d.get("changes") or {}
        new_assignee = changes.get("assignee", [None, None])[1] if isinstance(changes.get("assignee"), list) else None
        if new_assignee:
            uid = named(new_assignee)
            add(uid, "assigned", "assigned")
            auto_watch.append(uid)
        others = sorted(k for k in changes if k != "assignee")
        if action == "task.status_changed":
            old, new = changes["status"]
            extra["status"] = [old, new]
            for uid in watching_task:
                add(uid, "status_changed", "watching")
            project_audience("status_changed", new in IMPORTANT_STATUSES)
        elif others:
            extra["fields"] = others
            for uid in watching_task:
                add(uid, "updated", "watching")
            project_audience("updated", False)
    elif action in ("sprint.started", "sprint.closed"):
        extra.update(verb=action.split(".")[1], sprint=d.get("name"))
        project_audience("sprint", True)
    elif action == "project.updated":
        status = (d.get("changes") or {}).get("status")
        if status:
            extra["status"] = status
            project_audience("project", True)
    else:  # timesheet.approved / rejected / reopened
        add(named(d.get("user")), "timesheet", "owner")
        extra.update(decision=action.split(".")[1], week=d.get("week_start"), hours=d.get("hours"), note=truncate(d.get("note") or "", 140))

    for uid in auto_watch:
        _auto_watch(ctx, uid, task_id)
    if not reasons:
        return

    # who is actually told: active people, not the actor (unless they asked), who left the kind switched on, and who
    # have not ignored the task (a mention still gets through)
    rows = {r["id"]: r for r in conn.execute("SELECT id, active FROM users WHERE id IN (%s)" % ",".join("?" * len(reasons)), list(reasons))}
    settings = _settings_for(conn, [uid for uid in reasons if uid in rows])
    audience = []
    for uid, (kind, reason) in sorted(reasons.items()):
        row = rows.get(uid)
        if row is None or not row["active"]:
            continue
        if uid == ctx.actor_id and not settings[uid]["self_notify"]:
            continue
        if not settings[uid]["kinds"][kind]:
            continue
        if uid in ignoring and kind != "mention":
            continue
        audience.append((uid, kind, reason))
    if not audience:
        return
    audience = audience[:MAX_RECIPIENTS]

    if task_id is not None and title is None:
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        title = row["title"] if row else None
    if title is None and project_key:
        row = conn.execute("SELECT name FROM projects WHERE key = ?", (project_key,)).fetchone()
        title = row["name"] if row else project_key
    base = {
        "event_id": event_id, "actor_id": ctx.actor_id, "actor_name": truncate(ctx.actor_name, 100), "project_key": project_key,
        "task_key": task_key, "title": truncate(title or "", 200), "excerpt": excerpt, "extra": extra,
    }  # fmt: skip
    for uid, kind, reason in audience:
        _deliver(ctx, uid, kind, reason, base)


def _auto_watch(ctx, user_id, task_id):
    """Watching because of what you did; never overrides a choice (``ignoring``, or a manual watch)."""
    if user_id is None or task_id is None:
        return
    conn = ctx.conn
    if conn.execute("SELECT COUNT(*) FROM task_watches WHERE user_id = ?", (user_id,)).fetchone()[0] >= MAX_WATCHES:
        return
    conn.execute(
        "INSERT OR IGNORE INTO task_watches (user_id, task_id, state, source, created_at) VALUES (?,?,?,?,?)",
        (user_id, task_id, "watching", "auto", ctx.now_iso),
    )


def _deliver(ctx, user_id, kind, reason, base):
    conn = ctx.conn
    now = ctx.now_iso
    extra = dict(base["extra"])
    if kind == "assigned":
        extra["tasks"] = [base["task_key"]]
    if kind in MERGEABLE:
        cutoff = iso(ctx.now - timedelta(seconds=COALESCE_SECONDS))
        if kind == "task_created":
            where, args = "project_key IS ?", [base["project_key"]]
        elif kind == "assigned":
            # only a burst - one request handing out many tasks - becomes one item; a later assignment is its own news
            cutoff = now
            where, args = "project_key IS ? AND actor_id IS ?", [base["project_key"], base["actor_id"]]
        else:
            where, args = "task_key IS ?", [base["task_key"]]
        row = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND kind = ? AND read_at IS NULL AND updated_at >= ? AND " + where + " ORDER BY last_event_id DESC LIMIT 1",
            [user_id, kind, cutoff] + args,
        ).fetchone()
        if row is not None:
            old = json.loads(row["extra"] or "{}")
            actors = old.get("actors") or [row["actor_name"]]
            if base["actor_name"] not in actors:
                actors.append(base["actor_name"])
            merged = {**old, "actors": actors[-MAX_ACTORS:]}
            if "status" in extra:  # first "from", latest "to"
                merged["status"] = [old.get("status", extra["status"])[0], extra["status"][1]]
            if "tasks" in extra:
                merged["tasks"] = (old.get("tasks", []) + extra["tasks"])[:MAX_ACTORS]
            if "fields" in extra:
                merged["fields"] = sorted(set(old.get("fields", [])) | set(extra["fields"]))
            conn.execute(
                "UPDATE notifications SET count = count + 1, last_event_id = ?, actor_id = ?, actor_name = ?, excerpt = ?, "
                "title = ?, extra = ?, updated_at = ? WHERE id = ?",
                (base["event_id"], base["actor_id"], base["actor_name"], base["excerpt"] or row["excerpt"], base["title"] or row["title"], json.dumps(merged), now, row["id"]),
            )
            return
    extra["actors"] = [base["actor_name"]]
    conn.execute(
        "INSERT INTO notifications (user_id, kind, reason, actor_id, actor_name, project_key, task_key, title, excerpt, extra, count, "
        "first_event_id, last_event_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
        (user_id, kind, reason, base["actor_id"], base["actor_name"], base["project_key"], base["task_key"], base["title"], base["excerpt"],
         json.dumps(extra), base["event_id"], base["event_id"], now, now),
    )  # fmt: skip
    _prune(ctx, user_id)


def _prune(ctx, user_id):
    conn = ctx.conn
    conn.execute("DELETE FROM notifications WHERE user_id = ? AND read_at IS NOT NULL AND read_at < ?", (user_id, iso(ctx.now - timedelta(days=READ_KEEP_DAYS))))
    total = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ?", (user_id,)).fetchone()[0]
    if total > MAX_ITEMS:
        conn.execute(
            "DELETE FROM notifications WHERE id IN (SELECT id FROM notifications WHERE user_id = ? "
            "ORDER BY (read_at IS NULL), last_event_id LIMIT ?)",
            (user_id, total - MAX_ITEMS),
        )


_ACTIONS = frozenset(
    ("comment.added", "task.created", "task.updated", "task.status_changed", "sprint.started", "sprint.closed", "project.updated",
     "timesheet.approved", "timesheet.rejected", "timesheet.reopened")
)  # fmt: skip


# ------------------------------------------------------------------------------ rendering


def render(row):
    """One plain-text sentence for an item, built from stored fields only (never markup)."""
    extra = json.loads(row["extra"] or "{}")
    who = row["actor_name"] or "Someone"
    target = ("%s %s" % (row["task_key"], row["title"])).strip() if row["task_key"] else row["title"]
    n, kind = row["count"], row["kind"]
    quote = "\u201c%s\u201d" % row["excerpt"] if row["excerpt"] else ""
    if kind == "mention":
        return "%s mentioned you on %s: %s" % (who, target, quote)
    if kind == "assigned":
        if n > 1:
            return "%s assigned you %d tasks in %s (%s)" % (who, n, row["project_key"], ", ".join(extra.get("tasks", [])) + (", ..." if n > len(extra.get("tasks", [])) else ""))
        return "%s assigned %s to you" % (who, target)
    if kind == "timesheet":
        verb = {"approved": "approved", "rejected": "sent back", "reopened": "reopened"}.get(extra.get("decision"), "decided on")
        text = "%s %s your timesheet for the week of %s" % (who, verb, extra.get("week"))
        return text + (": \u201c%s\u201d" % extra["note"] if extra.get("note") else "")
    if kind == "comment":
        return "%s commented on %s: %s" % (who, target, quote) if n == 1 else "%d new comments on %s (latest by %s)" % (n, target, who)
    if kind == "status_changed":
        old, new = extra.get("status", ["?", "?"])
        return "%s moved %s from %s to %s" % (who, target, old, new) if n == 1 else "%d status changes on %s (%s to %s)" % (n, target, old, new)
    if kind == "updated":
        fields = ", ".join(extra.get("fields", [])) or "details"
        return "%s changed %s on %s" % (who, fields, target) if n == 1 else "%d edits on %s (%s)" % (n, target, fields)
    if kind == "task_created":
        return "%s created %s" % (who, target) if n == 1 else "%d tasks created in %s (latest by %s)" % (n, row["project_key"], who)
    if kind == "sprint":
        return "%s %s sprint %s in %s" % (who, extra.get("verb", "changed"), extra.get("sprint"), row["project_key"])
    if kind == "project":
        old, new = extra.get("status", ["?", "?"])
        return "%s moved project %s from %s to %s" % (who, row["project_key"], old, new)
    return who


def _item(row):
    extra = json.loads(row["extra"] or "{}")
    return {
        "id": row["id"], "kind": row["kind"], "reason": row["reason"], "text": render(row), "count": row["count"],
        "actor": {"id": row["actor_id"], "name": row["actor_name"]} if row["actor_name"] else None, "actors": extra.get("actors", []),
        "project": row["project_key"], "task": row["task_key"], "title": row["title"], "excerpt": row["excerpt"],
        "details": {k: v for k, v in extra.items() if k != "actors"}, "read": row["read_at"] is not None, "read_at": row["read_at"],
        "event_id": row["last_event_id"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }  # fmt: skip


# ------------------------------------------------------------------------------ the inbox


def _me(ctx):
    if ctx.user is None:
        raise ValidationError({"user": "the inbox belongs to a signed-in user"})
    return ctx.user


def _int(params, name, default, low, high):
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


def _kinds_param(value, name="kind"):
    kinds = [k.strip() for k in (value or "").split(",") if k.strip()]
    bad = [k for k in kinds if k not in _RANK]
    if bad:
        raise ValidationError({name: "unknown kind %s; allowed: %s" % (", ".join(bad), ", ".join(KINDS))})
    return kinds


def unread_count(conn, user_id):
    return conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)).fetchone()[0]


def count(ctx):
    user = _me(ctx)
    total = ctx.conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ?", (user["id"],)).fetchone()[0]
    return {"unread": unread_count(ctx.conn, user["id"]), "total": total}


def list_items(ctx, params):
    user = _me(ctx)
    where, args = ["user_id = ?"], [user["id"]]
    if truthy(params.get("unread")):
        where.append("read_at IS NULL")
    kinds = _kinds_param(params.get("kind"))
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        args += kinds
    if params.get("project"):
        where.append("project_key = ?")
        args.append(params["project"].upper())
    before = _int(params, "before", None, 1, 2**62)
    if before is not None:
        where.append("last_event_id < ?")
        args.append(before)
    limit = _int(params, "limit", 30, 1, MAX_PAGE)
    rows = ctx.conn.execute(
        "SELECT * FROM notifications WHERE " + " AND ".join(where) + " ORDER BY last_event_id DESC LIMIT ?", args + [limit + 1]
    ).fetchall()
    items = [_item(r) for r in rows[:limit]]
    return {"items": items, "unread": unread_count(ctx.conn, user["id"]), "next_before": items[-1]["event_id"] if len(rows) > limit else None}


def get_item(ctx, item_id):
    user = _me(ctx)
    row = ctx.conn.execute("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (item_id, user["id"])).fetchone()
    if row is None:
        raise NotFound("Notification not found", code="notification_not_found")
    return _item(row)


def _id_list(value):
    if not isinstance(value, list) or not value or len(value) > MAX_MARK or not all(isinstance(i, int) and not isinstance(i, bool) for i in value):
        raise ValidationError({"ids": "must be a list of 1 to %d notification ids" % MAX_MARK})
    return list(dict.fromkeys(value))


def mark_read(ctx, data):
    """Mark some items read (``ids``), or everything that matches (``all: true`` with optional ``project``/``kind``)."""
    user = _me(ctx)
    c = Checker(data, {"ids", "all", "project", "kind", "before"})
    c.raw("ids")
    c.boolean("all")
    c.text("project", max_len=10)
    c.text("kind", max_len=200)
    c.integer("before", minimum=1)
    v = c.result()
    if ("ids" in v) == bool(v.get("all")):
        raise ValidationError({"ids": "give either ids or all: true"})
    if "ids" in v:
        ids = _id_list(v["ids"])
        updated = ctx.conn.execute(
            "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL AND id IN (%s)" % ",".join("?" * len(ids)),
            [ctx.now_iso, user["id"]] + ids,
        ).rowcount
    else:
        where, args = ["user_id = ?", "read_at IS NULL"], [user["id"]]
        kinds = _kinds_param(v.get("kind"))
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            args += kinds
        if v.get("project"):
            where.append("project_key = ?")
            args.append(v["project"].upper())
        if "before" in v:
            where.append("last_event_id <= ?")
            args.append(v["before"])
        updated = ctx.conn.execute("UPDATE notifications SET read_at = ? WHERE " + " AND ".join(where), [ctx.now_iso] + args).rowcount
    return {"updated": updated, "unread": unread_count(ctx.conn, user["id"])}


def mark_unread(ctx, data):
    user = _me(ctx)
    c = Checker(data, {"ids"})
    c.raw("ids", required=True)
    ids = _id_list(c.result()["ids"])
    updated = ctx.conn.execute(
        "UPDATE notifications SET read_at = NULL WHERE user_id = ? AND read_at IS NOT NULL AND id IN (%s)" % ",".join("?" * len(ids)), [user["id"]] + ids
    ).rowcount
    return {"updated": updated, "unread": unread_count(ctx.conn, user["id"])}


def delete_item(ctx, item_id):
    user = _me(ctx)
    if ctx.conn.execute("DELETE FROM notifications WHERE id = ? AND user_id = ?", (item_id, user["id"])).rowcount != 1:
        raise NotFound("Notification not found", code="notification_not_found")


def clear(ctx, params):
    """``?status=read`` removes what is read, ``?status=all`` everything."""
    user = _me(ctx)
    status = params.get("status")
    if status not in ("read", "all"):
        raise ValidationError({"status": "must be 'read' or 'all'"})
    sql = "DELETE FROM notifications WHERE user_id = ?" + (" AND read_at IS NOT NULL" if status == "read" else "")
    removed = ctx.conn.execute(sql, (user["id"],)).rowcount
    return {"removed": removed, "unread": unread_count(ctx.conn, user["id"])}


# ------------------------------------------------------------------------------ watching


def _watch_total(conn, user_id):
    return conn.execute("SELECT (SELECT COUNT(*) FROM task_watches WHERE user_id = ?) + (SELECT COUNT(*) FROM project_watches WHERE user_id = ?)", (user_id, user_id)).fetchone()[0]


def task_watchers(ctx, key):
    row = taskrefs.get_row(ctx.conn, key)
    rows = ctx.conn.execute(
        "SELECT u.id, u.username, u.display_name, w.source FROM task_watches w JOIN users u ON u.id = w.user_id "
        "WHERE w.task_id = ? AND w.state = 'watching' AND u.active = 1 ORDER BY u.display_name COLLATE NOCASE LIMIT ?",
        (row["id"], MAX_LISTED_WATCHERS),
    ).fetchall()
    total = ctx.conn.execute("SELECT COUNT(*) FROM task_watches w JOIN users u ON u.id = w.user_id WHERE w.task_id = ? AND w.state = 'watching' AND u.active = 1", (row["id"],)).fetchone()[0]
    mine = None
    if ctx.user is not None:
        m = ctx.conn.execute("SELECT state, source FROM task_watches WHERE task_id = ? AND user_id = ?", (row["id"], ctx.user["id"])).fetchone()
        mine = {"state": m["state"], "source": m["source"]} if m else None
    return {
        "task": taskrefs.key_of(row), "count": total, "me": mine,
        "watchers": [{**users.ref(r["id"], r["username"], r["display_name"]), "source": r["source"]} for r in rows],
    }  # fmt: skip


def set_task_watch(ctx, key, data):
    user = _me(ctx)
    c = Checker(data, {"state"})
    c.choice("state", WATCH_STATES, required=True)
    state = c.result()["state"]
    row = taskrefs.get_row(ctx.conn, key)
    existing = ctx.conn.execute("SELECT 1 FROM task_watches WHERE user_id = ? AND task_id = ?", (user["id"], row["id"])).fetchone()
    if existing is None and _watch_total(ctx.conn, user["id"]) >= MAX_WATCHES:
        raise Conflict("You are already watching or ignoring %d things; stop some first" % MAX_WATCHES, code="watch_limit")
    ctx.conn.execute(
        "INSERT INTO task_watches (user_id, task_id, state, source, created_at) VALUES (?,?,?,'manual',?) "
        "ON CONFLICT(user_id, task_id) DO UPDATE SET state = excluded.state, source = 'manual'",
        (user["id"], row["id"], state, ctx.now_iso),
    )
    return task_watchers(ctx, key)


def clear_task_watch(ctx, key):
    """Back to the default: what you did on the task decides again."""
    user = _me(ctx)
    row = taskrefs.get_row(ctx.conn, key)
    ctx.conn.execute("DELETE FROM task_watches WHERE user_id = ? AND task_id = ?", (user["id"], row["id"]))


def _project_row(ctx, key):
    from . import projects

    return projects.get_row(ctx.conn, key)


def set_project_watch(ctx, key, data):
    user = _me(ctx)
    c = Checker(data, {"level"})
    c.choice("level", PROJECT_LEVELS)
    level = c.result().get("level", "all")
    project = _project_row(ctx, key)
    existing = ctx.conn.execute("SELECT 1 FROM project_watches WHERE user_id = ? AND project_id = ?", (user["id"], project["id"])).fetchone()
    if existing is None and _watch_total(ctx.conn, user["id"]) >= MAX_WATCHES:
        raise Conflict("You are already watching or ignoring %d things; stop some first" % MAX_WATCHES, code="watch_limit")
    ctx.conn.execute(
        "INSERT INTO project_watches (user_id, project_id, level, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, project_id) DO UPDATE SET level = excluded.level",
        (user["id"], project["id"], level, ctx.now_iso),
    )
    return {"project": project["key"], "level": level}


def clear_project_watch(ctx, key):
    user = _me(ctx)
    project = _project_row(ctx, key)
    ctx.conn.execute("DELETE FROM project_watches WHERE user_id = ? AND project_id = ?", (user["id"], project["id"]))


def watching(ctx, params):
    """Everything you watch or ignore."""
    user = _me(ctx)
    limit = _int(params, "limit", 200, 1, 500)
    tasks = ctx.conn.execute(
        "SELECT p.key AS project_key, t.number, t.title, w.state, w.source FROM task_watches w JOIN tasks t ON t.id = w.task_id "
        "JOIN projects p ON p.id = t.project_id WHERE w.user_id = ? ORDER BY w.created_at DESC, w.rowid DESC LIMIT ?",
        (user["id"], limit),
    ).fetchall()
    projects = ctx.conn.execute(
        "SELECT p.key, p.name, w.level FROM project_watches w JOIN projects p ON p.id = w.project_id WHERE w.user_id = ? ORDER BY p.key", (user["id"],)
    ).fetchall()
    return {
        "tasks": [{"key": "%s-%d" % (r["project_key"], r["number"]), "title": r["title"], "state": r["state"], "source": r["source"]} for r in tasks],
        "projects": [{"key": r["key"], "name": r["name"], "level": r["level"]} for r in projects],
    }  # fmt: skip
