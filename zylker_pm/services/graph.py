"""Dependency + hierarchy graph helpers.

Two kinds of ordering constraint exist inside a project:

* a dependency ``blocker -> blocked`` (finish-to-start), and
* a hierarchy edge ``child -> parent`` (a parent cannot finish before its subtasks).

A *deadlock* is a cycle in the union of both. Checking the union (rather than each
kind separately) catches longer loops such as ``parent -> X -> child -> parent``.
"""

from __future__ import annotations

from collections import deque

from ..domain import OPEN_SQL


def dependency_edges(conn, project_id):
    rows = conn.execute(
        "SELECT d.blocker_id, d.blocked_id FROM task_dependencies d "
        "JOIN tasks t ON t.id = d.blocker_id WHERE t.project_id = ?",
        (project_id,),
    )
    return {(r[0], r[1]) for r in rows}


def hierarchy_edges(conn, project_id):
    rows = conn.execute(
        "SELECT id, parent_id FROM tasks WHERE project_id = ? AND parent_id IS NOT NULL", (project_id,)
    )
    return {(r[0], r[1]) for r in rows}


def _path(adj, start, goal):
    previous = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            out = []
            while node is not None:
                out.append(node)
                node = previous[node]
            return out[::-1]
        for nxt in adj.get(node, ()):
            if nxt not in previous:
                previous[nxt] = node
                queue.append(nxt)
    return None


def find_cycle(conn, project_id, add, remove=()):
    """Return task ids forming a cycle if the ``add`` edges were applied, else None.

    Edges are ``(u, v)`` meaning "u must finish before v". ``remove`` edges are
    treated as already deleted (used when re-parenting a task).
    """
    edges = dependency_edges(conn, project_id) | hierarchy_edges(conn, project_id)
    edges -= set(remove)
    edges |= set(add)
    adj = {}
    for u, v in edges:
        adj.setdefault(u, []).append(v)
    for u, v in add:
        path = _path(adj, v, u)
        if path is not None:
            return [u] + path
    return None


def open_blockers(conn, task_id):
    return conn.execute(
        "SELECT b.id, b.number, b.title, b.status, p.key AS project_key "
        "FROM task_dependencies d JOIN tasks b ON b.id = d.blocker_id "
        "JOIN projects p ON p.id = b.project_id "
        "WHERE d.blocked_id = ? AND b.status IN %s ORDER BY b.number" % OPEN_SQL,
        (task_id,),
    ).fetchall()


def ancestors(conn, task_id):
    """Ids from the direct parent upwards."""
    out, current = [], task_id
    while True:
        row = conn.execute("SELECT parent_id FROM tasks WHERE id = ?", (current,)).fetchone()
        if row is None or row[0] is None or row[0] in out:
            return out
        out.append(row[0])
        current = row[0]


def subtree_height(conn, task_id):
    """Number of levels below a task (0 for a leaf)."""
    height, frontier = 0, [task_id]
    while frontier and height < 50:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE parent_id IN (%s)" % ",".join("?" * len(frontier)), frontier
        ).fetchall()
        frontier = [r[0] for r in rows]
        if frontier:
            height += 1
    return height
