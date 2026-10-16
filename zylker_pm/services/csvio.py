"""CSV export and import of tasks (the way to bring existing projects in)."""

from __future__ import annotations

import csv
import io
import re
from datetime import date

from ..errors import ApiError, ValidationError
from ..util import LABEL_RE, parse_date
from . import activity, projects, tasks, users

MAX_IMPORT_ROWS = 5000

EXPORT_COLUMNS = [
    "key", "title", "type", "status", "priority", "assignee", "reporter", "parent", "milestone",
    "start_date", "due_date", "estimate_hours", "logged_hours", "labels", "external_ref",
    "created_at", "completed_at", "description",
]  # fmt: skip

# Normalised header (lower-case, '_' and runs of spaces collapsed) -> canonical field.
HEADER_ALIASES = {
    "title": "title", "summary": "title", "name": "title", "task": "title", "task name": "title",
    "description": "description",
    "type": "type", "issue type": "type", "work type": "type", "work item type": "type",
    "status": "status",
    "priority": "priority",
    "assignee": "assignee", "assigned to": "assignee", "owner": "assignee",
    "reporter": "reporter", "creator": "reporter", "created by": "reporter",
    "start date": "start_date", "start": "start_date",
    "due date": "due_date", "due": "due_date", "duedate": "due_date", "target date": "due_date",
    "estimate hours": "estimate_hours", "estimate (hours)": "estimate_hours", "estimate": "estimate_hours",
    "labels": "labels", "label": "labels", "tags": "labels",
    "external ref": "external_ref", "issue key": "external_ref", "key": "external_ref", "ref": "external_ref",
}  # fmt: skip

TYPE_MAP = {
    "task": "task", "sub-task": "task", "subtask": "task", "technical task": "task", "improvement": "task",
    "bug": "bug", "defect": "bug", "story": "story", "user story": "story", "new feature": "story",
    "feature": "story", "epic": "epic",
}  # fmt: skip
STATUS_MAP = {
    "to do": "todo", "todo": "todo", "open": "todo", "backlog": "todo", "new": "todo", "selected for development": "todo",
    "in progress": "in_progress", "doing": "in_progress", "started": "in_progress", "in development": "in_progress",
    "in review": "in_review", "review": "in_review", "code review": "in_review",
    "blocked": "blocked", "on hold": "blocked", "impediment": "blocked",
    "done": "done", "closed": "done", "resolved": "done", "complete": "done", "completed": "done",
    "cancelled": "cancelled", "canceled": "cancelled", "won't do": "cancelled", "wontfix": "cancelled",
    "rejected": "cancelled",
}  # fmt: skip
PRIORITY_MAP = {
    "highest": "critical", "blocker": "critical", "critical": "critical", "urgent": "critical",
    "high": "high", "major": "high",
    "medium": "medium", "normal": "medium",
    "low": "low", "lowest": "low", "minor": "low", "trivial": "low",
}  # fmt: skip

MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
LOOSE_DATE = re.compile(
    r"^(\d{1,2})[-/ ]([A-Za-z]{3,9})[-/ ,]*(\d{2}|\d{4})(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?)?$"
)
ISO_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ]|$)")
FORMULA_ESCAPED = re.compile(r"^'+[=+\-@\t\r]")  # a cell we escaped: apostrophe(s) then a formula trigger
FORMULA_RISK = re.compile(r"^'*[=+\-@\t\r]")


# ------------------------------------------------------------------------------ export


def _safe(value):
    """Neutralise spreadsheet formula injection by prefixing risky cells with an apostrophe."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if FORMULA_RISK.match(text) else text


def export_csv(ctx, project_key):
    project = projects.get_row(ctx.conn, project_key)
    ids = [r[0] for r in ctx.conn.execute("SELECT id FROM tasks WHERE project_id = ? ORDER BY number", (project["id"],))]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for t in tasks._fetch(ctx, ids):
        writer.writerow(
            [
                _safe(t["key"]), _safe(t["title"]), t["type"], t["status"], t["priority"],
                _safe(t["assignee"]["username"] if t["assignee"] else None),
                _safe(t["reporter"]["username"] if t["reporter"] else None),
                _safe(t["parent"]), _safe(t["milestone"]["name"] if t["milestone"] else None),
                t["start_date"] or "", t["due_date"] or "",
                "" if t["estimate_hours"] is None else t["estimate_hours"], t["logged_hours"],
                _safe(";".join(t["labels"])), _safe(t["external_ref"]),
                t["created_at"], t["completed_at"] or "", _safe(t["description"]),
            ]
        )  # fmt: skip
    return out.getvalue()


# ------------------------------------------------------------------------------ import


def parse_loose_date(text):
    """ISO dates plus day-month-year with a month *name* (e.g. 30-Jul-26, 30/Jul/2026 12:00 AM).

    Purely numeric dd/mm vs mm/dd formats are deliberately not accepted: they are ambiguous.
    """
    text = (text or "").strip()
    if not text:
        return None
    iso = ISO_PREFIX.match(text)
    if iso:
        return parse_date(iso.group(1))
    m = LOOSE_DATE.match(text)
    if not m:
        return None
    day, month_name, year = m.groups()
    month = MONTHS.get(month_name[:3].lower())
    if month is None:
        return None
    year = int(year)
    year += 2000 if year < 100 else 0
    try:
        return date(year, month, int(day))
    except ValueError:
        return None


def _map_columns(header):
    columns, seen = [], {}
    for index, raw in enumerate(header):
        norm = re.sub(r"[\s_]+", " ", raw.strip().lower().lstrip("\ufeff"))
        field = HEADER_ALIASES.get(norm)
        columns.append(field)
        if field:
            seen.setdefault(field, []).append(index)
    return columns, seen


def _cell(row, indexes, join=False):
    values = []
    for i in indexes:
        if i < len(row):
            value = row[i].strip()
            value = value[1:] if FORMULA_ESCAPED.match(value) else value
            if value:
                values.append(value)
    if not values:
        return ""
    return " ".join(values) if join else values[0]


def _mapped(mapping, raw, what, line, warnings, default):
    key = raw.strip().lower()
    if not key:
        return default
    if key in mapping:
        return mapping[key]
    warnings.append({"row": line, "message": "unknown %s %r, using %r" % (what, raw, default)})
    return default


def _row_to_task(ctx, project, row, fields, line, warnings, add_missing_members):
    conn = ctx.conn
    title = _cell(row, fields["title"])
    if not title:
        raise ValidationError({"title": "is empty"})
    data = {"title": title[:200]}
    if len(title) > 200:
        warnings.append({"row": line, "message": "title truncated to 200 characters"})
    if "description" in fields:
        data["description"] = _cell(row, fields["description"])[:20000]
    data["type"] = _mapped(TYPE_MAP, _cell(row, fields.get("type", [])), "type", line, warnings, "task")
    data["status"] = _mapped(STATUS_MAP, _cell(row, fields.get("status", [])), "status", line, warnings, "todo")
    data["priority"] = _mapped(PRIORITY_MAP, _cell(row, fields.get("priority", [])), "priority", line, warnings, "medium")

    reporter_id = None
    name = _cell(row, fields.get("reporter", []))
    if name:
        reporter = users.find_by_identity(conn, name)
        if reporter is None:
            warnings.append({"row": line, "message": "reporter %r not found; using the importing user" % name})
        else:
            reporter_id = reporter["id"]
    name = _cell(row, fields.get("assignee", []))
    if name:
        user = users.find_by_identity(conn, name)
        if user is None or not user["active"]:
            warnings.append({"row": line, "message": "assignee %r not found; left unassigned" % name})
        elif projects.is_member(conn, project["id"], user["id"]):
            data["assignee"] = user["username"]
        elif add_missing_members:
            conn.execute("INSERT INTO project_members (project_id, user_id) VALUES (?,?)", (project["id"], user["id"]))
            data["assignee"] = user["username"]
        else:
            warnings.append({"row": line, "message": "%s is not a project member; left unassigned" % user["username"]})

    for field in ("start_date", "due_date"):
        text = _cell(row, fields.get(field, []))
        if text:
            parsed = parse_loose_date(text)
            if parsed is None:
                warnings.append({"row": line, "message": "unrecognised %s %r ignored" % (field, text)})
            else:
                data[field] = parsed.isoformat()
    text = _cell(row, fields.get("estimate_hours", []))
    if text:
        try:
            hours = float(text.replace(",", "."))
            if not 0 <= hours <= 10000:
                raise ValueError
            data["estimate_hours"] = hours
        except ValueError:
            warnings.append({"row": line, "message": "invalid estimate %r ignored" % text})
    labels = []
    for chunk in re.split(r"[,;\s]+", _cell(row, fields.get("labels", []), join=True)):
        label = chunk.strip().lower()
        if not label:
            continue
        if LABEL_RE.match(label):
            if label not in labels:
                labels.append(label)
        else:
            warnings.append({"row": line, "message": "invalid label %r dropped" % chunk})
    if labels:
        data["labels"] = labels[:20]
    ref = _cell(row, fields.get("external_ref", []))
    if ref:
        data["external_ref"] = ref[:100]
    return data, reporter_id


def import_csv(ctx, project_key, text, *, dry_run=False, add_missing_members=False):
    """Create tasks from CSV text. Valid rows are imported; bad rows are reported, never fatal.

    Re-running the same file is safe: rows whose ``external_ref`` (e.g. a Jira issue key)
    already exists in the project are skipped.
    """
    conn = ctx.conn
    project = projects.get_row(conn, project_key)
    projects.require_write(ctx, project)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError({"csv": "the file is empty"}) from None
    _, fields = _map_columns(header)
    if "title" not in fields:
        raise ValidationError({"csv": "no title column found; expected one of: title, summary, name, task"})

    existing = {r[0] for r in conn.execute("SELECT external_ref FROM tasks WHERE project_id = ? AND external_ref IS NOT NULL", (project["id"],))}
    created, skipped, errors, warnings, keys = 0, 0, [], [], []
    rows_read = 0
    if dry_run:
        conn.execute("SAVEPOINT import_dry_run")
    for line, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        rows_read += 1
        if rows_read > MAX_IMPORT_ROWS:
            errors.append({"row": line, "message": "row limit of %d reached; split the file" % MAX_IMPORT_ROWS})
            break
        conn.execute("SAVEPOINT import_row")
        try:
            data, reporter_id = _row_to_task(ctx, project, row, fields, line, warnings, add_missing_members)
            ref = data.get("external_ref")
            if ref and ref in existing:
                skipped += 1
                conn.execute("RELEASE import_row")
                continue
            _, key = tasks.create_in_project(ctx, project, data, notify=False, log=False, reporter_id=reporter_id)
            if ref:
                existing.add(ref)
            created += 1
            if len(keys) < 100:
                keys.append(key)
            conn.execute("RELEASE import_row")
        except ApiError as exc:
            conn.execute("ROLLBACK TO import_row")
            conn.execute("RELEASE import_row")
            detail = "; ".join("%s: %s" % kv for kv in (exc.details or {}).items())
            errors.append({"row": line, "message": detail or exc.message})
    if dry_run:
        conn.execute("ROLLBACK TO import_dry_run")
        conn.execute("RELEASE import_dry_run")
    else:
        activity.log(
            ctx, "import.completed", project_id=project["id"],
            details={"created": created, "skipped_duplicates": skipped, "errors": len(errors)},
        )  # fmt: skip
    return {
        "dry_run": dry_run, "rows_read": rows_read, "created": created, "skipped_duplicates": skipped,
        "errors": errors, "warnings": warnings, "keys": keys,
    }  # fmt: skip
