"""Domain constants: statuses, priorities, roles and the task workflow."""

from __future__ import annotations

ROLES = ("viewer", "member", "manager", "admin")
ROLE_RANK = {role: rank for rank, role in enumerate(ROLES)}

PROJECT_STATUSES = ("planning", "active", "on_hold", "completed", "archived")

TASK_TYPES = ("task", "bug", "story", "epic")
PRIORITIES = ("low", "medium", "high", "critical")

STATUSES = ("todo", "in_progress", "in_review", "blocked", "done", "cancelled")
OPEN_STATUSES = ("todo", "in_progress", "in_review", "blocked")
CLOSED_STATUSES = ("done", "cancelled")

# Allowed status moves. Anything not listed is rejected with 409.
TRANSITIONS = {
    "todo": ("in_progress", "blocked", "done", "cancelled"),
    "in_progress": ("todo", "in_review", "blocked", "done", "cancelled"),
    "in_review": ("in_progress", "blocked", "done", "cancelled"),
    "blocked": ("todo", "in_progress", "cancelled"),
    "done": ("todo", "in_progress"),  # reopen
    "cancelled": ("todo",),  # reopen
}

# Moving a task into one of these needs every blocking task to be closed first.
BLOCKER_GATED_STATUSES = ("in_progress", "in_review", "done")

MAX_TASK_DEPTH = 5  # epic -> story -> task -> subtask -> subtask

HEALTH_LEVELS = ("on_track", "at_risk", "off_track", "complete", "not_started", "on_hold")


def sql_in(values):
    """Render a trusted constant tuple as an SQL IN list."""
    return "(" + ",".join("'" + v + "'" for v in values) + ")"


OPEN_SQL = sql_in(OPEN_STATUSES)
CLOSED_SQL = sql_in(CLOSED_STATUSES)

PRIORITY_RANK_SQL = (
    "CASE t.priority WHEN 'critical' THEN 3 WHEN 'high' THEN 2 "
    "WHEN 'medium' THEN 1 ELSE 0 END"
)
STATUS_RANK_SQL = (
    "CASE t.status WHEN 'todo' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'in_review' THEN 2 "
    "WHEN 'blocked' THEN 3 WHEN 'done' THEN 4 ELSE 5 END"
)
