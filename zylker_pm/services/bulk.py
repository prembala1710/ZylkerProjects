"""Bulk actions: apply one set of changes to many tasks in a single request.

The changes go through exactly the same code as editing one task (``tasks.update_task``), so every rule - the workflow,
blockers, open subtasks, project membership, archived projects, per-field validation - applies to each task, and each one
gets its normal history entry.

By default the request is *best effort*: tasks that cannot take the change are reported with the reason and the rest are
saved (each task is its own savepoint, so a refused task changes nothing). Tasks that fail only because of ordering - closing
a parent while its subtasks are in the same selection, say - are retried after the others, so the order of the selection
does not matter. With ``"atomic": true`` any failure undoes the whole request.

One summary message goes to Cliq instead of one per task, so selecting 50 tasks does not flood a channel.
"""

from __future__ import annotations

from ..domain import BULK_MAX_TASKS
from ..errors import ApiError, Conflict, ValidationError
from ..util import Checker, normalize_labels, parse_task_key
from . import tasks, watchers

# Fields a bulk request may change. Titles, descriptions, parents and so on are deliberately one-task-at-a-time.
CHANGE_FIELDS = ("status", "priority", "assignee", "milestone", "due_date", "labels", "add_labels", "remove_labels")


def _validate(data):
    c = Checker(data, {"tasks", "changes", "atomic"})
    c.raw("tasks", required=True)
    c.raw("changes", required=True)
    c.boolean("atomic")
    v = c.result()

    keys, errors = [], {}
    raw_keys = v["tasks"]
    if not isinstance(raw_keys, list) or not raw_keys:
        errors["tasks"] = "must be a non-empty list of task keys"
    elif len(raw_keys) > BULK_MAX_TASKS:
        errors["tasks"] = "at most %d tasks per request" % BULK_MAX_TASKS
    else:
        for index, raw in enumerate(raw_keys):
            try:
                project_key, number = parse_task_key(raw if isinstance(raw, str) else "")
            except ApiError:
                errors["tasks"] = "item %d is not a task key such as WEB-12" % (index + 1)
                break
            key = "%s-%d" % (project_key, number)
            if key not in keys:
                keys.append(key)

    changes = v["changes"]
    prepared = {}
    if not isinstance(changes, dict) or not changes:
        errors["changes"] = "must be an object with at least one field to change"
    else:
        unknown = [name for name in changes if name not in CHANGE_FIELDS]
        if unknown:
            errors["changes"] = "cannot change %s in bulk; allowed: %s" % (", ".join(sorted(unknown)), ", ".join(CHANGE_FIELDS))
        elif "labels" in changes and ("add_labels" in changes or "remove_labels" in changes):
            errors["changes"] = "use either 'labels' (replace) or 'add_labels' / 'remove_labels', not both"
        else:
            try:
                # Shape checks for the plain fields come from the single-task validator.
                prepared.update(tasks._validate({k: changes[k] for k in changes if k in tasks.TASK_FIELDS}, partial=True))
            except ValidationError as exc:
                errors.update({"changes." + k: msg for k, msg in exc.details.items()})
            for name in ("add_labels", "remove_labels"):
                if name in changes:
                    try:
                        prepared[name] = normalize_labels(changes[name], name)
                    except ValidationError as exc:
                        errors.update({"changes." + k: msg for k, msg in exc.details.items()})
    if errors:
        raise ValidationError(errors)
    return keys, prepared, v.get("atomic", False)


def _label_names(conn, task_key):
    row = conn.execute(
        "SELECT group_concat(l.label, ',') FROM task_labels l JOIN tasks t ON t.id = l.task_id "
        "JOIN projects p ON p.id = t.project_id WHERE p.key = ? AND t.number = ?",
        parse_task_key(task_key),
    ).fetchone()
    return [x for x in (row[0] or "").split(",") if x]


def _describe(prepared):
    parts = []
    for name in CHANGE_FIELDS:
        if name not in prepared:
            continue
        value = prepared[name]
        if name in ("add_labels", "remove_labels"):
            parts.append("%s label %s" % ("added" if name == "add_labels" else "removed", ", ".join(value)))
        elif name == "labels":
            parts.append("labels \u2192 %s" % (", ".join(value) or "none"))
        elif name == "milestone":
            parts.append("milestone %s" % ("cleared" if value is None else "changed"))
        else:
            parts.append("%s \u2192 %s" % (name.replace("_", " "), "none" if value is None else str(value).replace("_", " ")))
    return "; ".join(parts)


def _apply_one(ctx, key, prepared):
    """Update one task inside a savepoint. Returns ``(outcome, error)``; a failure leaves no trace in the database or in Cliq."""
    conn = ctx.conn
    marker = len(ctx.events)
    last_activity = conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
    conn.execute("SAVEPOINT bulk_item")
    try:
        data = {k: v for k, v in prepared.items() if k not in ("add_labels", "remove_labels")}
        if "add_labels" in prepared or "remove_labels" in prepared:
            current = _label_names(conn, key)
            data["labels"] = [x for x in current + prepared.get("add_labels", []) if x not in prepared.get("remove_labels", [])]
            data["labels"] = list(dict.fromkeys(data["labels"]))
        tasks.update_task(ctx, key, data)
    except ApiError as exc:
        conn.execute("ROLLBACK TO bulk_item")
        conn.execute("RELEASE bulk_item")
        del ctx.events[marker:]
        return None, exc.to_dict()["error"]
    conn.execute("RELEASE bulk_item")
    del ctx.events[marker:]  # replaced by one summary message
    changed = conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0] != last_activity
    return ("updated" if changed else "unchanged"), None


def bulk_update(ctx, data):
    keys, prepared, atomic = _validate(data)
    outcomes, errors = {}, {}
    pending = list(keys)
    while pending:
        failed, progressed = [], False
        for key in pending:
            outcome, detail = _apply_one(ctx, key, prepared)
            if outcome is None:
                failed.append(key)
                errors[key] = detail
            else:
                progressed = True
                outcomes[key] = outcome
                errors.pop(key, None)
        if not progressed:
            break
        pending = failed  # tasks refused only because of the order they were processed in get another go
    failed = [k for k in keys if k not in outcomes]

    if failed and atomic:
        raise Conflict(
            "Nothing was changed because %d of %d task(s) could not be updated" % (len(failed), len(keys)),
            code="bulk_failed",
            details={"failed": [{"key": k, "error": errors[k]} for k in failed]},
        )

    updated = [k for k in keys if k in outcomes and outcomes[k] == "updated"]
    if updated:
        names = []
        for k in updated:
            for name in watchers.names_to_notify(ctx, _task_id(ctx, k)):
                if name not in names:
                    names.append(name)
        ctx.emit("bulk_update", keys=updated, actor=ctx.actor_name, summary=_describe(prepared), watchers=names)

    results = []
    for k in keys:
        if k in outcomes:
            results.append({"key": k, "result": outcomes[k]})
        else:
            results.append({"key": k, "result": "failed", "error": errors[k]})
    return {
        "requested": len(keys),
        "updated": len(updated),
        "unchanged": sum(1 for k in keys if k in outcomes and outcomes[k] == "unchanged"),
        "failed": len(failed),
        "atomic": atomic,
        "results": results,
    }


def _task_id(ctx, key):
    project_key, number = parse_task_key(key)
    return ctx.conn.execute(
        "SELECT t.id FROM tasks t JOIN projects p ON p.id = t.project_id WHERE p.key = ? AND t.number = ?", (project_key, number)
    ).fetchone()[0]
