"""Outbound webhooks: tell other systems when something happens, reliably and verifiably.

How it works
------------
* **Outbox.** Every audit event (``activity`` row) that matches an active webhook is copied into
  ``webhook_deliveries`` *in the same transaction* as the change itself. So an event is queued if and only if the
  change was committed - a rolled-back request announces nothing, and a crash cannot lose an announcement.
* **Delivery.** ``run_due`` claims due rows, POSTs them (a few at a time), and records the outcome. A failed attempt
  is retried with growing delays (1 min, 5 min, 30 min, 2 h, 6 h) and then given up on. Delivery is *at least once*:
  every attempt of one delivery carries the same ``X-Zylker-Delivery`` id, which receivers can use to ignore repeats.
* **Verification.** Each request is signed: ``X-Zylker-Signature: v1=HMAC_SHA256(secret, "<timestamp>.<body>")``
  with the timestamp in ``X-Zylker-Timestamp``. ``verify_signature`` is the reference implementation, including the
  replay window.
* **Safety.** A webhook that keeps failing is switched off (and says why), one answering ``410 Gone`` immediately.
  The queue per webhook is bounded, the log is pruned after 30 days, and the network layer (``netguard``) refuses to
  call private addresses.

Webhook events are never themselves delivered (no loops), and the secret is shown once, when it is created.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from urllib.parse import urlsplit

from .. import __version__, netguard
from ..db import connect, transaction
from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker, iso, truncate
from . import activity, audit

log = logging.getLogger("zylker.webhooks")

MAX_WEBHOOKS = 20
MAX_PATTERNS = 30
MAX_PENDING = 1000  # per webhook: past this, new events are recorded as failed ("queue_full") instead of piling up
MAX_PAYLOAD_BYTES = 64_000
MAX_ATTEMPTS = 6
RETRY_DELAYS = (60, 300, 1800, 7200, 21600)  # seconds to wait after attempt 1, 2, ...
DISABLE_AFTER = 15  # consecutive failed attempts before a webhook is switched off
STALE_CLAIM_SECONDS = 300  # a delivery stuck "sending" this long (a crashed worker) is picked up again
RETENTION_DAYS = 30
SIGNATURE_TOLERANCE = 300  # seconds a signed request stays acceptable to a verifier

INTERNAL_CATEGORIES = ("webhook",)  # their own events are never delivered
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,60}$")
ACTION_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")
PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,9}$")
STATUSES = ("pending", "sending", "delivered", "failed", "cancelled")


def subscribable_categories():
    return [c for c in audit.CATEGORIES if c not in INTERNAL_CATEGORIES]


# ------------------------------------------------------------------------------ signing


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), b"%d." % timestamp + body, sha256).hexdigest()
    return "v1=" + mac


def verify_signature(secret, timestamp_header, signature_header, body: bytes, *, now, tolerance=SIGNATURE_TOLERANCE) -> bool:
    """What a receiver should do: recompute the signature over the raw body, compare in constant time, and refuse
    requests whose timestamp is too old or too new (a captured request cannot be replayed later)."""
    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False
    if abs(int(now) - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected.encode("ascii"), str(signature_header or "").encode("utf-8", "replace"))


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _hint(secret):
    return "whsec_..." + secret[-4:]


# ------------------------------------------------------------------------------ event patterns


def matches(patterns, action: str) -> bool:
    category = action.split(".", 1)[0]
    return any(p == "*" or p == action or (p.endswith(".*") and p[:-2] == category) for p in patterns)


def _check_events(value):
    if not isinstance(value, list) or not value:
        raise ValidationError({"events": "must be a non-empty list such as [\"task.created\", \"project.*\"] or [\"*\"]"})
    if len(value) > MAX_PATTERNS:
        raise ValidationError({"events": "at most %d entries" % MAX_PATTERNS})
    categories, out = subscribable_categories(), []
    for item in value:
        ok = isinstance(item, str) and (
            item == "*"
            or (item.endswith(".*") and item[:-2] in categories)
            or (ACTION_RE.match(item) is not None and item.split(".", 1)[0] in categories)
        )
        if not ok:
            raise ValidationError({"events": "%r is not an event: use '*', 'category.*' or 'category.action' (categories: %s)" % (truncate(item, 40), ", ".join(categories))})
        if item not in out:
            out.append(item)
    return out


# ------------------------------------------------------------------------------ the outbox


def enqueue(conn, now_iso, *, event_id, action, project_key=None, task_key=None, actor_id=None, details=None):
    """Queue ``action`` for every active webhook subscribed to it. Called inside the transaction that wrote the
    audit event. Never raises: announcing must not break the change it announces."""
    try:
        _enqueue(conn, now_iso, event_id, action, project_key, task_key, actor_id, details)
    except Exception:  # noqa: BLE001
        log.warning("could not queue webhook deliveries for %s", action, exc_info=True)


def _enqueue(conn, now_iso, event_id, action, project_key, task_key, actor_id, details):
    if action.split(".", 1)[0] in INTERNAL_CATEGORIES:
        return
    hooks = conn.execute("SELECT id, events, project_key FROM webhooks WHERE active = 1").fetchall()
    chosen = [
        h["id"] for h in hooks
        if (h["project_key"] is None or h["project_key"] == project_key) and matches(json.loads(h["events"]), action)
    ]  # fmt: skip
    if not chosen:
        return
    actor = None
    if actor_id is not None:
        row = conn.execute("SELECT username, display_name FROM users WHERE id = ?", (actor_id,)).fetchone()
        if row is not None:
            actor = {"username": row["username"], "display_name": row["display_name"]}
    payload = {
        "event": action, "event_id": event_id, "occurred_at": now_iso, "project": project_key, "task": task_key,
        "actor": actor, "details": details or {},
    }  # fmt: skip
    text = json.dumps(payload, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        payload["details"] = {"truncated": True}
        text = json.dumps(payload, separators=(",", ":"))
    for hook_id in chosen:
        backlog = conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_id = ? AND status IN ('pending','sending')", (hook_id,)).fetchone()[0]
        if backlog >= MAX_PENDING:
            _insert_delivery(conn, hook_id, action, event_id, text, now_iso, status="failed", error="queue_full")
        else:
            _insert_delivery(conn, hook_id, action, event_id, text, now_iso)


def _insert_delivery(conn, hook_id, event, event_id, payload, now_iso, *, status="pending", error=None, redelivery_of=None):
    cur = conn.execute(
        "INSERT INTO webhook_deliveries (webhook_id, event, event_id, payload, status, next_attempt_at, error, redelivery_of, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (hook_id, event, event_id, payload, status, now_iso if status == "pending" else None, error, redelivery_of, now_iso, now_iso if status != "pending" else None),
    )
    return cur.lastrowid


# ------------------------------------------------------------------------------ management


def _allow_private(ctx):
    return bool(getattr(ctx.config, "webhook_allow_private", False))


def _get_row(conn, webhook_id):
    row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
    if row is None:
        raise NotFound("Webhook not found", code="webhook_not_found")
    return row


def _counts(conn, ids):
    out = {i: {s: 0 for s in STATUSES} for i in ids}
    for r in conn.execute("SELECT webhook_id, status, COUNT(*) AS n FROM webhook_deliveries GROUP BY webhook_id, status"):
        if r["webhook_id"] in out:
            out[r["webhook_id"]][r["status"]] = r["n"]
    return out


def to_dict(row, counts=None):
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "events": json.loads(row["events"]),
        "project": row["project_key"],
        "active": bool(row["active"]),
        "secret_hint": _hint(row["secret"]),
        "failure_streak": row["failure_streak"],
        "disabled_reason": row["disabled_reason"],
        "disabled_at": row["disabled_at"],
        "last_attempt_at": row["last_attempt_at"],
        "last_success_at": row["last_success_at"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "deliveries": counts if counts is not None else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def info():
    return {
        "categories": {c: audit.CATEGORIES[c] for c in subscribable_categories()},
        "limits": {
            "max_webhooks": MAX_WEBHOOKS, "max_attempts": MAX_ATTEMPTS, "retry_delays_seconds": list(RETRY_DELAYS),
            "disable_after_failures": DISABLE_AFTER, "max_pending_per_webhook": MAX_PENDING, "retention_days": RETENTION_DAYS,
        },
    }  # fmt: skip


def list_webhooks(ctx):
    rows = ctx.conn.execute("SELECT * FROM webhooks ORDER BY id").fetchall()
    counts = _counts(ctx.conn, [r["id"] for r in rows])
    return {"items": [to_dict(r, counts[r["id"]]) for r in rows], **info()}


def get_webhook(ctx, webhook_id):
    row = _get_row(ctx.conn, webhook_id)
    return to_dict(row, _counts(ctx.conn, [row["id"]])[row["id"]])


def _check_url(c, ctx):
    if "url" in c.out:
        try:
            netguard.validate_url(c.out["url"], _allow_private(ctx))
        except ValueError as exc:
            c.errors["url"] = str(exc)


def _check_project(ctx, key):
    if key is None:
        return None
    if not isinstance(key, str) or not PROJECT_KEY_RE.match(key):
        raise ValidationError({"project": "must be a project key or null"})
    row = ctx.conn.execute("SELECT key FROM projects WHERE key = ?", (key.upper(),)).fetchone()
    if row is None:
        raise ValidationError({"project": "no project with key %s" % key.upper()})
    return row["key"]


def _name_free(conn, name, own_id=None):
    row = conn.execute("SELECT id FROM webhooks WHERE name = ?", (name,)).fetchone()
    if row is not None and row["id"] != own_id:
        raise Conflict("A webhook named %r already exists" % name, code="webhook_name_taken")


def create_webhook(ctx, data):
    c = Checker(data, {"name", "url", "events", "project", "secret", "active"})
    c.text("name", required=True, min_len=1, max_len=60)
    c.text("url", required=True, max_len=netguard.MAX_URL_LENGTH)
    c.text("secret", min_len=16, max_len=200)
    c.boolean("active")
    c.raw("events", required=True)
    c.raw("project")
    _check_url(c, ctx)
    if "name" in c.out and not NAME_RE.match(c.out["name"]):
        c.errors["name"] = "must not contain control characters"
    if "secret" in c.out and not c.out["secret"].isascii():
        c.errors["secret"] = "must be plain ASCII"
    v = c.result()
    v["events"] = _check_events(v["events"])
    v["project"] = _check_project(ctx, v.get("project"))
    if ctx.conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] >= MAX_WEBHOOKS:
        raise Conflict("At most %d webhooks are allowed. Delete one first." % MAX_WEBHOOKS, code="webhook_limit_reached")
    _name_free(ctx.conn, v["name"])
    secret = v.get("secret") or new_secret()
    cur = ctx.conn.execute(
        "INSERT INTO webhooks (name, url, secret, events, project_key, active, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (v["name"], v["url"], secret, json.dumps(v["events"]), v["project"], int(v.get("active", True)), ctx.actor_id, ctx.now_iso, ctx.now_iso),
    )
    activity.log(ctx, "webhook.created", details={"id": cur.lastrowid, "name": v["name"], "host": _host(v["url"]), "events": v["events"], "project": v["project"]})
    out = get_webhook(ctx, cur.lastrowid)
    out["secret"] = secret  # the only time it is shown
    return out


def update_webhook(ctx, webhook_id, data):
    row = _get_row(ctx.conn, webhook_id)
    c = Checker(data, {"name", "url", "events", "project", "active"}, partial=True)
    c.text("name", min_len=1, max_len=60)
    c.text("url", max_len=netguard.MAX_URL_LENGTH)
    c.boolean("active")
    c.raw("events")
    c.raw("project")
    _check_url(c, ctx)
    if "name" in c.out and not NAME_RE.match(c.out["name"]):
        c.errors["name"] = "must not contain control characters"
    v = c.result()
    if "events" in v:
        v["events"] = _check_events(v["events"])
    if "project" in v:
        v["project"] = _check_project(ctx, v["project"])
    if "name" in v:
        _name_free(ctx.conn, v["name"], row["id"])
    changes, args = {}, {}
    for field, column in (("name", "name"), ("url", "url"), ("project", "project_key")):
        if field in v and v[field] != row[column]:
            changes[field] = [row[column] if field != "url" else _host(row[column]), v[field] if field != "url" else _host(v[field])]
            args[column] = v[field]
    if "events" in v and v["events"] != json.loads(row["events"]):
        changes["events"] = [json.loads(row["events"]), v["events"]]
        args["events"] = json.dumps(v["events"])
    if "active" in v and int(v["active"]) != row["active"]:
        changes["active"] = [bool(row["active"]), v["active"]]
        args["active"] = int(v["active"])
        if v["active"]:  # switching back on is a fresh start
            args.update(failure_streak=0, disabled_reason=None, disabled_at=None)
    if args:
        args["updated_at"] = ctx.now_iso
        ctx.conn.execute("UPDATE webhooks SET %s WHERE id = ?" % ", ".join("%s = ?" % k for k in args), list(args.values()) + [row["id"]])
        if args.get("active") == 0:
            _cancel_open(ctx.conn, row["id"], ctx.now_iso, "webhook_disabled")
        activity.log(ctx, "webhook.updated", details={"id": row["id"], "name": row["name"], "changes": changes})
    return get_webhook(ctx, row["id"])


def delete_webhook(ctx, webhook_id):
    row = _get_row(ctx.conn, webhook_id)
    ctx.conn.execute("DELETE FROM webhooks WHERE id = ?", (row["id"],))
    activity.log(ctx, "webhook.deleted", details={"id": row["id"], "name": row["name"], "host": _host(row["url"])})


def rotate_secret(ctx, webhook_id):
    row = _get_row(ctx.conn, webhook_id)
    secret = new_secret()
    ctx.conn.execute("UPDATE webhooks SET secret = ?, updated_at = ? WHERE id = ?", (secret, ctx.now_iso, row["id"]))
    activity.log(ctx, "webhook.secret_rotated", details={"id": row["id"], "name": row["name"]})
    return {"id": row["id"], "secret": secret, "secret_hint": _hint(secret)}


def send_test(ctx, webhook_id):
    """Queue a ``webhook.ping`` for this webhook only (it works even when the webhook is not subscribed to it)."""
    row = _get_row(ctx.conn, webhook_id)
    if not row["active"]:
        raise Conflict("The webhook is switched off. Switch it on first.", code="webhook_inactive")
    payload = {
        "event": "webhook.ping", "event_id": None, "occurred_at": ctx.now_iso, "project": None, "task": None,
        "actor": {"username": ctx.user["username"], "display_name": ctx.user["display_name"]} if ctx.user else None,
        "details": {"message": "This is a test delivery from Zylker Projects."},
    }  # fmt: skip
    delivery_id = _insert_delivery(ctx.conn, row["id"], "webhook.ping", None, json.dumps(payload, separators=(",", ":")), ctx.now_iso)
    activity.log(ctx, "webhook.test", details={"id": row["id"], "name": row["name"], "delivery": delivery_id})
    return _delivery_dict(ctx.conn.execute("SELECT * FROM webhook_deliveries WHERE id = ?", (delivery_id,)).fetchone())


def _host(url):
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


# ------------------------------------------------------------------------------ the delivery log


def _delivery_dict(row, detail=False):
    out = {
        "id": row["id"], "webhook_id": row["webhook_id"], "event": row["event"], "event_id": row["event_id"], "status": row["status"],
        "attempts": row["attempts"], "next_attempt_at": row["next_attempt_at"], "response_status": row["response_status"],
        "error": row["error"], "duration_ms": row["duration_ms"], "redelivery_of": row["redelivery_of"],
        "created_at": row["created_at"], "finished_at": row["finished_at"],
    }  # fmt: skip
    if detail:
        out["payload"] = json.loads(row["payload"])
        out["attempt_log"] = json.loads(row["attempt_log"])
    return out


def list_deliveries(ctx, webhook_id, params):
    _get_row(ctx.conn, webhook_id)
    where, args = ["webhook_id = ?"], [webhook_id]
    errors = {}
    statuses = [s.strip() for s in (params.get("status") or "").split(",") if s.strip()]
    if any(s not in STATUSES for s in statuses):
        errors["status"] = "must be a comma-separated list of: " + ", ".join(STATUSES)
    elif statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        args += statuses
    event = (params.get("event") or "").strip()
    if event:
        if not (ACTION_RE.match(event)):
            errors["event"] = "must look like task.created"
        else:
            where.append("event = ?")
            args.append(event)
    limit = 25
    if params.get("limit") is not None:
        try:
            limit = int(params["limit"])
            if not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            errors["limit"] = "must be an integer between 1 and 100"
    if params.get("before") is not None:
        try:
            args.append(int(params["before"]))
            where.append("id < ?")
        except ValueError:
            errors["before"] = "must be a delivery id"
    if errors:
        raise ValidationError(errors)
    rows = ctx.conn.execute("SELECT * FROM webhook_deliveries WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where), args + [limit]).fetchall()
    items = [_delivery_dict(r) for r in rows]
    return {"items": items, "next_before": items[-1]["id"] if len(items) == limit else None}


def get_delivery(ctx, webhook_id, delivery_id):
    _get_row(ctx.conn, webhook_id)
    row = ctx.conn.execute("SELECT * FROM webhook_deliveries WHERE id = ? AND webhook_id = ?", (delivery_id, webhook_id)).fetchone()
    if row is None:
        raise NotFound("Delivery not found", code="delivery_not_found")
    return _delivery_dict(row, detail=True)


def redeliver(ctx, webhook_id, delivery_id):
    """Send the same event again as a new delivery (the original stays in the log)."""
    hook = _get_row(ctx.conn, webhook_id)
    row = ctx.conn.execute("SELECT * FROM webhook_deliveries WHERE id = ? AND webhook_id = ?", (delivery_id, webhook_id)).fetchone()
    if row is None:
        raise NotFound("Delivery not found", code="delivery_not_found")
    if not hook["active"]:
        raise Conflict("The webhook is switched off. Switch it on first.", code="webhook_inactive")
    new_id = _insert_delivery(ctx.conn, hook["id"], row["event"], row["event_id"], row["payload"], ctx.now_iso, redelivery_of=row["id"])
    activity.log(ctx, "webhook.redelivered", details={"id": hook["id"], "name": hook["name"], "delivery": row["id"], "new_delivery": new_id})
    return _delivery_dict(ctx.conn.execute("SELECT * FROM webhook_deliveries WHERE id = ?", (new_id,)).fetchone())


# ------------------------------------------------------------------------------ the worker


def _cancel_open(conn, hook_id, now_iso, reason):
    conn.execute(
        "UPDATE webhook_deliveries SET status = 'cancelled', error = ?, next_attempt_at = NULL, finished_at = ? "
        "WHERE webhook_id = ? AND status IN ('pending','sending')",
        (reason, now_iso, hook_id),
    )


def _claim(conn, now, limit):
    now_iso, stale = iso(now), iso(now - timedelta(seconds=STALE_CLAIM_SECONDS))
    jobs = []
    with transaction(conn, write=True):
        rows = conn.execute(
            "SELECT d.*, w.url AS hook_url, w.secret AS hook_secret, w.name AS hook_name, w.active AS hook_active "
            "FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id "
            "WHERE (d.status = 'pending' AND d.next_attempt_at <= ?) OR (d.status = 'sending' AND d.claimed_at <= ?) "
            "ORDER BY d.id LIMIT ?",
            (now_iso, stale, limit),
        ).fetchall()
        for r in rows:
            if not r["hook_active"]:
                conn.execute("UPDATE webhook_deliveries SET status = 'cancelled', error = 'webhook_disabled', next_attempt_at = NULL, finished_at = ? WHERE id = ?", (now_iso, r["id"]))
                jobs.append((r, "cancelled"))
                continue
            conn.execute("UPDATE webhook_deliveries SET status = 'sending', claimed_at = ?, attempts = attempts + 1 WHERE id = ?", (now_iso, r["id"]))
            jobs.append((r, "send"))
    return jobs


def _request(job, attempt, now):
    payload = json.loads(job["payload"])
    envelope = {"delivery_id": job["id"], "attempt": attempt, "webhook": {"id": job["webhook_id"], "name": job["hook_name"]}, **payload}
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    timestamp = int(now.timestamp())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ZylkerProjects-Webhooks/" + __version__,
        "X-Zylker-Event": job["event"],
        "X-Zylker-Delivery": str(job["id"]),
        "X-Zylker-Attempt": str(attempt),
        "X-Zylker-Timestamp": str(timestamp),
        "X-Zylker-Signature": sign(job["hook_secret"], timestamp, body),
    }
    return body, headers


def _record(conn, now, job, result):
    """Store the outcome of one attempt. Returns ``'delivered'``, ``'retry'`` or ``'failed'`` (plus a disabled flag)."""
    now_iso = iso(now)
    attempt = job["attempts"] + 1  # _claim already counted it in the database, not in our copy of the row
    entry = {"at": now_iso, "attempt": attempt, "status": result.status, "error": result.error or (None if result.ok else "http_%d" % result.status), "ms": result.ms}
    log_text = json.dumps((json.loads(job["attempt_log"]) + [entry])[-MAX_ATTEMPTS:])
    disabled = None
    with transaction(conn, write=True):
        hook = conn.execute("SELECT * FROM webhooks WHERE id = ?", (job["webhook_id"],)).fetchone()
        if hook is None:  # deleted while we were sending: its deliveries went with it
            return "gone", None
        if result.ok:
            outcome = "delivered"
            conn.execute(
                "UPDATE webhook_deliveries SET status = 'delivered', response_status = ?, error = NULL, duration_ms = ?, attempt_log = ?, "
                "next_attempt_at = NULL, finished_at = ? WHERE id = ?",
                (result.status, result.ms, log_text, now_iso, job["id"]),
            )
            conn.execute(
                "UPDATE webhooks SET failure_streak = 0, last_attempt_at = ?, last_success_at = ?, last_status = ?, last_error = NULL WHERE id = ?",
                (now_iso, now_iso, result.status, hook["id"]),
            )
            return outcome, None
        gone = result.status == 410
        error = entry["error"]
        if attempt >= MAX_ATTEMPTS or gone or not hook["active"]:
            outcome = "failed"
            conn.execute(
                "UPDATE webhook_deliveries SET status = 'failed', response_status = ?, error = ?, duration_ms = ?, attempt_log = ?, "
                "next_attempt_at = NULL, finished_at = ? WHERE id = ?",
                (result.status, error, result.ms, log_text, now_iso, job["id"]),
            )
        else:
            outcome = "retry"
            retry_at = iso(now + timedelta(seconds=RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]))
            conn.execute(
                "UPDATE webhook_deliveries SET status = 'pending', response_status = ?, error = ?, duration_ms = ?, attempt_log = ?, "
                "next_attempt_at = ?, claimed_at = NULL WHERE id = ?",
                (result.status, error, result.ms, log_text, retry_at, job["id"]),
            )
        streak = hook["failure_streak"] + 1
        conn.execute(
            "UPDATE webhooks SET failure_streak = ?, last_attempt_at = ?, last_status = ?, last_error = ? WHERE id = ?",
            (streak, now_iso, result.status, error, hook["id"]),
        )
        if hook["active"] and (gone or streak >= DISABLE_AFTER):
            reason = "gone" if gone else "too_many_failures"
            disabled = {"id": hook["id"], "name": hook["name"], "reason": reason}
            conn.execute(
                "UPDATE webhooks SET active = 0, disabled_reason = ?, disabled_at = ? WHERE id = ?", (reason, now_iso, hook["id"])
            )
            _cancel_open(conn, hook["id"], now_iso, "webhook_disabled")
            audit.record(conn, now_iso, "webhook.disabled", details={"id": hook["id"], "name": hook["name"], "reason": reason, "failures": streak})
            if outcome == "retry":
                outcome = "cancelled"  # its retry was just cancelled along with the rest
    return outcome, disabled


def run_due(db_path, now, send, *, limit=20, workers=4):
    """Deliver what is due. Safe to run from several processes at once: rows are claimed in a transaction.

    ``send(url, body, headers, timeout)`` returns a ``netguard.SendResult``; the network is only touched here, and
    never while a database transaction is open.
    """
    report = {"delivered": 0, "retrying": 0, "failed": 0, "cancelled": 0, "disabled": [], "pruned": 0}
    conn = connect(db_path)
    try:
        due = conn.execute(
            "SELECT 1 FROM webhook_deliveries WHERE (status = 'pending' AND next_attempt_at <= ?) OR (status = 'sending' AND claimed_at <= ?) LIMIT 1",
            (iso(now), iso(now - timedelta(seconds=STALE_CLAIM_SECONDS))),
        ).fetchone()
        if due is not None:
            jobs = _claim(conn, now, limit)
            todo = [r for r, what in jobs if what == "send"]
            report["cancelled"] += len(jobs) - len(todo)
            prepared = [(r, *_request(r, r["attempts"] + 1, now)) for r in todo]

            def attempt(item):
                row, body, headers = item
                return send(row["hook_url"], body, headers, netguard.TIMEOUT_SECONDS)

            if len(prepared) > 1 and workers > 1:
                with ThreadPoolExecutor(max_workers=min(workers, len(prepared))) as pool:
                    results = list(pool.map(attempt, prepared))
            else:
                results = [attempt(p) for p in prepared]
            for (row, _, _), result in zip(prepared, results, strict=True):
                outcome, disabled = _record(conn, now, row, result)
                if outcome == "delivered":
                    report["delivered"] += 1
                elif outcome == "retry":
                    report["retrying"] += 1
                elif outcome == "failed":
                    report["failed"] += 1
                elif outcome == "cancelled":
                    report["cancelled"] += 1
                if disabled:
                    report["disabled"].append(disabled)
        report["pruned"] = prune(conn, now)
    finally:
        conn.close()
    return report


def prune(conn, now):
    """Drop finished deliveries older than the retention period."""
    cutoff = iso(now - timedelta(days=RETENTION_DAYS))
    if conn.execute("SELECT 1 FROM webhook_deliveries WHERE finished_at < ? LIMIT 1", (cutoff,)).fetchone() is None:
        return 0
    with transaction(conn, write=True):
        return conn.execute("DELETE FROM webhook_deliveries WHERE finished_at < ?", (cutoff,)).rowcount
