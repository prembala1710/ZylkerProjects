"""Calendar subscription feeds: a private iCalendar link that shows a person's tasks, or a project's, in any calendar app.

A feed belongs to one user and is one of two kinds:

* mine: the open tasks assigned to that user, across every project that is not archived;
* project: every dated open task of one project, plus (optionally) its milestones.

Finished tasks can be added with include_done; cancelled tasks never appear. Only items with a start or a due date can be
placed on a calendar, so the others are left out. To keep a feed small and useful it covers the window from 60 days ago to
one year ahead, and at most 5,000 tasks. A task with both dates is one event across those days, a task with one date is a
one-day event, and every event is marked as free time so a due date does not turn a whole day into a busy one.

Calendar apps cannot send an Authorization header, so a feed is read through a secret address instead,
/api/calendar/<token>.ics. That makes the address a bearer secret: anyone who has it sees the titles and dates it
includes, and nothing else - no descriptions, comments, hours or people other than the assignee of a task. It is shown
once (the database keeps only its SHA-256 hash), can be replaced at any time, stops working when its owner is deactivated, and
is never written to the access log. Feeds are managed with the ordinary token, one user at a time: nobody,
administrators included, can list or change somebody else's.

The table is created with CREATE TABLE IF NOT EXISTS by ensure_schema() whenever an App starts, independently of PRAGMA
user_version. It is purely additive, so existing databases gain it without a migration step.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .. import auth, ical
from ..db import connect
from ..domain import OPEN_SQL, OPEN_STATUSES, sql_in
from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker, parse_date
from . import activity, milestones, projects, users

log = logging.getLogger("zylker.calendar")

FEED_PREFIX = "zcf_"
SCOPES = ("mine", "project")
MAX_FEEDS_PER_USER = 10
NAME_MAX = 60
WINDOW_PAST_DAYS = 60
WINDOW_FUTURE_DAYS = 365
MAX_EVENTS = 5000
REFRESH_MINUTES = 60
LIMITS = {"max_feeds": MAX_FEEDS_PER_USER, "past_days": WINDOW_PAST_DAYS, "future_days": WINDOW_FUTURE_DAYS, "max_events": MAX_EVENTS}

OPEN_AND_DONE_SQL = sql_in(OPEN_STATUSES + ("done",))
STATUS_TEXT = {"todo": "To do", "in_progress": "In progress", "in_review": "In review", "blocked": "Blocked", "done": "Done"}

# Control characters and the invisible direction overrides that make text display differently from what it says.
_BAD_NAME = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")

CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_feeds (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    scope TEXT NOT NULL CHECK (scope IN ('mine','project')),
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    include_done INTEGER NOT NULL DEFAULT 0 CHECK (include_done IN (0,1)),
    include_milestones INTEGER NOT NULL DEFAULT 0 CHECK (include_milestones IN (0,1)),
    token_hash TEXT NOT NULL UNIQUE,
    token_hint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rotated_at TEXT,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    CHECK ((scope = 'project') = (project_id IS NOT NULL)),
    CHECK (scope = 'project' OR include_milestones = 0)
);
CREATE INDEX IF NOT EXISTS idx_calendar_feeds_user ON calendar_feeds (user_id, id);
CREATE INDEX IF NOT EXISTS idx_calendar_feeds_project ON calendar_feeds (project_id);
"""

FEED_SELECT = "SELECT f.*, p.key AS project_key FROM calendar_feeds f LEFT JOIN projects p ON p.id = f.project_id "


def ensure_schema(path: str) -> None:
    """Create the feed table in an initialised database file (no-op for new, missing or in-memory ones)."""
    if path == ":memory:" or not os.path.exists(path):
        return
    conn = connect(path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] > 0:
            conn.executescript(CALENDAR_SCHEMA)
    finally:
        conn.close()


def _require_schema(ctx):
    found = ctx.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'calendar_feeds'").fetchone()
    if found is None:
        raise Conflict(
            "The calendar feed table is missing from this database. Restart the server to create it.",
            code="calendar_not_initialised",
        )


# ------------------------------------------------------------------------------------------------ tokens


def new_token() -> str:
    return FEED_PREFIX + secrets.token_urlsafe(32)


def feed_path(token: str) -> str:
    return "/api/calendar/%s.ics" % token


def to_dict(ctx, row, token=None) -> dict:
    """A feed as the API shows it. The address (token, path, url) is only included when it has just been issued."""
    data = {
        "id": row["id"],
        "name": row["name"],
        "scope": row["scope"],
        "project": row["project_key"],
        "include_done": bool(row["include_done"]),
        "include_milestones": bool(row["include_milestones"]),
        "token_hint": row["token_hint"],
        "created_at": row["created_at"],
        "rotated_at": row["rotated_at"],
        "last_used_at": row["last_used_at"],
        "use_count": row["use_count"],
    }
    if token is not None:
        path = feed_path(token)
        data.update({"token": token, "path": path, "url": ctx.config.public_url + path if ctx.config.public_url else None})
    return data


# ------------------------------------------------------------------------------------------- management


def _own(ctx, feed_id):
    """A feed of the calling user. Somebody else's feed is reported as missing, not as forbidden."""
    row = ctx.conn.execute(FEED_SELECT + "WHERE f.id = ? AND f.user_id = ?", (feed_id, ctx.actor_id)).fetchone()
    if row is None:
        raise NotFound("Calendar feed not found", code="feed_not_found")
    return row


def list_feeds(ctx) -> dict:
    _require_schema(ctx)
    rows = ctx.conn.execute(FEED_SELECT + "WHERE f.user_id = ? ORDER BY f.id", (ctx.actor_id,)).fetchall()
    return {"items": [to_dict(ctx, r) for r in rows], "limits": dict(LIMITS)}


def create_feed(ctx, data) -> dict:
    _require_schema(ctx)
    c = Checker(data, {"name", "scope", "project", "include_done", "include_milestones"})
    c.text("name", min_len=1, max_len=NAME_MAX)
    c.choice("scope", SCOPES, required=True)
    c.text("project", max_len=10)
    c.boolean("include_done")
    c.boolean("include_milestones")
    v = c.result()
    problems = {}
    if _BAD_NAME.search(v.get("name", "")):
        problems["name"] = "must not contain control or direction-override characters"
    if v["scope"] == "project":
        if not v.get("project"):
            problems["project"] = "required for a project feed"
    else:
        for field in ("project", "include_milestones"):
            if field in v:
                problems[field] = "only applies to a project feed"
    if problems:
        raise ValidationError(problems)

    project = projects.get_row(ctx.conn, v["project"]) if v["scope"] == "project" else None
    used = ctx.conn.execute("SELECT COUNT(*) FROM calendar_feeds WHERE user_id = ?", (ctx.actor_id,)).fetchone()[0]
    if used >= MAX_FEEDS_PER_USER:
        raise Conflict(
            "You already have %d calendar feeds. Delete one before adding another." % MAX_FEEDS_PER_USER, code="feed_limit"
        )
    name = v.get("name") or ("My tasks" if project is None else _BAD_NAME.sub("", project["name"])[:NAME_MAX].strip() or project["key"])
    token = new_token()
    cursor = ctx.conn.execute(
        "INSERT INTO calendar_feeds (user_id, name, scope, project_id, include_done, include_milestones, token_hash, "
        "token_hint, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            ctx.actor_id, name, v["scope"], project["id"] if project else None,
            int(v.get("include_done", False)),
            int(v.get("include_milestones", True)) if project else 0,
            auth.hash_token(token), token[-4:], ctx.now_iso,
        ),
    )  # fmt: skip
    activity.log(ctx, "calendar.feed_created", details={"feed": name, "scope": v["scope"], "project": project["key"] if project else None})
    return to_dict(ctx, _own(ctx, cursor.lastrowid), token)


def rotate_feed(ctx, feed_id) -> dict:
    """Replace the address. The old one stops working at once; the new one starts with no fetches on record."""
    _require_schema(ctx)
    row = _own(ctx, feed_id)
    token = new_token()
    ctx.conn.execute(
        "UPDATE calendar_feeds SET token_hash = ?, token_hint = ?, rotated_at = ?, last_used_at = NULL, use_count = 0 WHERE id = ?",
        (auth.hash_token(token), token[-4:], ctx.now_iso, row["id"]),
    )
    activity.log(ctx, "calendar.feed_rotated", details={"feed": row["name"]})
    return to_dict(ctx, _own(ctx, row["id"]), token)


def delete_feed(ctx, feed_id) -> None:
    _require_schema(ctx)
    row = _own(ctx, feed_id)
    ctx.conn.execute("DELETE FROM calendar_feeds WHERE id = ?", (row["id"],))
    activity.log(ctx, "calendar.feed_deleted", details={"feed": row["name"]})


# ----------------------------------------------------------------------------------------------- the feed


def _hours(value) -> str:
    return ("%.2f" % value).rstrip("0").rstrip(".")


def _moment(text, fallback):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return fallback


def _task_rows(ctx, feed, owner, first, last):
    where = [
        "(t.start_date IS NOT NULL OR t.due_date IS NOT NULL)",
        "COALESCE(t.due_date, t.start_date) >= ?",
        "COALESCE(t.start_date, t.due_date) <= ?",
        "t.status IN %s" % (OPEN_AND_DONE_SQL if feed["include_done"] else OPEN_SQL),
    ]
    args = [first.isoformat(), last.isoformat()]
    if feed["scope"] == "mine":
        where += ["t.assignee_id = ?", "p.status <> 'archived'"]
        args.append(owner["id"])
    else:
        where.append("t.project_id = ?")
        args.append(feed["project_id"])
    sql = (
        "SELECT t.number, t.title, t.status, t.priority, t.start_date, t.due_date, t.estimate_hours, t.updated_at, "
        "p.key AS pkey, p.name AS pname, a.display_name AS assignee_name "
        "FROM tasks t JOIN projects p ON p.id = t.project_id LEFT JOIN users a ON a.id = t.assignee_id "
        "WHERE " + " AND ".join(where) + " ORDER BY COALESCE(t.start_date, t.due_date), p.key, t.number LIMIT ?"
    )
    return ctx.conn.execute(sql, args + [MAX_EVENTS + 1]).fetchall()


def _task_event(ctx, feed, row, host):
    start, due = parse_date(row["start_date"]), parse_date(row["due_date"])
    days = [d for d in (start, due) if d is not None]
    if not days:  # cannot happen with validated data; a damaged row must not take the whole feed down
        return None
    first, last = min(days), max(days)
    key = "%s-%d" % (row["pkey"], row["number"])
    summary = "%s %s" % (key, row["title"])
    if due and not start:
        summary += " (due)"
    elif start and not due:
        summary += " (starts)"
    if row["status"] == "done":
        summary = "\u2713 " + summary
    link = "%s/#/t/%s" % (ctx.config.public_url, key) if ctx.config.public_url else ""
    lines = [
        "Project: %s (%s)" % (row["pname"], row["pkey"]),
        "Status: %s" % STATUS_TEXT.get(row["status"], row["status"]),
        "Priority: %s" % str(row["priority"]).capitalize(),
    ]
    if feed["scope"] == "project" and row["assignee_name"]:
        lines.append("Assignee: %s" % row["assignee_name"])
    if row["estimate_hours"] is not None:
        lines.append("Estimate: %s h" % _hours(row["estimate_hours"]))
    if link:
        lines.append("Open in Zylker Projects: " + link)
    event = ical.Event(
        uid="task-%s@%s" % (key, host), summary=summary, first_day=first, last_day=last,
        modified=_moment(row["updated_at"], ctx.now), description="\n".join(lines), url=link,
    )  # fmt: skip
    return (first, last, 1, row["pkey"], row["number"]), event


def _milestone_events(ctx, feed, project, first, last, host):
    today = ctx.today.isoformat()
    rows = ctx.conn.execute(
        milestones.MILESTONE_SELECT + "WHERE m.project_id = ? AND m.due_date >= ? AND m.due_date <= ? ORDER BY m.due_date, m.id",
        (project["id"], first.isoformat(), last.isoformat()),
    ).fetchall()
    events = []
    for row in rows:
        m = milestones.to_dict(row, today)
        day = parse_date(m["due_date"])
        if day is None:
            continue
        lines = [
            "Project: %s (%s)" % (project["name"], project["key"]),
            "Milestone: %s" % m["status"],
            "Tasks done: %d of %d" % (m["tasks_done"], m["tasks_total"]),
        ]
        link = "%s/#/p/%s/milestones" % (ctx.config.public_url, project["key"]) if ctx.config.public_url else ""
        if link:
            lines.append("Open in Zylker Projects: " + link)
        event = ical.Event(
            uid="milestone-%d@%s" % (m["id"], host),
            summary="%sMilestone: %s" % ("\u2713 " if m["status"] == "achieved" else "", m["name"]),
            first_day=day, last_day=day, modified=_moment(m["updated_at"], ctx.now),
            description="\n".join(lines), url=link,
        )  # fmt: skip
        events.append(((day, day, 0, "", m["id"]), event))  # a milestone comes before the tasks of its day
    return events


def _record_fetch(ctx, feed_id, token_hash) -> None:
    """Count one fetch (best effort).

    This runs on a connection of its own. The request itself only reads, and a read that is upgraded to a write after
    another connection has committed fails at once instead of waiting; a separate short transaction waits for its turn
    and cannot fail the feed. It is also why an unknown address, which anybody can send, never takes the write lock.
    The address is matched again so that a link replaced a moment ago does not count against its successor.
    """
    try:
        conn = connect(ctx.config.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE calendar_feeds SET last_used_at = ?, use_count = use_count + 1 WHERE id = ? AND token_hash = ?",
                (ctx.now_iso, feed_id, token_hash),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
    except sqlite3.Error as exc:  # statistics are never worth a failed subscription
        log.warning("could not record a calendar feed fetch (%s)", type(exc).__name__)


def render_feed(ctx, token) -> str:
    """The iCalendar text behind a feed address, or 404. Every successful fetch is counted."""
    token_hash = auth.hash_token(token)
    try:
        feed = ctx.conn.execute(
            FEED_SELECT + "WHERE f.token_hash = ? AND f.user_id IN (SELECT id FROM users WHERE active = 1)", (token_hash,)
        ).fetchone()
    except sqlite3.OperationalError as exc:  # databases that never had the table have no feeds either
        if "no such table" not in str(exc):
            raise
        feed = None
    if feed is None:  # unknown, replaced or deleted, or its owner is deactivated: all the same answer
        raise NotFound("No such calendar feed", code="feed_not_found")
    owner = users.get_row(ctx.conn, feed["user_id"])
    today = ctx.today
    first, last = today - timedelta(days=WINDOW_PAST_DAYS), today + timedelta(days=WINDOW_FUTURE_DAYS)
    host = urlparse(ctx.config.public_url).hostname or "zylker-pm"

    rows = _task_rows(ctx, feed, owner, first, last)
    truncated = len(rows) > MAX_EVENTS
    events = [e for e in (_task_event(ctx, feed, r, host) for r in rows[:MAX_EVENTS]) if e is not None]
    if feed["scope"] == "project" and feed["include_milestones"]:
        project = ctx.conn.execute("SELECT * FROM projects WHERE id = ?", (feed["project_id"],)).fetchone()
        events += _milestone_events(ctx, feed, project, first, last, host)
    events = [event for _, event in sorted(events, key=lambda pair: pair[0])]  # calendar order: by day, then project and task number

    if feed["scope"] == "mine":
        about = "Open tasks assigned to %s" % owner["display_name"]
    else:
        project_row = ctx.conn.execute("SELECT key, name FROM projects WHERE id = ?", (feed["project_id"],)).fetchone()
        about = "%s (%s): %s" % (project_row["name"], project_row["key"], "tasks and milestones" if feed["include_milestones"] else "tasks")
    about += ", from %d days ago to %d days ahead." % (WINDOW_PAST_DAYS, WINDOW_FUTURE_DAYS)
    if truncated:
        about += " Only the first %d tasks are listed." % MAX_EVENTS
    text = ical.render_calendar(feed["name"], events, description="Zylker Projects. " + about, refresh_minutes=REFRESH_MINUTES)
    _record_fetch(ctx, feed["id"], token_hash)
    return text
