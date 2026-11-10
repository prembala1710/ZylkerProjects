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

# Dependency types. "blocker" is the task that comes first, "blocked" the one that has to wait for it.
#   finish_to_start   blocked cannot start (or finish) until the blocker is finished   - the original and default rule
#   start_to_start    blocked cannot start (or finish) until the blocker has started
#   finish_to_finish  blocked cannot finish until the blocker is finished (it may start earlier)
DEPENDENCY_TYPES = ("finish_to_start", "start_to_start", "finish_to_finish")
DEFAULT_DEPENDENCY_TYPE = "finish_to_start"
MAX_LAG_DAYS = 365  # positive = lag (wait this many days), negative = lead (may overlap by this many days)
# "Not started yet": a blocker in one of these statuses has not been worked on. (A task flagged blocked may have been
# started before, but the system cannot tell, so it counts as not started.) Closed blockers never hold anyone up.
NOT_STARTED_STATUSES = ("todo", "blocked")

# Informational links. They never gate the workflow, never take part in deadlock detection and never affect health.
# The API takes the relationship as seen from the task in the URL; the database stores one canonical direction.
LINK_TYPES = ("relates_to", "duplicates", "duplicated_by", "cloned_from", "cloned_to")
STORED_LINK_TYPES = ("relates_to", "duplicates", "cloned_from")
LINK_INVERSE = {
    "relates_to": "relates_to",
    "duplicates": "duplicated_by",
    "duplicated_by": "duplicates",
    "cloned_from": "cloned_to",
    "cloned_to": "cloned_from",
}

MAX_CHECKLIST_ITEMS = 50
MAX_CHECKLIST_TEXT = 200
MAX_WATCHERS = 50
BULK_MAX_TASKS = 100

HEALTH_LEVELS = ("on_track", "at_risk", "off_track", "complete", "not_started", "on_hold")


def sql_in(values):
    """Render a trusted constant tuple as an SQL IN list."""
    return "(" + ",".join("'" + v + "'" for v in values) + ")"


OPEN_SQL = sql_in(OPEN_STATUSES)
CLOSED_SQL = sql_in(CLOSED_STATUSES)

# True when a dependency ``d`` still holds the blocked task back, given its blocker ``b`` (both are SQL aliases):
# the blocker is unfinished (finish-to-start, finish-to-finish) or has not started (start-to-start).
UNMET_DEPENDENCY_SQL = (
    "((d.type IN ('finish_to_start','finish_to_finish') AND b.status IN %s) "
    "OR (d.type = 'start_to_start' AND b.status IN %s))" % (OPEN_SQL, sql_in(NOT_STARTED_STATUSES))
)

PRIORITY_RANK_SQL = (
    "CASE t.priority WHEN 'critical' THEN 3 WHEN 'high' THEN 2 "
    "WHEN 'medium' THEN 1 ELSE 0 END"
)
STATUS_RANK_SQL = (
    "CASE t.status WHEN 'todo' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'in_review' THEN 2 "
    "WHEN 'blocked' THEN 3 WHEN 'done' THEN 4 ELSE 5 END"
)
