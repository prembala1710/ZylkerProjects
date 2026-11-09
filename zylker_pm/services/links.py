"""Informational task links: "relates to", "duplicates" and "cloned from".

Unlike dependencies these never gate the workflow, never take part in deadlock detection and never change project health -
they only record how tasks relate so people can navigate between them. They may join tasks of different projects (a task
is often cloned into another project); editing one needs write access to both projects.

A request names the relationship as seen from the task in the URL (``duplicated_by`` means "the other task duplicates this
one"); the database stores one canonical direction, so reading it back from either task gives the matching wording.
"""

from __future__ import annotations

from ..domain import LINK_INVERSE, LINK_TYPES, STORED_LINK_TYPES
from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker
from . import activity, projects, taskrefs

_ORDER = {t: i for i, t in enumerate(LINK_TYPES)}


def _entry(link_type, row):
    return {
        "type": link_type,
        "task": {"key": "%s-%d" % (row["project_key"], row["number"]), "title": row["title"], "status": row["status"]},
    }


def list_for(conn, task_id):
    """Links of a task, each worded from this task's point of view."""
    out = []
    sql = (
        "SELECT l.type, o.number, o.title, o.status, p.key AS project_key FROM task_links l "
        "JOIN tasks o ON o.id = l.%s JOIN projects p ON p.id = o.project_id WHERE l.%s = ?"
    )
    for r in conn.execute(sql % ("target_id", "source_id"), (task_id,)):
        out.append(_entry(r["type"], r))
    for r in conn.execute(sql % ("source_id", "target_id"), (task_id,)):
        out.append(_entry(LINK_INVERSE[r["type"]], r))
    out.sort(key=lambda e: (_ORDER[e["type"]], e["task"]["key"]))
    return out


def _between(conn, a_id, b_id):
    return conn.execute(
        "SELECT * FROM task_links WHERE (source_id = ?1 AND target_id = ?2) OR (source_id = ?2 AND target_id = ?1)",
        (a_id, b_id),
    ).fetchone()


def _require_write_in(ctx, *rows):
    seen = set()
    for row in rows:
        if row["project_id"] not in seen:
            seen.add(row["project_id"])
            projects.require_write(ctx, projects.get_row(ctx.conn, row["project_key"]))


def add_link(ctx, key, data):
    """Body: ``{"type": "relates_to" | "duplicates" | "duplicated_by" | "cloned_from" | "cloned_to", "task": "WEB-7"}``."""
    conn = ctx.conn
    c = Checker(data, {"type", "task"})
    c.choice("type", LINK_TYPES, required=True)
    c.raw("task", required=True)
    v = c.result()
    row = taskrefs.get_row(conn, key)
    other = taskrefs.lookup(conn, v["task"], "task")
    if other["id"] == row["id"]:
        raise ValidationError({"task": "a task cannot be linked to itself"})
    _require_write_in(ctx, row, other)
    existing = _between(conn, row["id"], other["id"])
    if existing:
        raise Conflict(
            "%s and %s are already linked. Remove that link first to change it." % (taskrefs.key_of(row), taskrefs.key_of(other)),
            code="link_exists",
        )
    stored = v["type"] if v["type"] in STORED_LINK_TYPES else LINK_INVERSE[v["type"]]
    source, target = (row, other) if v["type"] == stored else (other, row)
    conn.execute(
        "INSERT INTO task_links (source_id, target_id, type, created_by, created_at) VALUES (?,?,?,?,?)",
        (source["id"], target["id"], stored, ctx.actor_id, ctx.now_iso),
    )
    activity.log(
        ctx, "task.link_added", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
        details={"type": v["type"], "other": taskrefs.key_of(other)},
    )  # fmt: skip


def remove_link(ctx, key, other_key):
    conn = ctx.conn
    row = taskrefs.get_row(conn, key)
    other = taskrefs.get_row(conn, other_key)
    _require_write_in(ctx, row, other)
    existing = _between(conn, row["id"], other["id"])
    if existing is None:
        raise NotFound(
            "%s and %s are not linked" % (taskrefs.key_of(row), taskrefs.key_of(other)), code="link_not_found"
        )
    seen_as = existing["type"] if existing["source_id"] == row["id"] else LINK_INVERSE[existing["type"]]
    conn.execute("DELETE FROM task_links WHERE source_id = ? AND target_id = ?", (existing["source_id"], existing["target_id"]))
    activity.log(
        ctx, "task.link_removed", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
        details={"type": seen_as, "other": taskrefs.key_of(other)},
    )  # fmt: skip
