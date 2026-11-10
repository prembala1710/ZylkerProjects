"""Microsoft Project interchange: export a project as MSPDI XML, import one back.

MSPDI ("Microsoft Project Data Interchange") is the XML that Microsoft Project writes with *File > Save As > XML*
and that other planning tools (MPXJ, Primavera, GanttProject, ...) read and write. Binary ``.mpp`` files are not
supported: save them as XML from Project first.

What is carried over, and what is not, is written down in the README ("Microsoft Project import and export").
Short version: titles, notes, the outline (parents), priority, estimates, dates, completion, people, dependencies
(with lag) and milestones. Types, labels, comments, time entries and attachments are not part of the format.

The importer never trusts the file: it is parsed with expat directly (no DOCTYPE, no entities, bounded size,
nesting and element counts), only the few elements it needs are kept, and every value is validated by the same
rules as the API before it is stored.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from xml.parsers import expat

from ..domain import MAX_LAG_DAYS, MAX_TASK_DEPTH, OPEN_STATUSES
from ..errors import ApiError, PayloadTooLarge, ValidationError
from . import activity, graph, projects, tasks, users

NS = "http://schemas.microsoft.com/project"
SAVE_VERSION = 14  # the format Project 2010 writes; every later version reads it
MAX_TASKS = 5000
MAX_UID = 2**31 - 1  # Project's task ids are 32-bit
MAX_RAW_TASKS = 2 * MAX_TASKS  # including blank rows and the project summary row
MAX_ITEMS = 50_000  # resources, assignments and dependency links
MAX_TEXT = 100_000  # the longest text kept for any single element
MAX_DEPTH = 100  # element nesting
MAX_LOGGED = 200  # warnings / errors listed one by one; the rest are only counted
MAX_MILESTONE_LINKS = 10_000  # links made by looking through milestones

MINUTES_PER_DAY = 480
PRIORITY_VALUE = {"low": 250, "medium": 500, "high": 750, "critical": 950}
PERCENT_COMPLETE = {"todo": 0, "blocked": 0, "in_progress": 50, "in_review": 80, "done": 100, "cancelled": 0}
DEPENDENCY_TO_MSP = {"finish_to_start": 1, "start_to_start": 3, "finish_to_finish": 0}
DEPENDENCY_FROM_MSP = {0: "finish_to_finish", 1: "finish_to_start", 3: "start_to_start"}  # 2 = start-to-finish: no equivalent
ELAPSED_LAG_FORMATS = {4, 6, 8, 10, 12}  # "elapsed" minutes / hours / days / weeks / months: clock time, not working time
PERCENT_LAG_FORMATS = {19, 20}
START_NO_EARLIER_THAN, FINISH_NO_EARLIER_THAN = 4, 6
AUTHOR = "Zylker Projects"  # written by the export; an import uses it to tell our own dates from Project's computed ones


# ----------------------------------------------------------------------------------------------- shared helpers


def project_guid(key):
    """The id an export carries in ``<Project><UID>``. The same project key always gives the same value, which is how
    an import recognises a file that came from this project (task UID *n* is then task ``KEY-n``)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "urn:zylker-projects:project:" + key.upper()))


def format_duration(hours):
    """MSPDI durations are ``PT<hours>H<minutes>M<seconds>S`` (the hours do not roll over into days)."""
    seconds = round(float(hours) * 3600)
    hours_, rest = divmod(seconds, 3600)
    return "PT%dH%dM%dS" % (hours_, *divmod(rest, 60))


_DURATION = re.compile(r"^\s*P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?\s*$")


def parse_duration_hours(text):
    """Hours in an ISO 8601 duration such as ``PT8H0M0S`` (a day, ``D``, counts as 24 hours); None when it is not one."""
    m = _DURATION.match(text or "")
    if not m or not any(m.groups()):
        return None
    days, hours, minutes, seconds = m.groups()
    return int(days or 0) * 24 + int(hours or 0) + int(minutes or 0) / 60 + float(seconds or 0) / 3600


# ------------------------------------------------------------------------------------------------- working time
# One calendar is used for every date the export computes: Monday to Friday, 08:00-12:00 and 13:00-17:00.


def _next_workday(day):
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _prev_workday(day):
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _add_workdays(day, count):
    """``day`` (a working day) moved by ``count`` working days, forwards or backwards."""
    step = 1 if count >= 0 else -1
    count = abs(count)
    day += timedelta(weeks=step * (count // 5))
    for _ in range(count % 5):
        day += timedelta(days=step)
        while day.weekday() >= 5:
            day += timedelta(days=step)
    return day


def _workdays_between(first, last):
    """Working days in ``[first, last)``."""
    if last <= first:
        return 0
    weeks, rest = divmod((last - first).days, 7)
    return weeks * 5 + sum(1 for i in range(rest) if (first + timedelta(days=i)).weekday() < 5)


def _worked_before(moment):
    """Working minutes of the day that have passed at ``moment``."""
    m = moment.hour * 60 + moment.minute
    if m <= 480:
        return 0
    if m <= 720:
        return m - 480
    if m <= 780:
        return 240
    return min(m - 540, MINUTES_PER_DAY)


def _work_minutes(start, finish):
    if finish <= start:
        return 0
    return _workdays_between(start.date(), finish.date()) * MINUTES_PER_DAY + _worked_before(finish) - _worked_before(start)


def _finish_after(first_day, minutes):
    """When ``minutes`` of work that begins at 08:00 on ``first_day`` are done."""
    if minutes <= 0:
        return datetime.combine(first_day, time(8, 0))
    days, rest = divmod(minutes - 1, MINUTES_PER_DAY)
    rest += 1  # 1..480 minutes into the last day
    day = _add_workdays(first_day, days)
    return datetime.combine(day, time(8, 0)) + timedelta(minutes=rest if rest <= 240 else rest + 60)  # +60: lunch


# ------------------------------------------------------------------------------------------------------ export

_INVALID_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _clean(text):
    """Characters XML 1.0 cannot carry at all (most control characters) are dropped."""
    return _INVALID_XML_CHARS.sub("", text)


def _add(parent, tag, value=None):
    element = ET.SubElement(parent, tag)
    if value is not None:
        element.text = _clean(value) if isinstance(value, str) else str(value)
    return element


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _flag(value):
    return "1" if value else "0"


class _Node:
    """One task of the file: a task of the project, or a milestone."""

    __slots__ = ("row", "uid", "children", "milestone", "outline", "wbs", "start", "finish", "minutes", "hours", "percent", "predecessors")

    def __init__(self, row, uid, milestone=False):
        self.row, self.uid, self.milestone = row, uid, milestone
        self.children, self.outline, self.wbs = [], 1, ""
        self.start = self.finish = None
        self.minutes = self.hours = self.percent = 0
        self.predecessors = []  # (uid, MSP link type, lag in days)

    @property
    def summary(self):
        return bool(self.children)


def _load(conn, project):
    rows = conn.execute(
        "SELECT id, number, title, description, status, priority, assignee_id, parent_id, milestone_id, "
        "start_date, due_date, estimate_hours, created_at FROM tasks WHERE project_id = ? ORDER BY number",
        (project["id"],),
    ).fetchall()
    links = conn.execute(
        "SELECT d.blocker_id, d.blocked_id, d.type, d.lag_days FROM task_dependencies d "
        "JOIN tasks t ON t.id = d.blocker_id WHERE t.project_id = ?",
        (project["id"],),
    ).fetchall()
    milestones = conn.execute(
        "SELECT id, name, description, due_date FROM milestones WHERE project_id = ? ORDER BY due_date, id", (project["id"],)
    ).fetchall()
    people = conn.execute(
        "SELECT id, username, display_name, email, weekly_capacity_hours FROM users "
        "WHERE id IN (SELECT user_id FROM project_members WHERE project_id = ?) "
        "OR id IN (SELECT assignee_id FROM tasks WHERE project_id = ? AND assignee_id IS NOT NULL) "
        "ORDER BY LOWER(display_name), username",
        (project["id"], project["id"]),
    ).fetchall()
    return rows, links, milestones, people


def _outline(rows):
    """Task nodes by id, and in outline order (a parent, then its subtasks, each level by task number)."""
    nodes = {r["id"]: _Node(r, r["number"]) for r in rows}
    roots = []
    for r in rows:  # ordered by number, so every list below is too
        parent = nodes.get(r["parent_id"])
        (parent.children if parent else roots).append(nodes[r["id"]])
    ordered = []
    stack = [(node, 1, str(i)) for i, node in reversed(list(enumerate(roots, 1)))]
    while stack:
        node, level, wbs = stack.pop()
        node.outline, node.wbs = level, wbs
        ordered.append(node)
        stack.extend((child, level + 1, "%s.%d" % (wbs, i)) for i, child in reversed(list(enumerate(node.children, 1))))
    return nodes, ordered


def _plan(ordered, project, today):
    """Dates and durations for the file. Leaves start on their start date (or end on their due date), work for their
    estimate on the standard calendar and ignore capacity; summaries span their subtasks. All of it is a suggestion
    that Project may reschedule - the real due dates travel separately, as deadlines."""
    fallback = _next_workday(date.fromisoformat(project["start_date"]) if project["start_date"] else today)
    for node in reversed(ordered):  # subtasks before their parents
        row = node.row
        if node.summary:
            node.start = min(k.start for k in node.children)
            node.finish = max(k.finish for k in node.children)
            node.minutes = _work_minutes(node.start, node.finish)
            node.hours = node.minutes / 60
            total = sum(k.minutes for k in node.children)
            node.percent = round(sum(k.percent * k.minutes for k in node.children) / total) if total else PERCENT_COMPLETE[row["status"]]
            continue
        node.hours = 8.0 if row["estimate_hours"] is None else row["estimate_hours"]
        node.minutes = round(node.hours * 60)
        needed = max(1, -(-node.minutes // MINUTES_PER_DAY))
        if row["start_date"]:
            first = _next_workday(date.fromisoformat(row["start_date"]))
        elif row["due_date"]:
            first = _add_workdays(_prev_workday(date.fromisoformat(row["due_date"])), -(needed - 1))
        else:
            first = fallback
        node.start = datetime.combine(first, time(8, 0))
        node.finish = _finish_after(first, node.minutes)
        node.percent = PERCENT_COMPLETE[row["status"]]


def export_xml(ctx, project_key):
    """The whole project as an MSPDI document (UTF-8 bytes)."""
    conn = ctx.conn
    project = projects.get_row(conn, project_key)
    rows, links, milestone_rows, people = _load(conn, project)
    nodes, ordered = _outline(rows)
    _plan(ordered, project, ctx.today)

    gates = []  # milestones become zero-length tasks at the end of the outline
    top_level = sum(1 for n in ordered if n.outline == 1)
    for i, m in enumerate(milestone_rows):
        gate = _Node(m, max((r["number"] for r in rows), default=0) + 1 + i, milestone=True)
        gate.wbs = str(top_level + i + 1)
        gate.start = gate.finish = datetime.combine(date.fromisoformat(m["due_date"]), time(17, 0))
        gates.append(gate)
    by_milestone = {g.row["id"]: g for g in gates}
    for node in ordered:  # a task that belongs to a milestone has to finish before the milestone
        gate = by_milestone.get(node.row["milestone_id"])
        if gate:
            gate.predecessors.append((node.uid, 1, 0))
    for blocker, blocked, kind, lag in links:
        if blocker in nodes and blocked in nodes:
            nodes[blocked].predecessors.append((nodes[blocker].uid, DEPENDENCY_TO_MSP[kind], lag))

    everything = ordered + gates
    first_moment = min((n.start for n in everything), default=datetime.combine(_next_workday(ctx.today), time(8, 0)))
    last_moment = max((n.finish for n in everything), default=first_moment)
    owner = conn.execute("SELECT display_name FROM users WHERE id = ?", (project["owner_id"],)).fetchone() if project["owner_id"] else None
    resource_uid = {p["id"]: i for i, p in enumerate(people, 1)}
    saved = _stamp(ctx.now.astimezone(ctx.tz).replace(tzinfo=None))
    name = project["name"][:255]

    root = ET.Element("Project", {"xmlns": NS})
    _add(root, "SaveVersion", SAVE_VERSION)
    _add(root, "UID", project_guid(project["key"]))
    _add(root, "Name", name)
    _add(root, "Title", name)
    if owner:
        _add(root, "Manager", owner["display_name"])
    _add(root, "Author", AUTHOR)
    _add(root, "CreationDate", saved)
    _add(root, "LastSaved", saved)
    _add(root, "ScheduleFromStart", "1")
    _add(root, "StartDate", _stamp(first_moment))
    _add(root, "FinishDate", _stamp(last_moment))
    _add(root, "CalendarUID", "1")
    _add(root, "DefaultStartTime", "08:00:00")
    _add(root, "DefaultFinishTime", "17:00:00")
    _add(root, "MinutesPerDay", MINUTES_PER_DAY)
    _add(root, "MinutesPerWeek", MINUTES_PER_DAY * 5)
    _add(root, "DaysPerMonth", "20")
    _add(root, "DurationFormat", "7")
    _add(root, "WorkFormat", "2")
    _add(root, "NewTasksEstimated", "1")
    _add(root, "WeekStartDay", "1")

    calendar = _add(_add(root, "Calendars"), "Calendar")
    _add(calendar, "UID", "1")
    _add(calendar, "Name", "Standard")
    _add(calendar, "IsBaseCalendar", "1")
    week = _add(calendar, "WeekDays")
    for day_type in range(1, 8):  # 1 = Sunday ... 7 = Saturday
        day = _add(week, "WeekDay")
        _add(day, "DayType", day_type)
        _add(day, "DayWorking", _flag(2 <= day_type <= 6))
        if 2 <= day_type <= 6:
            times = _add(day, "WorkingTimes")
            for start, end in (("08:00:00", "12:00:00"), ("13:00:00", "17:00:00")):
                slot = _add(times, "WorkingTime")
                _add(slot, "FromTime", start)
                _add(slot, "ToTime", end)

    out = _add(root, "Tasks")
    summary = _add(out, "Task")  # row 0 is the project itself, as Project writes it
    for tag, value in (("UID", 0), ("ID", 0), ("Name", name), ("Active", "1"), ("Manual", "0"), ("Type", "0"), ("IsNull", "0"),
                       ("WBS", "0"), ("OutlineNumber", "0"), ("OutlineLevel", 0), ("Priority", 500), ("Start", _stamp(first_moment)),
                       ("Finish", _stamp(last_moment)), ("Duration", format_duration(_work_minutes(first_moment, last_moment) / 60)),
                       ("DurationFormat", "7"), ("Summary", "1"), ("PercentComplete", 0)):  # fmt: skip
        _add(summary, tag, value)

    for index, node in enumerate(everything, 1):
        row = node.row
        task = _add(out, "Task")
        _add(task, "UID", node.uid)
        _add(task, "ID", index)
        _add(task, "Name", (row["name"] if node.milestone else row["title"])[:255])
        _add(task, "Active", _flag(node.milestone or row["status"] != "cancelled"))
        _add(task, "Manual", "0")
        _add(task, "Type", "0")
        _add(task, "IsNull", "0")
        if not node.milestone:
            _add(task, "CreateDate", row["created_at"][:19])
        _add(task, "WBS", node.wbs)
        _add(task, "OutlineNumber", node.wbs)
        _add(task, "OutlineLevel", node.outline)
        _add(task, "Priority", 500 if node.milestone else PRIORITY_VALUE[row["priority"]])
        _add(task, "Start", _stamp(node.start))
        _add(task, "Finish", _stamp(node.finish))
        _add(task, "Duration", format_duration(node.hours))
        _add(task, "DurationFormat", "7" if node.milestone or node.minutes >= MINUTES_PER_DAY else "5")
        if not node.milestone and not node.summary:
            _add(task, "Work", format_duration(node.hours if row["estimate_hours"] is not None else 0))
            _add(task, "Estimated", _flag(row["estimate_hours"] is None))
        _add(task, "Milestone", _flag(node.milestone))
        _add(task, "Summary", _flag(node.summary))
        _add(task, "PercentComplete", node.percent)
        _add(task, "PercentWorkComplete", node.percent)
        if node.milestone:  # finish no earlier than: it moves later when its tasks slip, never earlier
            _add(task, "ConstraintType", FINISH_NO_EARLIER_THAN)
            _add(task, "ConstraintDate", _stamp(node.finish))
        elif row["start_date"]:
            _add(task, "ConstraintType", START_NO_EARLIER_THAN)
            _add(task, "ConstraintDate", _stamp(datetime.combine(date.fromisoformat(row["start_date"]), time(8, 0))))
        if row["due_date"]:
            _add(task, "Deadline", _stamp(datetime.combine(date.fromisoformat(row["due_date"]), time(17, 0))))
        if row["description"]:
            _add(task, "Notes", row["description"])
        for uid, kind, lag in node.predecessors:
            link = _add(task, "PredecessorLink")
            _add(link, "PredecessorUID", uid)
            _add(link, "Type", kind)
            _add(link, "CrossProject", "0")
            _add(link, "LinkLag", lag * MINUTES_PER_DAY * 10)  # tenths of a minute
            _add(link, "LagFormat", "7")

    resources = _add(root, "Resources")
    for person in people:
        res = _add(resources, "Resource")
        _add(res, "UID", resource_uid[person["id"]])
        _add(res, "ID", resource_uid[person["id"]])
        _add(res, "Name", person["display_name"])
        _add(res, "Type", "1")
        _add(res, "IsNull", "0")
        if person["email"]:
            _add(res, "EmailAddress", person["email"])
        _add(res, "MaxUnits", "%.2f" % (person["weekly_capacity_hours"] / 40.0))

    assignments = _add(root, "Assignments")
    for n, node in enumerate((n for n in ordered if n.row["assignee_id"] is not None), 1):
        a = _add(assignments, "Assignment")
        _add(a, "UID", n)
        _add(a, "TaskUID", node.uid)
        _add(a, "ResourceUID", resource_uid[node.row["assignee_id"]])
        _add(a, "Units", "1")
        if not node.summary:
            _add(a, "Work", format_duration(node.hours if node.row["estimate_hours"] is not None else 0))

    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


# ------------------------------------------------------------------------------------------------------ reading


class _Refused(Exception):
    """The document is not acceptable (the message is shown to the user)."""


class _Reader:
    """Collects what the importer needs while expat parses; nothing else is kept, so memory stays small however big
    or odd the file is. Element names are matched without their namespace."""

    PROJECT = {"UID", "Name", "Title", "Author"}
    TASK = {"UID", "Name", "IsNull", "Active", "OutlineLevel", "Priority", "Start", "Finish", "Duration", "Work", "Estimated",
            "Milestone", "Summary", "PercentComplete", "ConstraintType", "ConstraintDate", "Deadline", "Notes"}  # fmt: skip
    LINK = {"PredecessorUID", "Type", "CrossProject", "LinkLag", "LagFormat"}
    RESOURCE = {"UID", "Name", "Type", "IsNull", "EmailAddress"}
    ASSIGNMENT = {"TaskUID", "ResourceUID", "Work"}
    COLLECTIONS = {"Tasks": ("Task", TASK), "Resources": ("Resource", RESOURCE), "Assignments": ("Assignment", ASSIGNMENT)}

    def __init__(self):
        self.path = []
        self.project, self.tasks, self.resources, self.assignments = {}, [], [], []
        self.lists = {"Tasks": self.tasks, "Resources": self.resources, "Assignments": self.assignments}
        self.task_rows = self.other_rows = 0
        self.has_tasks = False
        self._item = None  # the Task / Resource / Assignment being read
        self._link = None
        self._field = None  # (name, target dict, depth of its element)
        self._text, self._size = [], 0

    def _begin(self, name, target):
        self._field, self._text, self._size = (name, target, len(self.path)), [], 0

    def _counted(self, collection):
        if collection == "Tasks":
            self.task_rows += 1
            if self.task_rows > MAX_RAW_TASKS:
                raise _Refused("the document has too many tasks (more than %d rows)" % MAX_RAW_TASKS)
        else:
            self.other_rows += 1
            if self.other_rows > MAX_ITEMS:
                raise _Refused("the document has too many resources, assignments or dependency links (more than %d)" % MAX_ITEMS)

    def start(self, name, attributes):
        local = name.rpartition("}")[2]
        self.path.append(local)
        path, depth = self.path, len(self.path)
        if depth > MAX_DEPTH:
            raise _Refused("the document is nested too deeply")
        if depth == 1:
            if local != "Project":
                raise _Refused("this is not a Microsoft Project file: the top element is <%s>, not <Project>" % local[:40])
            return
        if self._field is not None:
            return
        if depth == 2:
            if local in self.PROJECT:
                self._begin(local, self.project)
            elif local == "Tasks":
                self.has_tasks = True
        elif depth == 3 and path[1] in self.COLLECTIONS and local == self.COLLECTIONS[path[1]][0]:
            self._counted(path[1])
            self._item = {"links": []} if path[1] == "Tasks" else {}
            self.lists[path[1]].append(self._item)
        elif depth == 4 and self._item is not None and path[1] in self.COLLECTIONS and path[2] == self.COLLECTIONS[path[1]][0]:
            if local in self.COLLECTIONS[path[1]][1]:
                self._begin(local, self._item)
            elif path[1] == "Tasks" and local == "PredecessorLink":
                self._counted("Links")
                self._link = {}
                self._item["links"].append(self._link)
        elif depth == 5 and self._link is not None and path[3] == "PredecessorLink" and local in self.LINK:
            self._begin(local, self._link)

    def text(self, data):
        if self._field is not None and self._size < MAX_TEXT:
            self._text.append(data)
            self._size += len(data)

    def end(self, name):
        depth = len(self.path)
        if self._field is not None:
            if depth == self._field[2]:
                field, target, _ = self._field
                target.setdefault(field, "".join(self._text)[:MAX_TEXT])
                self._field = None
        elif depth == 3:
            self._item = None
        elif depth == 4 and self._link is not None:
            self._link = None
        self.path.pop()


def _read(data):
    reader = _Reader()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    parser.StartElementHandler = reader.start
    parser.EndElementHandler = reader.end
    parser.CharacterDataHandler = reader.text

    def refuse(*_):
        raise _Refused("documents with a DOCTYPE or entity declarations are not accepted")

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValidationError({"xml": "not well-formed XML (line %d): %s" % (exc.lineno, expat.ErrorString(exc.code))}) from None
    except _Refused as exc:
        raise ValidationError({"xml": str(exc)}) from None
    except (ValueError, LookupError):  # a text encoding that expat cannot read, however it is declared
        raise ValidationError({"xml": "the file uses a text encoding that is not supported (use UTF-8)"}) from None
    if not reader.has_tasks:
        raise ValidationError({"xml": "this is not a Microsoft Project file: it has no <Tasks> section"})
    return reader


# ----------------------------------------------------------------------------------------------------- parsing

_DAY = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_CONTROL = re.compile("[\x00-\x1f\x7f]+")


def _day(text):
    m = _DAY.match(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _int(text, default=None):
    try:
        return int((text or "").strip())
    except ValueError:
        return default


def _bool(text, default=False):
    value = (text or "").strip().lower()
    if value in ("1", "true"):
        return True
    if value in ("0", "false"):
        return False
    return default


class _Log:
    """Warnings and errors: the first ones are listed, the rest only counted, so a hostile file cannot flood the answer."""

    def __init__(self):
        self.warnings, self.errors = [], []
        self._dropped = {"warnings": 0, "errors": 0}

    def _put(self, kind, uid, message):
        target = getattr(self, kind)
        if len(target) < MAX_LOGGED:
            target.append({"uid": uid, "message": message})
        else:
            self._dropped[kind] += 1

    def warn(self, uid, message):
        self._put("warnings", uid, message)

    def error(self, uid, message):
        self._put("errors", uid, message)

    def finish(self):
        for kind in ("warnings", "errors"):
            if self._dropped[kind]:
                getattr(self, kind).append({"uid": None, "message": "... and %d more %s not listed" % (self._dropped[kind], kind)})


class _Item:
    """One task of the file, on its way to becoming a task (or a milestone) of the project."""

    def __init__(self, raw, uid):
        self.raw, self.uid = raw, uid
        self.level = max(1, _int(raw.get("OutlineLevel"), 1))
        self.name = " ".join(_CONTROL.sub(" ", raw.get("Name") or "").split())
        self.ref = None  # external_ref
        self.parent = None
        self.children = []
        self.app_level = 1
        self.milestone = False
        self.task_id = self.key = None
        self.created = False

    @property
    def summary(self):
        return bool(self.children)


def _items(reader, log):
    found, seen = [], set()
    for raw in reader.tasks:
        uid = _int(raw.get("UID"))
        if uid == 0 or _bool(raw.get("IsNull")):
            continue  # the project summary row and blank rows
        if uid is None or not 0 < uid <= MAX_UID:
            log.error(None, "a task without a usable UID was skipped (a UID is a whole number from 1 to %d)" % MAX_UID)
        elif uid in seen:
            log.error(uid, "the UID is used more than once; the later task was skipped")
        else:
            seen.add(uid)
            found.append(_Item(raw, uid))
    if not found and not log.errors:
        log.warn(None, "the file contains no tasks")
    if len(found) > MAX_TASKS:
        raise ValidationError({"xml": "the file has %d tasks; at most %d can be imported at once - split the project" % (len(found), MAX_TASKS)})
    return found


def _tree(found, log):
    """Parents from the outline levels, never deeper than the app allows (a deeper task is moved up to that level)."""
    stack = []
    for item in found:
        while stack and stack[-1].level >= item.level:
            stack.pop()
        parent = stack[-1] if stack else None
        if parent is not None and parent.app_level >= MAX_TASK_DEPTH:
            log.warn(item.uid, "%s is nested more than %d levels deep and was moved up to level %d" % (item.name or "a task", MAX_TASK_DEPTH, MAX_TASK_DEPTH))
            parent = parent.parent
        item.parent = parent
        item.app_level = parent.app_level + 1 if parent else 1
        if parent is not None:
            parent.children.append(item)
        stack.append(item)
    for item in found:
        item.milestone = _bool(item.raw.get("Milestone")) and not item.summary and not _bool(item.raw.get("Summary"))


def _lag_days(link, uid, log):
    lag = _int(link.get("LinkLag"), 0)
    if not lag:
        return 0
    fmt = _int(link.get("LagFormat"))
    if fmt in PERCENT_LAG_FORMATS:
        log.warn(uid, "a lag given as a percentage was ignored")
        return 0
    days = lag / 10 / (1440 if fmt in ELAPSED_LAG_FORMATS else MINUTES_PER_DAY)
    rounded = int(days + 0.5) if days >= 0 else -int(-days + 0.5)
    if abs(rounded) > MAX_LAG_DAYS:
        log.warn(uid, "a lag of %d days was limited to %d" % (rounded, MAX_LAG_DAYS))
        rounded = MAX_LAG_DAYS if rounded > 0 else -MAX_LAG_DAYS
    return rounded


def _edges(found, log):
    """Dependencies as ``(predecessor, successor, type, lag in days)``; the ones the app cannot hold are reported."""
    by_uid = {i.uid: i for i in found}
    edges = []
    for item in found:
        for link in item.raw["links"]:
            other = _int(link.get("PredecessorUID"))
            if _bool(link.get("CrossProject")):
                log.warn(item.uid, "a dependency on a task in another project file was not imported")
                continue
            kind = DEPENDENCY_FROM_MSP.get(_int(link.get("Type"), 1))
            if kind is None:
                log.warn(item.uid, "a start-to-finish dependency was not imported (the app has no equivalent)")
                continue
            pred = by_uid.get(other)
            if pred is None:
                log.warn(item.uid, "a dependency on task UID %s was not imported: there is no such task in the file" % (other if other is not None else "?"))
            elif pred is item:
                log.warn(item.uid, "a task cannot depend on itself; that dependency was not imported")
            else:
                edges.append((pred, item, kind, _lag_days(link, item.uid, log)))
    return edges


def _through_milestones(edges, log):
    """The dependencies between tasks, looking through milestones (they are not part of the dependency graph here):
    A -> milestone -> B becomes A -> B, with the two lags added and the type of the second link."""
    into = defaultdict(list)
    for edge in edges:
        into[edge[1].uid].append(edge)
    result, seen = [], set()
    for first in edges:
        succ = first[1]
        if succ.milestone:
            continue
        stack, visited = [(first[0], first[3])], set()
        while stack:
            pred, lag = stack.pop()
            if not pred.milestone:
                if (pred.uid, succ.uid) not in seen:
                    seen.add((pred.uid, succ.uid))
                    result.append((pred, succ, first[2], lag))
            elif pred.uid not in visited:
                visited.add(pred.uid)
                stack.extend((p, lag + extra) for p, _, _, extra in into[pred.uid])
        if len(result) > MAX_MILESTONE_LINKS:
            log.warn(None, "dependencies through milestones were limited to %d" % MAX_MILESTONE_LINKS)
            break
    return result


# ---------------------------------------------------------------------------------------------------- importing


def _priority(raw):
    value = _int(raw.get("Priority"), 500)  # Project's names: Lowest 100 ... Low 400, Medium 500, High 600 ... Highest 900
    return "low" if value < 500 else "medium" if value < 600 else "high" if value < 900 else "critical"


def _status(raw):
    if not _bool(raw.get("Active"), True):
        return "cancelled"
    percent = _int(raw.get("PercentComplete"), 0)
    return "done" if percent >= 100 else "todo" if percent <= 0 else "in_progress"


def _rolled_up(states):
    if all(s in ("done", "cancelled") for s in states):
        return "done" if "done" in states else "cancelled"
    return "todo" if all(s == "todo" for s in states) else "in_progress"


def _people(conn, project, reader, found, add_missing_members, log):
    """Task UID -> username, from the assignments: the person with the most work on a task is its assignee."""
    resources = {}
    for raw in reader.resources:
        uid = _int(raw.get("UID"))
        if uid is not None and not _bool(raw.get("IsNull")) and _int(raw.get("Type"), 1) == 1:  # not material or cost resources
            resources[uid] = raw
    options = defaultdict(list)
    for a in reader.assignments:
        task, res = _int(a.get("TaskUID")), resources.get(_int(a.get("ResourceUID")))
        if task is not None and res is not None:
            options[task].append((parse_duration_hours(a.get("Work")) or 0.0, res))
    wanted = {i.uid for i in found}
    resolved, left_out = {}, defaultdict(int)
    for uid, choices in sorted(options.items()):
        if uid not in wanted:
            continue
        choices.sort(key=lambda c: -c[0])  # stable: the first of equals stays first
        res = choices[0][1]
        if len(choices) > 1:
            names = ", ".join(sorted({c[1].get("Name") or "?" for c in choices}))
            log.warn(uid, "assigned to several people (%s); only %s is kept" % (names, res.get("Name") or "?"))
        user = None
        for text in ((res.get("EmailAddress") or "").strip(), (res.get("Name") or "").strip()):
            user = users.find_by_identity(conn, text) if text else None
            if user is not None:
                break
        who = res.get("Name") or res.get("EmailAddress") or "?"
        if user is None or not user["active"]:
            left_out[(who, "there is no active user with that name or e-mail address")] += 1
        elif projects.is_member(conn, project["id"], user["id"]):
            resolved[uid] = user["username"]
        elif add_missing_members:
            conn.execute("INSERT INTO project_members (project_id, user_id) VALUES (?,?)", (project["id"], user["id"]))
            resolved[uid] = user["username"]
        else:
            left_out[(user["username"], "they are not a member of the project (importing with the add-missing-members option adds them)")] += 1
    for (who, why), count in sorted(left_out.items()):
        log.warn(None, "%s: %d task%s left unassigned because %s" % (who, count, "" if count == 1 else "s", why))
    return resolved


def _existing(conn, project, item, own_export, refs):
    """The task this one already is in the project, as ``(id, key)``: UID *n* of our own export is ``KEY-n``;
    otherwise a task that an earlier import of the same file created."""
    if own_export:
        row = conn.execute("SELECT id FROM tasks WHERE project_id = ? AND number = ?", (project["id"], item.uid)).fetchone()
        if row:
            return row["id"], "%s-%d" % (project["key"], item.uid)
    found = refs.get(item.ref)
    return found if found else None


def _task_data(item, people, log, ours):
    raw = item.raw
    if not item.name:
        raise ValidationError({"title": "the task has no name"})
    if len(item.name) > 200:
        log.warn(item.uid, "the name was cut to 200 characters")
    data = {"title": item.name[:200], "priority": _priority(raw), "external_ref": item.ref}
    notes = (raw.get("Notes") or "").replace("\r\n", "\n").strip()
    if notes:
        data["description"] = notes[:20000]
        if len(notes) > 20000:
            log.warn(item.uid, "the notes were cut to 20,000 characters")
    data["type"] = "epic" if item.summary and item.app_level == 1 else "story" if item.summary and item.app_level == 2 else "task"
    data["status"] = "todo" if item.summary else _status(raw)  # a parent's status follows its subtasks (see _roll_up)
    if item.parent is not None:
        data["parent"] = item.parent.key
    # "Start no earlier than" is a date somebody chose; Start and Finish are what Project worked out. Our own export only
    # writes the former and the deadline as real dates (the rest is a suggestion), so for it those are the only dates read.
    constraint = _day(raw.get("ConstraintDate")) if _int(raw.get("ConstraintType")) == START_NO_EARLIER_THAN else None
    start = constraint or (None if ours else _day(raw.get("Start")))
    due = _day(raw.get("Deadline")) or (None if ours else _day(raw.get("Finish")))
    if start and due and due < start:
        log.warn(item.uid, "the finish is before the start; the due date was set to the start")
        due = start
    if start:
        data["start_date"] = start.isoformat()
    if due:
        data["due_date"] = due.isoformat()
    if not item.summary:
        work, duration = parse_duration_hours(raw.get("Work")), parse_duration_hours(raw.get("Duration"))
        hours = work if work else duration
        placeholder = not work and _bool(raw.get("Estimated")) and duration is not None and abs(duration - 8) < 1e-9  # Project's "1 day?"
        if hours and not placeholder:
            if hours > 10000:
                log.warn(item.uid, "the estimate was limited to 10,000 hours")
            data["estimate_hours"] = round(min(hours, 10000.0), 2)
    if item.uid in people:
        data["assignee"] = people[item.uid]
    return data


def _roll_up(ctx, made):
    """A parent's status is not in the file (Project works it out): it follows its subtasks, deepest parents first."""
    conn = ctx.conn
    for item in sorted((i for i in made if i.summary), key=lambda i: -i.app_level):
        states = [conn.execute("SELECT status FROM tasks WHERE id = ?", (k.task_id,)).fetchone()[0] for k in item.children if k.task_id]
        status = _rolled_up(states) if states else "todo"
        if status != "todo":
            closed = status not in OPEN_STATUSES
            conn.execute("UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?", (status, ctx.now_iso if closed else None, item.task_id))


def _milestones(ctx, project, found, edges, log):
    """Milestone tasks become milestones (one that already exists with the same name and date is reused); the tasks that
    lead into one, if they were created now, are attached to it. Returns how many milestones were created."""
    conn = ctx.conn
    created, ids = 0, {}
    for item in found:
        if not item.milestone:
            continue
        raw = item.raw
        due = _day(raw.get("Finish")) or _day(raw.get("Start")) or _day(raw.get("Deadline"))
        if not item.name or due is None:
            log.error(item.uid, "the milestone has no %s" % ("name" if not item.name else "date"))
            continue
        if len(item.name) > 100:
            log.warn(item.uid, "the milestone name was cut to 100 characters")
        name = item.name[:100]
        row = conn.execute("SELECT id FROM milestones WHERE project_id = ? AND name = ? AND due_date = ?", (project["id"], name, due.isoformat())).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO milestones (project_id, name, description, due_date, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (project["id"], name, (raw.get("Notes") or "").strip()[:2000], due.isoformat(), ctx.now_iso, ctx.now_iso),
            )
            created += 1
            ids[item.uid] = (cur.lastrowid, due)
        else:
            ids[item.uid] = (row["id"], due)
    belongs = {}  # task uid -> milestone uid (the earliest one when a task leads into several)
    for pred, succ, kind, _ in edges:
        if succ.uid in ids and pred.created and not pred.milestone and kind in ("finish_to_start", "finish_to_finish"):
            current = belongs.get(pred.uid)
            if current is not None and current != succ.uid:
                log.warn(pred.uid, "the task leads into several milestones; it was attached to the earliest")
            if current is None or ids[succ.uid][1] < ids[current][1]:
                belongs[pred.uid] = succ.uid
    task_of = {i.uid: i.task_id for i in found}
    for uid, gate in belongs.items():
        conn.execute("UPDATE tasks SET milestone_id = ? WHERE id = ? AND milestone_id IS NULL", (ids[gate][0], task_of[uid]))
    return created


def _links(ctx, project, wanted, log):
    conn = ctx.conn
    made = 0
    guard = graph.CycleGuard(conn, project["id"])
    for pred, succ, kind, lag in wanted:
        if pred.task_id is None or succ.task_id is None:
            continue  # one of the two was not imported, which is reported already
        pair = (pred.task_id, succ.task_id)
        if conn.execute("SELECT 1 FROM task_dependencies WHERE blocker_id = ? AND blocked_id = ?", pair).fetchone():
            continue  # there already (a second import of the same file)
        if conn.execute("SELECT 1 FROM task_dependencies WHERE blocker_id = ? AND blocked_id = ?", pair[::-1]).fetchone():
            # the app reads a dependency between two tasks as one thing whichever way it points (changing or removing it
            # takes either), so a second one the other way round would be impossible to tell apart from the first
            log.warn(succ.uid, "the dependency on UID %d was not imported: that task already depends on this one, and two tasks keep a single dependency between them" % pred.uid)
            continue
        cycle = guard.add((pred.task_id, succ.task_id, kind))
        if cycle:
            log.error(succ.uid, "the dependency on UID %d was not imported: %s" % (pred.uid, tasks._cycle_error(ctx, cycle).message))
            continue
        conn.execute(
            "INSERT INTO task_dependencies (blocker_id, blocked_id, created_at, type, lag_days) VALUES (?,?,?,?,?)",
            (pred.task_id, succ.task_id, ctx.now_iso, kind, lag),
        )
        made += 1
    return made


def _dangling_milestones(found, edges, log):
    has_before = {succ.uid for _, succ, _, _ in edges}
    for item in found:
        if item.milestone and item.uid not in has_before and any(pred is item for pred, _, _, _ in edges):
            log.warn(item.uid, "milestone %s has tasks after it but none before it, so those dependencies were not imported" % item.name)


def import_xml(ctx, project_key, data, *, dry_run=False, add_missing_members=False):
    """Create tasks, milestones and dependencies from an MSPDI document.

    Nothing that already exists is changed. Importing the same file again is safe: tasks are recognised by an
    ``external_ref`` of the form ``msp:<file id>:<task UID>``, milestones by name and date, and a file that was
    exported from this very project is matched task by task (UID *n* is ``KEY-n``).
    """
    conn = ctx.conn
    project = projects.get_row(conn, project_key)
    projects.require_write(ctx, project)
    if len(data) > ctx.config.max_import_bytes:
        raise PayloadTooLarge("The file exceeds %d bytes" % ctx.config.max_import_bytes)
    if not data.strip():
        raise ValidationError({"xml": "the file is empty"})

    reader = _read(data)
    log = _Log()
    found = _items(reader, log)
    _tree(found, log)
    edges = _edges(found, log)
    identity = (reader.project.get("UID") or "").strip().lower()
    own_export = identity == project_guid(project["key"])
    ours = (reader.project.get("Author") or "").strip() == AUTHOR
    file_id = hashlib.sha256((identity or (reader.project.get("Name") or "").strip().lower()).encode("utf-8")).hexdigest()[:8]

    if dry_run:
        conn.execute("SAVEPOINT import_dry_run")
    people = _people(conn, project, reader, found, add_missing_members, log)
    refs = {r["external_ref"]: (r["id"], "%s-%d" % (project["key"], r["number"])) for r in conn.execute(
        "SELECT external_ref, id, number FROM tasks WHERE project_id = ? AND external_ref IS NOT NULL", (project["id"],))}  # fmt: skip
    created = skipped = 0
    keys, made = [], []
    for item in found:
        if item.milestone:
            continue
        item.ref = ("msp:%s:%d" % (file_id, item.uid))[:100]
        known = _existing(conn, project, item, own_export, refs)
        if known:
            item.task_id, item.key = known
            skipped += 1
            continue
        if item.parent is not None and item.parent.task_id is None:
            log.error(item.uid, "%s was not imported because its parent (UID %d) was not" % (item.name or "the task", item.parent.uid))
            continue
        conn.execute("SAVEPOINT import_task")
        try:
            item.task_id, item.key = tasks.create_in_project(ctx, project, _task_data(item, people, log, ours), notify=False, log=False)
            conn.execute("RELEASE import_task")
        except ApiError as exc:
            conn.execute("ROLLBACK TO import_task")
            conn.execute("RELEASE import_task")
            detail = "; ".join("%s: %s" % kv for kv in (exc.details or {}).items())
            log.error(item.uid, "%s: %s" % (item.name or "task", detail or exc.message))
            continue
        item.created = True
        refs[item.ref] = (item.task_id, item.key)
        created += 1
        made.append(item)
        if len(keys) < 100:
            keys.append(item.key)

    _roll_up(ctx, made)
    gates = _milestones(ctx, project, found, edges, log)
    linked = _links(ctx, project, _through_milestones(edges, log), log)
    _dangling_milestones(found, edges, log)

    if dry_run:
        conn.execute("ROLLBACK TO import_dry_run")
        conn.execute("RELEASE import_dry_run")
    else:
        activity.log(
            ctx, "import.completed", project_id=project["id"],
            details={"source": "msproject", "created": created, "skipped_duplicates": skipped, "errors": len(log.errors)},
        )  # fmt: skip
    log.finish()
    return {
        "dry_run": dry_run, "tasks_read": len(found), "created": created, "skipped_duplicates": skipped,
        "milestones": gates, "links": linked, "errors": log.errors, "warnings": log.warnings, "keys": keys,
    }  # fmt: skip
