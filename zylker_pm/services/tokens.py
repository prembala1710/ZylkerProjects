"""API tokens: narrower, expiring credentials for integrations.

A user keeps their primary token (never expires, full powers). On top of it they can mint API tokens that

* expire (1-365 days, never "forever"),
* can be capped to a lower role than the user's own (a "member" token for a manager's CI job),
* can be read-only (every method but GET and HEAD is refused),
* can be limited to a list of addresses / networks,
* are tracked (last used, from where, how often) and can be revoked at any time.

Safeguards: the token value is shown once and only its hash is stored; a token can never create or revoke tokens, nor
rotate its owner's primary token (managing credentials takes the primary token), so a leaked integration key cannot
mint itself a successor; deactivating the user kills every token at once; a user's role being lowered lowers what
their tokens may do.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import timedelta

from .. import auth
from ..domain import ROLE_RANK, ROLES
from ..errors import Conflict, Forbidden, NotFound, ValidationError
from ..util import Checker, iso, truncate
from . import activity, users

MAX_ACTIVE_PER_USER = 10
DEFAULT_DAYS, MAX_DAYS = 30, 365
MAX_ADDRESSES = 10
EXPIRING_SOON_DAYS = 7
KEEP_FINISHED_DAYS = 30  # expired and revoked tokens stay listed (and attributable in the audit log) this long
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,60}$")
STATUSES = ("active", "expired", "revoked")


def limits():
    return {
        "max_active_per_user": MAX_ACTIVE_PER_USER, "default_days": DEFAULT_DAYS, "max_days": MAX_DAYS,
        "max_addresses": MAX_ADDRESSES, "keep_finished_days": KEEP_FINISHED_DAYS,
    }  # fmt: skip


def _hint(token):
    return auth.API_TOKEN_PREFIX + "..." + token[-4:]


def _require_primary(ctx):
    if auth.token_of(ctx.user) is not None:
        raise Forbidden("API tokens are managed with your primary token, not with another API token", code="primary_token_required")


def _status(row, now_iso):
    if row["revoked_at"]:
        return "revoked"
    return "expired" if row["expires_at"] <= now_iso else "active"


def to_dict(row, ctx):
    now_iso = ctx.now_iso
    status = _status(row, now_iso)
    soon = iso(ctx.now + timedelta(days=EXPIRING_SOON_DAYS))
    return {
        "id": row["id"], "name": row["name"], "hint": row["hint"], "user": row["owner"], "role_cap": row["role_cap"],
        "read_only": bool(row["read_only"]), "allowed_ips": json.loads(row["allowed_ips"]), "status": status,
        "expires_at": row["expires_at"], "expires_soon": status == "active" and row["expires_at"] <= soon,
        "created_at": row["created_at"], "last_used_at": row["last_used_at"], "last_used_ip": row["last_used_ip"],
        "use_count": row["use_count"], "revoked_at": row["revoked_at"],
    }  # fmt: skip


_SELECT = "SELECT t.*, u.username AS owner FROM api_tokens t JOIN users u ON u.id = t.user_id"


def _get(ctx, token_id):
    row = ctx.conn.execute(_SELECT + " WHERE t.id = ?", (token_id,)).fetchone()
    if row is None:
        raise NotFound("Token not found", code="token_not_found")
    return row


def _may_see(ctx, row):
    return ctx.user is None or ctx.has_role("admin") or row["user_id"] == ctx.user["id"]


def _networks(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ADDRESSES:
        raise ValidationError({"allowed_ips": "must be a list of at most %d addresses or networks" % MAX_ADDRESSES})
    out = []
    for item in value:
        try:
            net = str(ipaddress.ip_network(str(item).strip(), strict=False)) if isinstance(item, str) else None
        except ValueError:
            net = None
        if net is None:
            raise ValidationError({"allowed_ips": "%r is not an IP address or network such as 203.0.113.0/24" % truncate(item, 40)})
        if net not in out:
            out.append(net)
    return out


def create_token(ctx, data, owner=None):
    """Mint a token. Over the API the owner is the caller (admins may name another user, e.g. a service account);
    the command line passes ``owner``. The response carries the token itself - once."""
    _require_primary(ctx)
    c = Checker(data, {"name", "expires_in_days", "role_cap", "read_only", "allowed_ips", "user"})
    c.text("name", required=True, min_len=1, max_len=60)
    c.integer("expires_in_days", minimum=1, maximum=MAX_DAYS)
    c.choice("role_cap", ROLES)
    c.boolean("read_only")
    c.text("user", max_len=60)
    c.raw("allowed_ips")
    if "name" in c.out and not NAME_RE.match(c.out["name"]):
        c.errors["name"] = "must not contain control characters"
    v = c.result()
    if owner is None:
        owner = ctx.user
        if v.get("user") and ctx.user is not None and v["user"].lower() != ctx.user["username"].lower():
            if not ctx.has_role("admin"):
                raise Forbidden("Only an administrator can create a token for someone else", code="insufficient_role")
            owner = users.find_by_username(ctx.conn, v["user"])
            if owner is None:
                raise ValidationError({"user": "no such user"})
    if owner is None:
        raise ValidationError({"user": "a user is required"})
    if not owner["active"]:
        raise Conflict("A deactivated user cannot have tokens", code="user_inactive")
    cap = v.get("role_cap", owner["role"])
    if ROLE_RANK[cap] > ROLE_RANK[owner["role"]]:
        raise ValidationError({"role_cap": "cannot be higher than the user's own role (%s)" % owner["role"]})
    networks = _networks(v.get("allowed_ips"))
    days = v.get("expires_in_days", DEFAULT_DAYS)

    _prune(ctx)
    active = ctx.conn.execute(
        "SELECT COUNT(*) FROM api_tokens WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?", (owner["id"], ctx.now_iso)
    ).fetchone()[0]
    if active >= MAX_ACTIVE_PER_USER:
        raise Conflict("At most %d active tokens per user. Revoke one first." % MAX_ACTIVE_PER_USER, code="token_limit_reached")
    taken = ctx.conn.execute(
        "SELECT 1 FROM api_tokens WHERE user_id = ? AND name = ? COLLATE NOCASE AND revoked_at IS NULL AND expires_at > ?", (owner["id"], v["name"], ctx.now_iso)
    ).fetchone()
    if taken:
        raise Conflict("%s already has an active token named %r" % (owner["username"], v["name"]), code="token_name_taken")

    token = auth.new_api_token()
    expires = iso(ctx.now + timedelta(days=days))
    cur = ctx.conn.execute(
        "INSERT INTO api_tokens (user_id, name, token_hash, hint, role_cap, read_only, allowed_ips, created_by, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (owner["id"], v["name"], auth.hash_token(token), _hint(token), cap, int(v.get("read_only", False)), json.dumps(networks), ctx.actor_id, ctx.now_iso, expires),
    )
    activity.log(ctx, "token.created", details={
        "id": cur.lastrowid, "name": v["name"], "user": owner["username"], "hint": _hint(token), "role_cap": cap,
        "read_only": bool(v.get("read_only", False)), "allowed_ips": networks, "expires_at": expires,
    })  # fmt: skip
    out = to_dict(ctx.conn.execute(_SELECT + " WHERE t.id = ?", (cur.lastrowid,)).fetchone(), ctx)
    out["token"] = token  # the only time it is shown
    return out


def _prune(ctx):
    cutoff = iso(ctx.now - timedelta(days=KEEP_FINISHED_DAYS))
    ctx.conn.execute("DELETE FROM api_tokens WHERE expires_at < ? OR revoked_at < ?", (cutoff, cutoff))


def list_tokens(ctx, params):
    """Your tokens; ``?user=NAME`` or ``?user=*`` (everyone) for administrators. ``?status=active,expired,revoked``."""
    _require_primary(ctx)
    scope = (params.get("user") or "").strip()
    status = [s.strip() for s in (params.get("status") or "").split(",") if s.strip()]
    if any(s not in STATUSES for s in status):
        raise ValidationError({"status": "must be a comma-separated list of: " + ", ".join(STATUSES)})
    where, args = "", []
    if ctx.user is not None and (not scope or scope.lower() == ctx.user["username"].lower()):
        where, args = " WHERE t.user_id = ?", [ctx.user["id"]]
    else:
        if not ctx.has_role("admin"):
            raise Forbidden("Only an administrator can list other people's tokens", code="insufficient_role")
        if scope and scope != "*":
            target = users.find_by_username(ctx.conn, scope)
            if target is None:
                raise ValidationError({"user": "no such user"})
            where, args = " WHERE t.user_id = ?", [target["id"]]
    rows = ctx.conn.execute(_SELECT + where + " ORDER BY t.id DESC", args).fetchall()
    items = [to_dict(r, ctx) for r in rows]
    if status:
        items = [i for i in items if i["status"] in status]
    return {"items": items, "limits": limits()}


def get_token(ctx, token_id):
    _require_primary(ctx)
    row = _get(ctx, token_id)
    if not _may_see(ctx, row):
        raise NotFound("Token not found", code="token_not_found")
    return to_dict(row, ctx)


def current(ctx):
    """How this request is authenticated."""
    tok = auth.token_of(ctx.user)
    if tok is None:
        return {"type": "primary", "role": ctx.user["role"] if ctx.user is not None else None}
    row = _get(ctx, tok["id"])
    return {"type": "api_token", "role": ctx.user["role"], "owner_role": ctx.user["real_role"], **to_dict(row, ctx)}


def revoke_token(ctx, token_id):
    _require_primary(ctx)
    row = _get(ctx, token_id)
    if not _may_see(ctx, row):
        raise NotFound("Token not found", code="token_not_found")  # someone else's token does not exist as far as you can tell
    if not row["revoked_at"]:
        ctx.conn.execute("UPDATE api_tokens SET revoked_at = ?, revoked_by = ? WHERE id = ?", (ctx.now_iso, ctx.actor_id, row["id"]))
        activity.log(ctx, "token.revoked", details={"id": row["id"], "name": row["name"], "user": row["owner"], "hint": row["hint"]})
    return to_dict(_get(ctx, token_id), ctx)


def revoke_all(ctx, username=None):
    """Revoke every active token of a user (yours by default; administrators may name anyone)."""
    _require_primary(ctx)
    target = ctx.user
    if username and (ctx.user is None or username.lower() != ctx.user["username"].lower()):
        if ctx.user is not None and not ctx.has_role("admin"):
            raise Forbidden("Only an administrator can revoke other people's tokens", code="insufficient_role")
        target = users.find_by_username(ctx.conn, username)
        if target is None:
            raise ValidationError({"user": "no such user"})
    if target is None:
        raise ValidationError({"user": "a user is required"})
    cur = ctx.conn.execute(
        "UPDATE api_tokens SET revoked_at = ?, revoked_by = ? WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?",
        (ctx.now_iso, ctx.actor_id, target["id"], ctx.now_iso),
    )
    if cur.rowcount:
        activity.log(ctx, "token.revoked_all", details={"user": target["username"], "count": cur.rowcount})
    return {"user": target["username"], "revoked": cur.rowcount}
