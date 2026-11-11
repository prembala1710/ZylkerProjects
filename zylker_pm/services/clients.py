"""MSP clients: the organisations a managed-service provider works for, and what each of them may see of its projects.

A *client* groups projects (a project belongs to at most one client) and has *contacts*: people on the client's side who sign
in with a token of their own. A contact is an ordinary user row whose id is also in ``client_users``, and that one fact changes
what the server lets it do (see ``app.App.run``): a contact may call only the routes marked ``client=True`` - who it is, the
server's enumerations and the client portal - and nothing else, whatever its role. The portal shows a project only when it is
linked to the contact's client *and* has been shared (``share`` is ``summary`` or ``full``); nothing is shared until a manager
says so.

A contact cannot be turned back into a staff member: no endpoint removes the link, a client that still has contacts cannot be
deleted, and a contact who is no longer wanted is deactivated. The database says the same (the link cannot be deleted or moved,
and neither the client nor the user it points at can be deleted), so no sequence of API calls - and no bug in one - can widen
what a contact's token reaches.

The tables are created like the baseline tables: with ``CREATE TABLE IF NOT EXISTS`` by :func:`ensure_schema` when an ``App``
starts, independently of ``PRAGMA user_version``. They are purely additive.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict

from ..db import connect
from ..errors import Conflict, NotFound, ValidationError
from ..util import EMAIL_RE, Checker, truncate
from . import activity, projection, projects, reports, users

# Control characters and invisible direction overrides could make a name read as something else; a message may hold line breaks.
_INVISIBLE = "\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2060\u2066-\u2069\ufeff"
BAD_NAME_RE = re.compile("[\x00-\x1f\x7f-\x9f%s]" % _INVISIBLE)
BAD_TEXT_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f%s]" % _INVISIBLE)

SHARE_LEVELS = ("none", "summary", "full")
MAX_CLIENTS = 500
MAX_NAME_CHARS = 80
MAX_UPDATE_CHARS = 1000

CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_name ON clients (name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS client_projects (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    share TEXT NOT NULL DEFAULT 'none' CHECK (share IN ('none','summary','full')),
    update_text TEXT NOT NULL DEFAULT '',
    update_by INTEGER REFERENCES users(id),
    update_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_projects_client ON client_projects (client_id);
CREATE TABLE IF NOT EXISTS client_users (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_client_users_client ON client_users (client_id);
CREATE TRIGGER IF NOT EXISTS client_users_stay BEFORE DELETE ON client_users
BEGIN SELECT RAISE(ABORT, 'a client contact cannot be removed or moved to another client'); END;
CREATE TRIGGER IF NOT EXISTS client_users_do_not_move BEFORE UPDATE ON client_users
BEGIN SELECT RAISE(ABORT, 'a client contact cannot be removed or moved to another client'); END;
"""


def ensure_schema(path: str) -> None:
    """Create the client tables in an initialised database file (no-op for new, missing or in-memory ones)."""
    if path == ":memory:" or not os.path.exists(path):
        return
    conn = connect(path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] > 0:
            conn.executescript(CLIENT_SCHEMA)
    finally:
        conn.close()


def has_tables(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'client_users'").fetchone() is not None


def require_schema(ctx):
    if not has_tables(ctx.conn):
        raise Conflict(
            "The client tables are missing from this database. Restart the server (or run `zylker-pm init`) to create them.",
            code="clients_not_initialised",
        )


def restriction(conn, user_id):
    """The client account a user belongs to - ``{"id", "name", "active"}`` - or ``None`` for staff.

    Only a missing table means "nobody is a client contact"; any other failure propagates, so a request that cannot be
    classified is an error and never a request from staff.
    """
    if not has_tables(conn):
        return None
    row = conn.execute(
        "SELECT c.id, c.name, c.active FROM client_users u JOIN clients c ON c.id = u.client_id WHERE u.user_id = ?", (user_id,)
    ).fetchone()
    return None if row is None else {"id": row["id"], "name": row["name"], "active": bool(row["active"])}


# ------------------------------------------------------------------------------------------------------ serialising


def get_row(conn, client_id):
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        raise NotFound("Client not found", code="client_not_found")
    return row


def _client_dict(row):
    return {"id": row["id"], "name": row["name"], "active": bool(row["active"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}


def update_dict(row, staff=True):
    """The latest message a manager left for the client on a project, or ``None``."""
    if not row["update_text"]:
        return None
    by = users.ref(row["update_by"], row["u_username"], row["u_name"]) if staff else (row["u_name"] if row["update_by"] else None)
    return {"text": row["update_text"], "by": by, "at": row["update_at"]}


LINK_SELECT = (
    "SELECT cp.client_id, cp.share, cp.update_text, cp.update_by, cp.update_at, p.id AS project_id, p.key, p.name, p.status, "
    "p.due_date, p.start_date, p.owner_id, o.display_name AS owner_name, u.username AS u_username, u.display_name AS u_name "
    "FROM client_projects cp JOIN projects p ON p.id = cp.project_id LEFT JOIN users u ON u.id = cp.update_by "
    "LEFT JOIN users o ON o.id = p.owner_id "
)


def _contact_dict(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "email": row["email"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------------------------------------- standing


def _standing(ctx):
    """Health and forecast of every project that is not archived, by key: one portfolio report and one projection for all."""
    out = {}
    for item in reports.portfolio(ctx)["projects"]:
        out[item["key"]] = {
            "health": item["health"],
            "open_tasks": item["open_tasks"],
            "overdue": item["overdue"],
            "progress": item["progress"]["tasks_pct"],
            "forecast": None,
        }
    try:
        for item in projection.portfolio_projection(ctx, {})["projects"]:
            if item["project"] in out and item["state"] == "projected":
                out[item["project"]]["forecast"] = {k: item[k] for k in ("verdict", "finish", "late_by_days", "late_tasks")}
    except Conflict:
        pass  # more open work than a projection will schedule: the overview simply has no forecast
    return out


def _worst_health(standings):
    statuses = [s["health"]["status"] for s in standings]
    return min(statuses, key=lambda status: reports.HEALTH_ORDER[status], default=None)


def _worst_forecast(standings):
    forecasts = [s["forecast"] for s in standings if s["forecast"]]
    if not forecasts:
        return None
    finishes = [f["finish"] for f in forecasts if f["finish"]]
    lates = [f["late_by_days"] for f in forecasts if f["late_by_days"] is not None]
    return {
        "verdict": min((f["verdict"] for f in forecasts), key=lambda v: projection.VERDICT_RANK.get(v, 4)),
        "late_by_days": max(lates, default=None),
        "late_tasks": sum(f["late_tasks"] for f in forecasts),
        "finish": max(finishes, default=None),
    }


# ----------------------------------------------------------------------------------------------- clients (for staff)


def list_clients(ctx):
    """Every client with a roll-up of its projects, the ones that need attention first."""
    require_schema(ctx)
    conn = ctx.conn
    rows = conn.execute("SELECT * FROM clients").fetchall()
    links = defaultdict(list)
    for r in conn.execute("SELECT cp.client_id, cp.share, p.key FROM client_projects cp JOIN projects p ON p.id = cp.project_id"):
        links[r["client_id"]].append(r)
    contacts = dict(conn.execute("SELECT client_id, COUNT(*) FROM client_users GROUP BY client_id").fetchall())
    standing = _standing(ctx)
    items = []
    for row in rows:
        mine = [standing[link["key"]] for link in links[row["id"]] if link["key"] in standing]  # archived projects are not in it
        items.append({
            **_client_dict(row),
            "projects": {
                "total": len(links[row["id"]]),
                "shared": sum(1 for link in links[row["id"]] if link["share"] != "none"),
                "open_tasks": sum(s["open_tasks"] for s in mine),
                "overdue_tasks": sum(s["overdue"] for s in mine),
            },
            "contacts": contacts.get(row["id"], 0),
            "health": _worst_health(mine),
            "forecast": _worst_forecast(mine),
        })  # fmt: skip
    items.sort(key=lambda i: (not i["active"], reports.HEALTH_ORDER.get(i["health"], len(reports.HEALTH_ORDER)), i["name"].lower()))
    return {"items": items, "limits": {"max_clients": MAX_CLIENTS, "max_name_chars": MAX_NAME_CHARS}}


def get_client(ctx, client_id):
    require_schema(ctx)
    conn = ctx.conn
    row = get_row(conn, client_id)
    standing = _standing(ctx)
    links = []
    for r in conn.execute(LINK_SELECT + "WHERE cp.client_id = ? ORDER BY p.key", (row["id"],)):
        s = standing.get(r["key"])  # None for an archived project
        links.append({
            "key": r["key"], "name": r["name"], "status": r["status"], "due_date": r["due_date"],
            "share": r["share"], "update": update_dict(r),
            "health": s["health"] if s else None, "progress_pct": s["progress"] if s else None,
            "open_tasks": s["open_tasks"] if s else None, "overdue_tasks": s["overdue"] if s else None,
            "forecast": s["forecast"] if s else None,
        })  # fmt: skip
    contacts = conn.execute(
        "SELECT u.* FROM client_users cu JOIN users u ON u.id = cu.user_id WHERE cu.client_id = ? ORDER BY u.display_name COLLATE NOCASE", (row["id"],)
    ).fetchall()
    free = conn.execute(
        "SELECT key, name, status FROM projects WHERE status <> 'archived' AND id NOT IN (SELECT project_id FROM client_projects) ORDER BY key"
    ).fetchall()
    return {
        **_client_dict(row),
        "projects": links,
        "contacts": [_contact_dict(c) for c in contacts],
        "available_projects": [{"key": p["key"], "name": p["name"], "status": p["status"]} for p in free],
    }


def _checked_name(c):
    if BAD_NAME_RE.search(c.out.get("name", "")):
        c.errors["name"] = "must not contain control or direction characters"


def _name_taken(conn, name, except_id=None):
    row = conn.execute("SELECT id FROM clients WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return row is not None and row["id"] != except_id


def create_client(ctx, data):
    require_schema(ctx)
    conn = ctx.conn
    c = Checker(data, {"name"})
    c.text("name", required=True, min_len=1, max_len=MAX_NAME_CHARS)
    _checked_name(c)
    v = c.result()
    if conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= MAX_CLIENTS:
        raise Conflict("At most %d clients can be created" % MAX_CLIENTS, code="too_many_clients")
    if _name_taken(conn, v["name"]):
        raise Conflict("There is already a client called %r" % v["name"], code="client_name_taken")
    cur = conn.execute(
        "INSERT INTO clients (name, active, created_by, created_at, updated_at) VALUES (?,1,?,?,?)", (v["name"], ctx.actor_id, ctx.now_iso, ctx.now_iso)
    )
    activity.log(ctx, "client.created", details={"id": cur.lastrowid, "name": v["name"]})
    return _client_dict(get_row(conn, cur.lastrowid))


def update_client(ctx, client_id, data):
    require_schema(ctx)
    conn = ctx.conn
    row = get_row(conn, client_id)
    c = Checker(data, {"name", "active"}, partial=True)
    c.text("name", min_len=1, max_len=MAX_NAME_CHARS)
    c.boolean("active")
    _checked_name(c)
    v = c.result()
    if "name" in v and _name_taken(conn, v["name"], row["id"]):
        raise Conflict("There is already a client called %r" % v["name"], code="client_name_taken")
    changes = {f: [bool(row[f]) if f == "active" else row[f], val] for f, val in v.items() if val != (bool(row[f]) if f == "active" else row[f])}
    if changes:
        conn.execute(
            "UPDATE clients SET %s, updated_at = ? WHERE id = ?" % ", ".join("%s = ?" % f for f in changes),
            [int(new) if f == "active" else new for f, (_, new) in changes.items()] + [ctx.now_iso, row["id"]],
        )
        activity.log(ctx, "client.updated", details={"id": row["id"], "name": v.get("name", row["name"]), "changes": changes})
    return _client_dict(get_row(conn, row["id"]))


def delete_client(ctx, client_id):
    """Only a client without contacts can go: contacts are never turned back into staff, and never deleted."""
    require_schema(ctx)
    conn = ctx.conn
    row = get_row(conn, client_id)
    if conn.execute("SELECT 1 FROM client_users WHERE client_id = ?", (row["id"],)).fetchone():
        raise Conflict(
            "Client %r still has contacts. Suspend the client instead (contacts cannot be removed, only deactivated)." % row["name"],
            code="client_has_contacts",
        )
    conn.execute("DELETE FROM clients WHERE id = ?", (row["id"],))  # its project links go with it; the projects stay
    activity.log(ctx, "client.deleted", details={"id": row["id"], "name": row["name"]})


# ----------------------------------------------------------------------------------- projects of a client (for staff)


def _link_row(conn, project_id):
    return conn.execute(LINK_SELECT + "WHERE cp.project_id = ?", (project_id,)).fetchone()


def _link_out(row):
    return {"client": row["client_id"], "key": row["key"], "name": row["name"], "status": row["status"], "share": row["share"], "update": update_dict(row)}


def put_project(ctx, client_id, key, data):
    """Link a project to the client (or change how it is shared). ``{"share"?, "update"?}``; nothing is shared by default."""
    require_schema(ctx)
    conn = ctx.conn
    client = get_row(conn, client_id)
    project = projects.get_row(conn, key)
    c = Checker(data, {"share", "update"})
    c.choice("share", SHARE_LEVELS)
    c.text("update", max_len=MAX_UPDATE_CHARS, nullable=True)
    if BAD_TEXT_RE.search(c.out.get("update") or ""):
        c.errors["update"] = "must not contain control or direction characters"
    v = c.result()
    link = conn.execute("SELECT * FROM client_projects WHERE project_id = ?", (project["id"],)).fetchone()
    if link is not None and link["client_id"] != client["id"]:
        raise Conflict(
            "Project %s already belongs to client %r. Remove it from there first." % (project["key"], get_row(conn, link["client_id"])["name"]),
            code="project_assigned_elsewhere",
        )
    if link is None:
        text = v.get("update") or ""
        conn.execute(
            "INSERT INTO client_projects (project_id, client_id, share, update_text, update_by, update_at) VALUES (?,?,?,?,?,?)",
            (project["id"], client["id"], v.get("share", "none"), text, ctx.actor_id if text else None, ctx.now_iso if text else None),
        )
        activity.log(ctx, "client.project_linked", project_id=project["id"], details={"client": client["name"], "share": v.get("share", "none")})
    else:
        sets, changes = {}, {}
        if "share" in v and v["share"] != link["share"]:
            sets["share"], changes["share"] = v["share"], [link["share"], v["share"]]
        if "update" in v and (v["update"] or "") != link["update_text"]:
            text = v["update"] or ""
            sets.update(update_text=text, update_by=ctx.actor_id if text else None, update_at=ctx.now_iso if text else None)
            changes["update"] = [truncate(link["update_text"]), truncate(text)]
        if sets:
            conn.execute("UPDATE client_projects SET %s WHERE project_id = ?" % ", ".join("%s = ?" % f for f in sets), list(sets.values()) + [project["id"]])
            activity.log(ctx, "client.project_updated", project_id=project["id"], details={"client": client["name"], "changes": changes})
    return _link_out(_link_row(conn, project["id"]))


def remove_project(ctx, client_id, key):
    require_schema(ctx)
    conn = ctx.conn
    client = get_row(conn, client_id)
    project = projects.get_row(conn, key)
    cur = conn.execute("DELETE FROM client_projects WHERE project_id = ? AND client_id = ?", (project["id"], client["id"]))
    if cur.rowcount == 0:
        raise NotFound("Project %s is not linked to this client" % project["key"], code="project_not_linked")
    activity.log(ctx, "client.project_unlinked", project_id=project["id"], details={"client": client["name"]})


# ---------------------------------------------------------------------------------------- contacts (for admins)


def _contact(conn, client_id, user_id):
    row = conn.execute(
        "SELECT u.* FROM client_users cu JOIN users u ON u.id = cu.user_id WHERE cu.client_id = ? AND cu.user_id = ?", (client_id, user_id)
    ).fetchone()
    if row is None:
        raise NotFound("This client has no such contact", code="contact_not_found")
    return row


def create_contact(ctx, client_id, data):
    """A new person on the client's side. Returns ``{"contact", "token"}``; the token is shown this once."""
    require_schema(ctx)
    conn = ctx.conn
    client = get_row(conn, client_id)
    Checker(data, {"username", "display_name", "email"}).result()  # nothing else (a role, a capacity) can be asked for
    user, token = users.create_user(ctx, {**data, "role": "viewer", "weekly_capacity_hours": 0})
    conn.execute("INSERT INTO client_users (user_id, client_id) VALUES (?,?)", (user["id"], client["id"]))
    activity.log(ctx, "user.client_contact_added", details={"client": client["name"], "username": user["username"]})
    return {"contact": _contact_dict(users.get_row(conn, user["id"])), "token": token}


def update_contact(ctx, client_id, user_id, data):
    require_schema(ctx)
    conn = ctx.conn
    client = get_row(conn, client_id)
    row = _contact(conn, client["id"], user_id)
    c = Checker(data, {"display_name", "email", "active"}, partial=True)
    c.text("display_name", min_len=1, max_len=100)
    c.text("email", max_len=200, nullable=True)
    c.boolean("active")
    v = c.result()
    if v.get("email") and not EMAIL_RE.match(v["email"]):
        raise ValidationError({"email": "must be a valid e-mail address"})
    changes = {f: [bool(row[f]) if f == "active" else row[f], val] for f, val in v.items() if val != (bool(row[f]) if f == "active" else row[f])}
    if changes:
        conn.execute(
            "UPDATE users SET %s WHERE id = ?" % ", ".join("%s = ?" % f for f in changes),
            [int(new) if f == "active" else new for f, (_, new) in changes.items()] + [row["id"]],
        )
        activity.log(ctx, "user.client_contact_updated", details={"client": client["name"], "user": row["username"], "changes": changes})
    return _contact_dict(users.get_row(conn, row["id"]))


def rotate_contact_token(ctx, client_id, user_id):
    require_schema(ctx)
    row = _contact(ctx.conn, get_row(ctx.conn, client_id)["id"], user_id)
    return {"contact": _contact_dict(row), "token": users.rotate_token(ctx, row["id"])}
