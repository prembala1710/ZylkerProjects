"""Dependency + hierarchy graph helpers.

Ordering constraints inside a project come from two places:

* a dependency ``blocker -> blocked`` of one of three types (finish-to-start, start-to-start, finish-to-finish), and
* a hierarchy edge ``child -> parent`` (a parent cannot finish before its subtasks).

A *deadlock* is a cycle in the combined graph. Because the dependency types talk about different moments of a task,
the graph is built over *events* rather than tasks: every task has a ``start`` event and a ``finish`` event, and

* ``start -> finish`` for the same task (work has to begin before it can end),
* finish-to-start:  ``finish(blocker) -> start(blocked)``,
* start-to-start:   ``start(blocker)  -> start(blocked)``,
* finish-to-finish: ``finish(blocker) -> finish(blocked)``,
* hierarchy:        ``finish(child)   -> finish(parent)``.

A cycle in that graph means something would have to happen before itself. Working with events (instead of treating every
link as "u before v") is what keeps legitimate combinations such as "B starts after A starts" together with "A finishes
after B finishes" from being refused, while still catching loops that run through parent/subtask links.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta

from ..domain import NOT_STARTED_STATUSES, OPEN_STATUSES
from ..util import parse_date

START, FINISH = 0, 1
HIERARCHY = "child_of"  # edge kind for subtask -> parent; the other kinds are the dependency types

_EVENTS = {
    "finish_to_start": (FINISH, START),
    "start_to_start": (START, START),
    "finish_to_finish": (FINISH, FINISH),
    HIERARCHY: (FINISH, FINISH),
}


def dependency_edges(conn, project_id):
    """Dependencies of a project as ``(blocker_id, blocked_id, type)`` triples."""
    rows = conn.execute(
        "SELECT d.blocker_id, d.blocked_id, d.type FROM task_dependencies d "
        "JOIN tasks t ON t.id = d.blocker_id WHERE t.project_id = ?",
        (project_id,),
    )
    return {(r[0], r[1], r[2]) for r in rows}


def hierarchy_edges(conn, project_id):
    """Subtask links as ``(child_id, parent_id, 'child_of')`` triples."""
    rows = conn.execute(
        "SELECT id, parent_id FROM tasks WHERE project_id = ? AND parent_id IS NOT NULL", (project_id,)
    )
    return {(r[0], r[1], HIERARCHY) for r in rows}


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


def _event_edge(edge):
    u, v, kind = edge
    left, right = _EVENTS[kind]
    return (u, left), (v, right)


def find_cycle(conn, project_id, add=(), remove=()):
    """Return task ids forming a cycle if the ``add`` edges were applied, else None.

    Edges are ``(u, v, kind)`` triples where ``kind`` is a dependency type or ``HIERARCHY`` (see the module docstring for what
    each one means). ``remove`` edges are treated as already deleted - used when re-parenting a task or changing a
    dependency's type. The result lists the tasks around the loop, starting and ending with the same task.
    """
    edges = dependency_edges(conn, project_id) | hierarchy_edges(conn, project_id)
    edges -= set(remove)
    edges |= set(add)
    adj, tasks = {}, set()
    for edge in edges:
        a, b = _event_edge(edge)
        adj.setdefault(a, []).append(b)
        tasks.update((edge[0], edge[1]))
    for t in tasks:
        adj.setdefault((t, START), []).append((t, FINISH))
    for neighbours in adj.values():
        neighbours.sort()  # deterministic loops in error messages
    for edge in add:
        a, b = _event_edge(edge)
        path = _path(adj, b, a)
        if path is not None:
            loop = [a] + path
            ids = [event[0] for event in loop]
            return [t for i, t in enumerate(ids) if i == 0 or t != ids[i - 1]]
    return None


class CycleGuard:
    """Cycle checks for many new dependencies at once (an import): the graph is built a single time and grows as edges
    are accepted, where ``find_cycle`` rebuilds it for every edge. Both give the same answers."""

    def __init__(self, conn, project_id):
        self._adj, self._known = {}, set()
        for edge in sorted(dependency_edges(conn, project_id) | hierarchy_edges(conn, project_id)):
            self._insert(edge)

    def _insert(self, edge):
        a, b = _event_edge(edge)
        for task in edge[:2]:
            if task not in self._known:
                self._known.add(task)
                self._adj.setdefault((task, START), []).append((task, FINISH))
        self._adj.setdefault(a, []).append(b)

    def add(self, edge):
        """Accept ``(blocker_id, blocked_id, kind)`` unless it would close a loop; returns the loop (as ``find_cycle``
        does) when it would, and None when the edge was added."""
        for task in edge[:2]:  # a task that has no edge yet still has to be able to lead from its start to its finish
            if task not in self._known:
                self._known.add(task)
                self._adj.setdefault((task, START), []).append((task, FINISH))
        a, b = _event_edge(edge)
        path = _path(self._adj, b, a)
        if path is not None:
            ids = [event[0] for event in [a] + path]
            return [t for i, t in enumerate(ids) if i == 0 or t != ids[i - 1]]
        self._insert(edge)
        return None


def blocking_links(conn, task_id, new_status):
    """Dependencies that stop ``task_id`` from moving into ``new_status`` right now.

    * To start (in progress / in review) or finish (done): finish-to-start needs the blocker closed, start-to-start needs
      it started.
    * To finish (done) only: finish-to-finish needs the blocker closed.

    Closed blockers (done or cancelled) never hold anybody up.
    """
    if new_status not in ("in_progress", "in_review", "done"):
        return []
    rows = conn.execute(
        "SELECT b.id, b.number, b.title, b.status, p.key AS project_key, d.type "
        "FROM task_dependencies d JOIN tasks b ON b.id = d.blocker_id "
        "JOIN projects p ON p.id = b.project_id WHERE d.blocked_id = ? ORDER BY b.number",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        if r["type"] == "finish_to_start":
            hold = r["status"] in OPEN_STATUSES
        elif r["type"] == "start_to_start":
            hold = r["status"] in NOT_STARTED_STATUSES
        else:  # finish_to_finish
            hold = new_status == "done" and r["status"] in OPEN_STATUSES
        if hold:
            out.append(r)
    return out


def schedule_hint(dep_type, lag_days, blocker_start, blocker_due, blocked_start, blocked_due):
    """When the dependency says the blocked task may start / finish, and whether its planned dates already break that.

    Calendar days, not working days; the same day is allowed (a task due on the 10th can hand over on the 10th).
    ``None`` when the anchoring date on the blocker is not set.
    """
    if dep_type == "start_to_start":
        anchor, applies = blocker_start, "start"
    elif dep_type == "finish_to_finish":
        anchor, applies = blocker_due, "finish"
    else:
        anchor, applies = blocker_due, "start"
    anchor_date = parse_date(anchor)
    if anchor_date is None:
        return None
    not_before = (anchor_date + timedelta(days=lag_days or 0)).isoformat()
    planned = blocked_start if applies == "start" else blocked_due
    return {"applies_to": applies, "not_before": not_before, "conflict": bool(planned) and planned < not_before}


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
