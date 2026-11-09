"""Checklists: lightweight to-do items inside a task.

An item is just text with a tick box. It has no assignee, status, dates or key of its own and takes no part in workflow
rules - ticking every item does not close the task and unticked items do not stop it closing (use subtasks when work needs
an owner, a due date or a gate). Items keep an order that can be changed; at most 50 per task.
"""

from __future__ import annotations

from ..domain import MAX_CHECKLIST_ITEMS, MAX_CHECKLIST_TEXT
from ..errors import Conflict, NotFound
from ..util import Checker
from . import activity, projects, taskrefs, users

SELECT = (
    "SELECT i.*, u.username AS done_username, u.display_name AS done_name, t.number, p.key AS project_key "
    "FROM task_checklist_items i JOIN tasks t ON t.id = i.task_id JOIN projects p ON p.id = t.project_id "
    "LEFT JOIN users u ON u.id = i.done_by "
)


def to_dict(row):
    return {
        "id": row["id"],
        "task": "%s-%d" % (row["project_key"], row["number"]),
        "position": row["position"],
        "text": row["text"],
        "done": bool(row["done"]),
        "done_by": users.ref(row["done_by"], row["done_username"], row["done_name"]),
        "done_at": row["done_at"],
        "created_at": row["created_at"],
    }


def list_for(conn, task_id):
    return [to_dict(r) for r in conn.execute(SELECT + "WHERE i.task_id = ? ORDER BY i.position, i.id", (task_id,))]


def list_items(ctx, key):
    return list_for(ctx.conn, taskrefs.get_row(ctx.conn, key)["id"])


def _item_row(conn, item_id):
    row = conn.execute(SELECT + "WHERE i.id = ?", (item_id,)).fetchone()
    if row is None:
        raise NotFound("Checklist item not found", code="checklist_item_not_found")
    return row


def _renumber(conn, task_id, ordered_ids):
    for position, item_id in enumerate(ordered_ids, start=1):
        conn.execute("UPDATE task_checklist_items SET position = ? WHERE id = ? AND position <> ?", (position, item_id, position))


def _ids(conn, task_id):
    return [r[0] for r in conn.execute("SELECT id FROM task_checklist_items WHERE task_id = ? ORDER BY position, id", (task_id,))]


def add_item(ctx, key, data):
    """Body: ``{"text": "...", "done"?: bool, "position"?: int}``. Without a position the item goes to the end."""
    conn = ctx.conn
    row = taskrefs.get_row(conn, key)
    projects.require_write(ctx, projects.get_row(conn, row["project_key"]))
    c = Checker(data, {"text", "done", "position"})
    c.text("text", required=True, min_len=1, max_len=MAX_CHECKLIST_TEXT)
    c.boolean("done")
    c.integer("position", minimum=1, maximum=MAX_CHECKLIST_ITEMS)
    v = c.result()
    ids = _ids(conn, row["id"])
    if len(ids) >= MAX_CHECKLIST_ITEMS:
        raise Conflict("A task can have at most %d checklist items" % MAX_CHECKLIST_ITEMS, code="checklist_full")
    done = v.get("done", False)
    cur = conn.execute(
        "INSERT INTO task_checklist_items (task_id, position, text, done, done_by, done_at, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (row["id"], len(ids) + 1, v["text"], int(done), ctx.actor_id if done else None, ctx.now_iso if done else None,
         ctx.actor_id, ctx.now_iso, ctx.now_iso),
    )  # fmt: skip
    wanted = min(v.get("position", len(ids) + 1), len(ids) + 1)
    if wanted <= len(ids):
        ids.insert(wanted - 1, cur.lastrowid)
        _renumber(conn, row["id"], ids)
    activity.log(
        ctx, "checklist.added", project_id=row["project_id"], task_id=row["id"], task_key=taskrefs.key_of(row),
        details={"item_id": cur.lastrowid, "text": v["text"], "done": done},
    )  # fmt: skip
    return to_dict(_item_row(conn, cur.lastrowid))


def update_item(ctx, item_id, data):
    """Body: any of ``text``, ``done`` and ``position`` (1 = top). Ticking records who did it and when."""
    conn = ctx.conn
    item = _item_row(conn, item_id)
    task = taskrefs.get_row(conn, "%s-%d" % (item["project_key"], item["number"]))
    projects.require_write(ctx, projects.get_row(conn, item["project_key"]))
    c = Checker(data, {"text", "done", "position"}, partial=True)
    c.text("text", min_len=1, max_len=MAX_CHECKLIST_TEXT)
    c.boolean("done")
    c.integer("position", minimum=1, maximum=MAX_CHECKLIST_ITEMS)
    v = c.result()
    key = taskrefs.key_of(task)

    def log(action, **details):
        activity.log(
            ctx, action, project_id=task["project_id"], task_id=task["id"], task_key=key,
            details={"item_id": item["id"], "text": item["text"], **details},
        )  # fmt: skip

    if "text" in v and v["text"] != item["text"]:
        conn.execute("UPDATE task_checklist_items SET text = ?, updated_at = ? WHERE id = ?", (v["text"], ctx.now_iso, item["id"]))
        log("checklist.edited", new_text=v["text"])
    if "done" in v and v["done"] != bool(item["done"]):
        if v["done"]:
            conn.execute(
                "UPDATE task_checklist_items SET done = 1, done_by = ?, done_at = ?, updated_at = ? WHERE id = ?",
                (ctx.actor_id, ctx.now_iso, ctx.now_iso, item["id"]),
            )
        else:
            conn.execute(
                "UPDATE task_checklist_items SET done = 0, done_by = NULL, done_at = NULL, updated_at = ? WHERE id = ?",
                (ctx.now_iso, item["id"]),
            )
        log("checklist.checked" if v["done"] else "checklist.unchecked")
    if "position" in v:
        current = _ids(conn, task["id"])
        ids = [i for i in current if i != item["id"]]
        ids.insert(min(v["position"], len(ids) + 1) - 1, item["id"])
        if ids != current:
            _renumber(conn, task["id"], ids)
            log("checklist.moved", position=ids.index(item["id"]) + 1)
    return to_dict(_item_row(conn, item_id))


def delete_item(ctx, item_id):
    conn = ctx.conn
    item = _item_row(conn, item_id)
    task = taskrefs.get_row(conn, "%s-%d" % (item["project_key"], item["number"]))
    projects.require_write(ctx, projects.get_row(conn, item["project_key"]))
    conn.execute("DELETE FROM task_checklist_items WHERE id = ?", (item["id"],))
    _renumber(conn, task["id"], _ids(conn, task["id"]))
    activity.log(
        ctx, "checklist.removed", project_id=task["project_id"], task_id=task["id"], task_key=taskrefs.key_of(task),
        details={"item_id": item["id"], "text": item["text"]},
    )  # fmt: skip
