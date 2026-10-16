"""User accounts, roles and tokens."""

from __future__ import annotations

from .. import auth
from ..domain import ROLES
from ..errors import Conflict, Forbidden, NotFound, ValidationError
from ..util import EMAIL_RE, USERNAME_RE, Checker
from . import activity


def public(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "email": row["email"],
        "role": row["role"],
        "weekly_capacity_hours": row["weekly_capacity_hours"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


def ref(user_id, username, display_name):
    """Compact user reference embedded in tasks, comments, projects."""
    if user_id is None:
        return None
    return {"id": user_id, "username": username, "display_name": display_name}


def get_row(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise NotFound("User not found", code="user_not_found")
    return row


def find_by_username(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?", (str(username).strip(),)).fetchone()


def find_by_identity(conn, text):
    """Match username, e-mail or display name (case-insensitive); None if absent/ambiguous."""
    text = (text or "").strip()
    if not text:
        return None
    rows = conn.execute(
        "SELECT * FROM users WHERE username = ?1 COLLATE NOCASE OR email = ?1 COLLATE NOCASE "
        "OR display_name = ?1 COLLATE NOCASE",
        (text,),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def create_user(ctx, data):
    c = Checker(data, {"username", "display_name", "email", "role", "weekly_capacity_hours"})
    c.text("username", required=True, min_len=2, max_len=32)
    c.text("display_name", required=True, min_len=1, max_len=100)
    c.text("email", max_len=200, nullable=True)
    c.choice("role", ROLES)
    c.number("weekly_capacity_hours", minimum=0, maximum=168)
    v = c.result()
    username = v["username"].lower()
    errors = {}
    if not USERNAME_RE.match(username):
        errors["username"] = "2-32 chars: lowercase letters, digits, '.', '_' or '-'"
    if v.get("email") and not EMAIL_RE.match(v["email"]):
        errors["email"] = "must be a valid e-mail address"
    if errors:
        raise ValidationError(errors)
    if find_by_username(ctx.conn, username):
        raise Conflict("Username %r is already taken" % username, code="username_taken")
    token = auth.new_token()
    cur = ctx.conn.execute(
        "INSERT INTO users (username, display_name, email, role, weekly_capacity_hours, active, "
        "token_hash, created_at) VALUES (?,?,?,?,?,1,?,?)",
        (
            username,
            v["display_name"],
            v.get("email") or None,
            v.get("role", "member"),
            v.get("weekly_capacity_hours", 40),
            auth.hash_token(token),
            ctx.now_iso,
        ),
    )
    activity.log(ctx, "user.created", details={"username": username, "role": v.get("role", "member")})
    return public(get_row(ctx.conn, cur.lastrowid)), token


def list_users(ctx, active=None):
    sql, args = "SELECT * FROM users", []
    if active is not None:
        sql += " WHERE active = ?"
        args.append(1 if active else 0)
    return [public(r) for r in ctx.conn.execute(sql + " ORDER BY display_name COLLATE NOCASE", args)]


def _active_admin_count(conn, excluding_id):
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1 AND id <> ?", (excluding_id,)
    ).fetchone()[0]


def update_user(ctx, user_id, data):
    target = get_row(ctx.conn, user_id)
    is_admin = ctx.has_role("admin")
    is_self = ctx.user is not None and ctx.user["id"] == target["id"]
    if not (is_admin or is_self):
        raise Forbidden("You can only edit your own profile", code="insufficient_role")
    if not is_admin and isinstance(data, dict):
        restricted = [k for k in data if k not in ("display_name", "email")]
        if restricted:
            raise Forbidden("Only admins can change: " + ", ".join(sorted(restricted)), code="insufficient_role")
    c = Checker(data, {"display_name", "email", "role", "weekly_capacity_hours", "active"}, partial=True)
    c.text("display_name", min_len=1, max_len=100)
    c.text("email", max_len=200, nullable=True)
    c.choice("role", ROLES)
    c.number("weekly_capacity_hours", minimum=0, maximum=168)
    c.boolean("active")
    v = c.result()
    if v.get("email") and not EMAIL_RE.match(v["email"]):
        raise ValidationError({"email": "must be a valid e-mail address"})
    losing_admin = target["role"] == "admin" and target["active"] and (
        v.get("role", "admin") != "admin" or v.get("active", True) is False
    )
    if losing_admin and _active_admin_count(ctx.conn, target["id"]) == 0:
        raise Conflict("At least one active admin is required", code="last_admin")
    changes = {}
    for field, value in v.items():
        current = target[field]
        if field == "active":
            current = bool(current)
        if value != current:
            changes[field] = [current, value]
    if changes:
        sets = ", ".join("%s = ?" % f for f in changes)
        args = [int(c_[1]) if f == "active" else c_[1] for f, c_ in changes.items()]
        ctx.conn.execute("UPDATE users SET %s WHERE id = ?" % sets, args + [target["id"]])
        activity.log(ctx, "user.updated", details={"user": target["username"], "changes": changes})
    return public(get_row(ctx.conn, target["id"]))


def rotate_token(ctx, user_id):
    target = get_row(ctx.conn, user_id)
    if not (ctx.has_role("admin") or (ctx.user is not None and ctx.user["id"] == target["id"])):
        raise Forbidden("You can only rotate your own token", code="insufficient_role")
    token = auth.new_token()
    ctx.conn.execute("UPDATE users SET token_hash = ? WHERE id = ?", (auth.hash_token(token), target["id"]))
    activity.log(ctx, "user.token_rotated", details={"user": target["username"]})
    return token
