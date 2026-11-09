"""Outbound webhooks: subscriptions, the transactional outbox and the delivery log.

Every audited change (``activity.log``) is offered to :func:`enqueue_activity` *inside the same
transaction*, which writes one ``webhook_deliveries`` row per matching subscription. Because the row
commits - or rolls back - together with the change itself, an event is never announced for a request that
failed, and never lost for one that succeeded. Sending happens later, off the request path, in
``webhook_worker`` (retries, back-off, delivery log).

The tables are created with ``CREATE TABLE IF NOT EXISTS`` by :func:`ensure_schema` whenever an ``App``
starts, independently of ``PRAGMA user_version``; they are purely additive, so existing databases gain them
without a migration step.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from .. import webhook_http
from ..db import connect
from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker
from . import activity

log = logging.getLogger("zylker.webhooks")

# Event names are the audit-trail action names. "*" and "<group>.*" subscribe to a whole family, including
# actions added by later features, so only exact names have to be catalogued.
EVENT_GROUPS = {
    "project": ("created", "updated", "deleted", "member_added", "member_removed"),
    "milestone": ("created", "updated", "deleted"),
    "task": ("created", "updated", "status_changed", "deleted", "dependency_added", "dependency_removed"),
    "comment": ("added", "edited", "deleted"),
    "time": ("logged", "deleted"),
    "import": ("completed",),
}
EVENTS = tuple("%s.%s" % (group, name) for group, names in EVENT_GROUPS.items() for name in names)
PATTERNS = ("*",) + tuple(group + ".*" for group in EVENT_GROUPS) + EVENTS
PING_EVENT = "webhook.ping"
# User administration and webhook management are never delivered: they are about people and credentials,
# and delivering webhook changes to webhooks would feed back on itself.
NEVER_DELIVERED = ("user", "webhook")

MAX_WEBHOOKS = 50
MAX_PENDING = 5000  # queued + in-flight deliveries per webhook; beyond this new events are dropped (and logged)
KEEP_DELIVERIES = 500  # finished deliveries kept per webhook
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_ATTEMPTS = 6
RETRY_DELAYS = (30, 120, 600, 3600, 21600)  # seconds to wait after attempt 1, 2, 3, ...
HEARTBEAT_STALE_SECONDS = 30
DELIVERY_STATUSES = ("pending", "sending", "succeeded", "failed")

WEBHOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    events TEXT NOT NULL,
    projects TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_delivery_at TEXT,
    last_status TEXT CHECK (last_status IN ('succeeded','failed')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event TEXT NOT NULL,
    event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','sending','succeeded','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 6,
    next_attempt_at TEXT,
    lease_until TEXT,
    response_status INTEGER,
    error TEXT,
    response_excerpt TEXT,
    duration_ms INTEGER,
    redelivery_of INTEGER,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wh_deliveries_due ON webhook_deliveries (status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_wh_deliveries_hook ON webhook_deliveries (webhook_id, id);
CREATE TABLE IF NOT EXISTS webhook_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def ensure_schema(path: str) -> None:
    """Create the webhook tables in an initialised database file (no-op for new, missing or in-memory ones)."""
    if path == ":memory:" or not os.path.exists(path):
        return
    conn = connect(path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] > 0:
            conn.executescript(WEBHOOK_SCHEMA)
    finally:
        conn.close()


def _require_schema(ctx):
    found = ctx.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'webhooks'").fetchone()
    if found is None:
        raise Conflict(
            "The webhook tables are missing from this database. Restart the server (or run `zylker-pm init`) to create them.",
            code="webhooks_not_initialised",
        )


# ------------------------------------------------------------------------------ signing


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp, body: bytes) -> str:
    """``v1=`` + hex HMAC-SHA256 of ``"<timestamp>.<body>"`` keyed with the webhook secret."""
    mac = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def verify(secret: str, timestamp, body: bytes, signature, *, tolerance=300, now=None) -> bool:
    """Reference check for receivers: fresh timestamp, and any ``v1=`` value in the header matches."""
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((time.time() if now is None else now) - stamp) > tolerance:
        return False
    expected = sign(secret, stamp, body)
    return any(hmac.compare_digest(expected, part.strip()) for part in str(signature or "").split(","))


# ------------------------------------------------------------------------------ matching


def matches(patterns, event: str) -> bool:
    for pattern in patterns:
        if pattern == "*" or pattern == event or (pattern.endswith(".*") and event.startswith(pattern[:-1])):
            return True
    return False


def catalog():
    return {
        "events": list(EVENTS),
        "groups": {group: ["%s.%s" % (group, n) for n in names] for group, names in EVENT_GROUPS.items()},
        "patterns": list(PATTERNS),
        "ping": PING_EVENT,
        "never_delivered": [g + ".*" for g in NEVER_DELIVERED],
    }


# ------------------------------------------------------------------------------ outbox

_last_warned: dict = {}
WARN_EVERY_SECONDS = 300


def _warn_throttled(key, message, *args):
    """Operators need to see a stuck queue, but not once per request."""
    now = time.monotonic()
    if now - _last_warned.get(key, -WARN_EVERY_SECONDS) >= WARN_EVERY_SECONDS:
        _last_warned[key] = now
        log.warning(message, *args)


def queue_delivery(conn, webhook_id, event, event_id, payload_json, now_iso, *, max_attempts=MAX_ATTEMPTS, redelivery_of=None):
    cur = conn.execute(
        "INSERT INTO webhook_deliveries (webhook_id, event, event_id, payload, status, attempts, max_attempts, "
        "next_attempt_at, redelivery_of, created_at) VALUES (?,?,?,?, 'pending', 0, ?, ?, ?, ?)",
        (webhook_id, event, event_id, payload_json, max_attempts, now_iso, redelivery_of, now_iso),
    )
    return cur.lastrowid


def _dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def enqueue_activity(ctx, activity_id, action, *, project_id, task_id, task_key, details):
    """Called by ``activity.log`` in the writer's transaction. Never lets a webhook problem fail the request."""
    if action.split(".", 1)[0] in NEVER_DELIVERED:
        return
    try:
        hooks = ctx.conn.execute("SELECT id, events, projects FROM webhooks WHERE active = 1").fetchall()
        if not hooks:
            return
        project = None
        if project_id is not None:
            row = ctx.conn.execute("SELECT key, name FROM projects WHERE id = ?", (project_id,)).fetchone()
            project = {"key": row["key"], "name": row["name"]} if row else None
        elif action == "project.deleted":
            project = {"key": details.get("key"), "name": details.get("name")}
        key = project["key"] if project else None
        chosen = []
        for hook in hooks:
            scope = json.loads(hook["projects"])
            if matches(json.loads(hook["events"]), action) and (not scope or key in scope):
                chosen.append(hook["id"])
        if not chosen:
            return
        payload = build_payload(ctx, "evt_%d" % activity_id, action, project, task_id, task_key, details)
        body = _dump(payload)
        if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            payload["details"] = {"truncated": True}
            body = _dump(payload)
        for hook_id in chosen:
            backlog = ctx.conn.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_id = ? AND status IN ('pending','sending')", (hook_id,)
            ).fetchone()[0]
            if backlog >= MAX_PENDING:
                _warn_throttled(("backlog", hook_id), "webhook %d has %d undelivered events; dropping new ones until it catches up", hook_id, backlog)
                continue
            queue_delivery(ctx.conn, hook_id, action, payload["id"], body, ctx.now_iso)
    except sqlite3.Error:
        _warn_throttled("outbox", "could not queue webhook deliveries (are the webhook tables missing?)")
        log.debug("outbox failure", exc_info=True)


def build_payload(ctx, event_id, action, project, task_id, task_key, details):
    base = ctx.config.public_url
    if project and base and action != "project.deleted":  # no link to something that no longer exists
        project = {**project, "url": "%s/#/p/%s" % (base, project["key"])}
    task = None
    if task_key:
        task = {"key": task_key}
        row = None
        if task_id is not None:
            row = ctx.conn.execute(
                "SELECT t.title, t.status, t.priority, t.type, t.due_date, u.username AS assignee "
                "FROM tasks t LEFT JOIN users u ON u.id = t.assignee_id WHERE t.id = ?",
                (task_id,),
            ).fetchone()
        if row is not None:
            task.update(title=row["title"], status=row["status"], priority=row["priority"], type=row["type"],
                        assignee=row["assignee"], due_date=row["due_date"])  # fmt: skip
            if base:
                task["url"] = "%s/#/t/%s" % (base, task_key)
        elif details.get("title"):  # the task is gone (deleted): the audit entry still knows its title, but there is nothing to link to
            task["title"] = details["title"]
    actor = None
    if ctx.user is not None:
        actor = {"username": ctx.user["username"], "display_name": ctx.user["display_name"]}
    return {
        "id": event_id,
        "event": action,
        "created_at": ctx.now_iso,
        "project": project,
        "task": task,
        "actor": actor,
        "details": details,
    }


# ------------------------------------------------------------------------------ webhooks (CRUD)

FIELDS = ("name", "url", "events", "projects", "active")


def _normalise_events(value):
    if not isinstance(value, list) or not value:
        raise ValueError("must be a non-empty list of event names (see GET /api/webhooks/events)")
    if len(value) > 200:
        raise ValueError("too many entries")
    out = []
    for item in value:
        if not isinstance(item, str) or item not in PATTERNS:
            raise ValueError("unknown event %s (see GET /api/webhooks/events)" % (json.dumps(item)[:60],))
        if item not in out:
            out.append(item)
    return ["*"] if "*" in out else out


def _normalise_projects(conn, value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50 or not all(isinstance(v, str) for v in value):
        raise ValueError("must be a list of up to 50 project keys (an empty list means every project)")
    keys = []
    for item in value:
        key = item.strip().upper()
        if key not in keys:
            keys.append(key)
    if keys:
        known = {r[0] for r in conn.execute("SELECT key FROM projects WHERE key IN (%s)" % ",".join("?" * len(keys)), keys)}
        missing = [k for k in keys if k not in known]
        if missing:
            raise ValueError("unknown project%s: %s" % ("s" if len(missing) > 1 else "", ", ".join(missing)[:100]))
    return keys


def _validate(ctx, data, partial):
    c = Checker(data, FIELDS, partial=partial)
    c.text("name", required=True, max_len=80, min_len=1)
    c.text("url", required=True, max_len=webhook_http.MAX_URL_LENGTH, min_len=1)
    c.raw("events", required=True)
    c.raw("projects")
    c.boolean("active")
    if "url" in c.out:
        try:
            c.out["url"] = webhook_http.validate_url(c.out["url"], ctx.config.webhook_allow_private)
        except ValueError as exc:
            c.errors["url"] = str(exc)
    if "events" in c.out:
        try:
            c.out["events"] = _normalise_events(c.out["events"])
        except ValueError as exc:
            c.errors["events"] = str(exc)
    if "projects" in c.out:
        try:
            c.out["projects"] = _normalise_projects(ctx.conn, c.out["projects"])
        except ValueError as exc:
            c.errors["projects"] = str(exc)
    return c.result()


_SELECT = "SELECT w.*, u.username AS creator FROM webhooks w LEFT JOIN users u ON u.id = w.created_by "


def _row(conn, webhook_id):
    row = conn.execute(_SELECT + "WHERE w.id = ?", (webhook_id,)).fetchone()
    if row is None:
        raise NotFound("Webhook not found", code="webhook_not_found")
    return row


def _hook_dict(row, pending=0):
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "events": json.loads(row["events"]),
        "projects": json.loads(row["projects"]),
        "active": bool(row["active"]),
        "secret_hint": "\u2026" + row["secret"][-4:],  # the secret itself is shown once, when it is created or rotated
        "pending_deliveries": pending,
        "health": {
            "last_delivery_at": row["last_delivery_at"],
            "last_status": row["last_status"],
            "consecutive_failures": row["consecutive_failures"],
        },
        "created_by": row["creator"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _pending_counts(conn):
    sql = "SELECT webhook_id, COUNT(*) FROM webhook_deliveries WHERE status IN ('pending','sending') GROUP BY webhook_id"
    return {r[0]: r[1] for r in conn.execute(sql)}


def _pending_count(conn, webhook_id):
    return conn.execute(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_id = ? AND status IN ('pending','sending')", (webhook_id,)
    ).fetchone()[0]


def worker_status(ctx):
    row = ctx.conn.execute("SELECT value FROM webhook_state WHERE key = 'worker_heartbeat'").fetchone()
    if row is None:
        return {"running": False, "last_seen": None}
    seen = datetime.strptime(row["value"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return {"running": (ctx.now - seen).total_seconds() <= HEARTBEAT_STALE_SECONDS, "last_seen": row["value"]}


def list_webhooks(ctx):
    ctx.require_role("admin")
    _require_schema(ctx)
    pending = _pending_counts(ctx.conn)
    rows = ctx.conn.execute(_SELECT + "ORDER BY w.id").fetchall()
    return {"items": [_hook_dict(r, pending.get(r["id"], 0)) for r in rows], "worker": worker_status(ctx)}


def get_webhook(ctx, webhook_id):
    ctx.require_role("admin")
    _require_schema(ctx)
    row = _row(ctx.conn, webhook_id)
    return _hook_dict(row, _pending_count(ctx.conn, webhook_id))


def create_webhook(ctx, data):
    ctx.require_role("admin")
    _require_schema(ctx)
    v = _validate(ctx, data, partial=False)
    if ctx.conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] >= MAX_WEBHOOKS:
        raise Conflict("At most %d webhooks are supported" % MAX_WEBHOOKS, code="webhook_limit")
    secret = new_secret()
    cur = ctx.conn.execute(
        "INSERT INTO webhooks (name, url, secret, events, projects, active, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (v["name"], v["url"], secret, json.dumps(v["events"]), json.dumps(v.get("projects", [])), 1 if v.get("active", True) else 0,
         ctx.actor_id, ctx.now_iso, ctx.now_iso),
    )  # fmt: skip
    activity.log(ctx, "webhook.created", details={"id": cur.lastrowid, "name": v["name"]})
    return {**_hook_dict(_row(ctx.conn, cur.lastrowid)), "secret": secret}


def update_webhook(ctx, webhook_id, data):
    ctx.require_role("admin")
    _require_schema(ctx)
    row = _row(ctx.conn, webhook_id)
    v = _validate(ctx, data, partial=True)
    updates = {}
    for field in ("name", "url"):
        if field in v and v[field] != row[field]:
            updates[field] = v[field]
    for field in ("events", "projects"):
        if field in v and v[field] != json.loads(row[field]):
            updates[field] = json.dumps(v[field])
    if "active" in v and int(v["active"]) != row["active"]:
        updates["active"] = int(v["active"])
    if updates:
        updates["updated_at"] = ctx.now_iso
        ctx.conn.execute(
            "UPDATE webhooks SET %s WHERE id = ?" % ", ".join("%s = ?" % c for c in updates), list(updates.values()) + [webhook_id]
        )
        if updates.get("active") == 0:  # pausing means "stop sending", not "send the backlog when I resume"
            ctx.conn.execute(
                "UPDATE webhook_deliveries SET status = 'failed', error = 'cancelled', next_attempt_at = NULL, lease_until = NULL "
                "WHERE webhook_id = ? AND status = 'pending' AND event <> ?",
                (webhook_id, PING_EVENT),
            )
        changed = sorted(k for k in updates if k != "updated_at")
        activity.log(ctx, "webhook.updated", details={"id": webhook_id, "name": updates.get("name", row["name"]), "fields": changed})
    return _hook_dict(_row(ctx.conn, webhook_id), _pending_count(ctx.conn, webhook_id))


def delete_webhook(ctx, webhook_id):
    ctx.require_role("admin")
    _require_schema(ctx)
    row = _row(ctx.conn, webhook_id)
    ctx.conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))
    activity.log(ctx, "webhook.deleted", details={"id": webhook_id, "name": row["name"]})


def rotate_secret(ctx, webhook_id):
    ctx.require_role("admin")
    _require_schema(ctx)
    row = _row(ctx.conn, webhook_id)
    secret = new_secret()
    ctx.conn.execute("UPDATE webhooks SET secret = ?, updated_at = ? WHERE id = ?", (secret, ctx.now_iso, webhook_id))
    activity.log(ctx, "webhook.secret_rotated", details={"id": webhook_id, "name": row["name"]})
    return {**_hook_dict(_row(ctx.conn, webhook_id), _pending_count(ctx.conn, webhook_id)), "secret": secret}


# ------------------------------------------------------------------------------ deliveries

_DELIVERY_COLUMNS = (
    "id, event, event_id, status, attempts, max_attempts, next_attempt_at, response_status, error, response_excerpt, "
    "duration_ms, redelivery_of, created_at, last_attempt_at"
)


def _delivery_dict(row, payload=False):
    out = {k: row[k] for k in row.keys() if k != "payload"}
    if payload:
        out["payload"] = json.loads(row["payload"])
    return out


def send_test(ctx, webhook_id):
    """Queue a ``webhook.ping``; the worker sends it within about a second. Pings are tried once and work even
    while the webhook is paused, so a new endpoint can be checked before it is switched on."""
    ctx.require_role("admin")
    _require_schema(ctx)
    row = _row(ctx.conn, webhook_id)
    event_id = "evt_test_" + secrets.token_hex(6)
    actor = {"username": ctx.user["username"], "display_name": ctx.user["display_name"]} if ctx.user is not None else None
    payload = _dump({
        "id": event_id, "event": PING_EVENT, "created_at": ctx.now_iso, "project": None, "task": None, "actor": actor,
        "details": {"message": "Test event from Zylker Projects", "webhook": row["name"]},
    })  # fmt: skip
    delivery_id = queue_delivery(ctx.conn, webhook_id, PING_EVENT, event_id, payload, ctx.now_iso, max_attempts=1)
    return _delivery_dict(ctx.conn.execute("SELECT %s FROM webhook_deliveries WHERE id = ?" % _DELIVERY_COLUMNS, (delivery_id,)).fetchone())


def list_deliveries(ctx, webhook_id, params):
    ctx.require_role("admin")
    _require_schema(ctx)
    _row(ctx.conn, webhook_id)
    errors, limit, before = {}, 25, None
    if params.get("limit") not in (None, ""):
        try:
            limit = int(params["limit"])
            if not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            errors["limit"] = "must be an integer between 1 and 100"
    status = params.get("status") or None
    if status is not None and status not in DELIVERY_STATUSES:
        errors["status"] = "must be one of: " + ", ".join(DELIVERY_STATUSES)
    if params.get("before") not in (None, ""):
        try:
            before = int(params["before"])
        except ValueError:
            errors["before"] = "must be a delivery id"
    if errors:
        raise ValidationError(errors)
    where, args = ["webhook_id = ?"], [webhook_id]
    if status:
        where.append("status = ?")
        args.append(status)
    if before is not None:
        where.append("id < ?")
        args.append(before)
    rows = ctx.conn.execute(
        "SELECT %s FROM webhook_deliveries WHERE %s ORDER BY id DESC LIMIT ?" % (_DELIVERY_COLUMNS, " AND ".join(where)), args + [limit]
    ).fetchall()
    items = [_delivery_dict(r) for r in rows]
    return {"items": items, "next_before": items[-1]["id"] if len(items) == limit else None}


def _delivery_row(conn, webhook_id, delivery_id):
    row = conn.execute(
        "SELECT %s, payload FROM webhook_deliveries WHERE id = ? AND webhook_id = ?" % _DELIVERY_COLUMNS, (delivery_id, webhook_id)
    ).fetchone()
    if row is None:
        raise NotFound("Delivery not found", code="delivery_not_found")
    return row


def get_delivery(ctx, webhook_id, delivery_id):
    ctx.require_role("admin")
    _require_schema(ctx)
    _row(ctx.conn, webhook_id)
    return _delivery_dict(_delivery_row(ctx.conn, webhook_id, delivery_id), payload=True)


def redeliver(ctx, webhook_id, delivery_id):
    """Send a finished delivery again as a new delivery (same event id and payload, fresh signature)."""
    ctx.require_role("admin")
    _require_schema(ctx)
    hook = _row(ctx.conn, webhook_id)
    old = _delivery_row(ctx.conn, webhook_id, delivery_id)
    if old["status"] in ("pending", "sending"):
        raise Conflict("This delivery is still queued or in flight", code="delivery_in_progress")
    if not hook["active"] and old["event"] != PING_EVENT:
        raise Conflict("The webhook is paused. Resume it first.", code="webhook_paused")
    new_id = queue_delivery(ctx.conn, webhook_id, old["event"], old["event_id"], old["payload"], ctx.now_iso,
                            max_attempts=old["max_attempts"], redelivery_of=old["id"])  # fmt: skip
    return _delivery_dict(_delivery_row(ctx.conn, webhook_id, new_id))
