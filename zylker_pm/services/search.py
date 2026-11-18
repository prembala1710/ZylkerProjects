"""Full-text search across tasks, comments and projects, with a small query language.

Query language (everything is optional and combinable)::

    login crash              every word must match (stemmed: "crashes" finds "crash")
    "login page"             an exact phrase
    log*                     a prefix
    login OR signin          either word
    login -safari            exclude a word or phrase
    project:WEB status:open  filters (see FILTERS), several values with commas: status:todo,blocked
    status:open              filters alone list matching tasks, most recently changed first
    WEB-12                   a task key goes straight to that task

Two engines answer the same questions. With SQLite's FTS5 (the normal case) results are ranked by BM25 with the title
weighted above labels above text, and highlighted snippets come from the index. Without FTS5 the "like" engine does
case-insensitive substring matching straight on the tables - slower on big databases and cruder at ranking, but the same
filters, the same result shape.

User input never reaches the database as search syntax: every word becomes a quoted FTS5 string, so ``AND``, ``NEAR``,
quotes and parentheses typed by a user are just text. Highlights are returned as segments (``{"text", "match"}``), never
as markup, so the browser can show them with ``textContent``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import db
from ..domain import CLOSED_STATUSES, OPEN_STATUSES, PRIORITIES, STATUSES, TASK_TYPES
from ..errors import Conflict, ValidationError
from ..util import escape_like, truncate
from . import activity

KINDS = ("task", "comment", "project")
FILTERS = ("project", "in", "type", "status", "priority", "assignee", "reporter", "label", "is")

MAX_QUERY = 200
MAX_TERMS = 16
DEFAULT_LIMIT, MAX_LIMIT, MAX_OFFSET = 20, 50, 1000
COUNT_CAP = 1000
SNIPPET_WORDS = 16
SNIPPET_CHARS = 140

ON, OFF = "\x02", "\x03"  # highlight markers; the index strips them from indexed text
_TOKEN = re.compile(r'(-?)"([^"]*)"|(\S+)')
_WORD = re.compile(r"\w", re.UNICODE)
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,9}-[0-9]+$")
_PROJECT = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,9}$")
_LABEL = re.compile(r"^[a-z0-9_./-]{1,30}$")
_USER = re.compile(r"^[A-Za-z0-9_.@-]{1,60}$")


# ------------------------------------------------------------------------------ parsing


@dataclass
class Term:
    text: str
    prefix: bool = False

    def shown(self):
        return self.text + ("*" if self.prefix else "")


@dataclass
class Query:
    raw: str
    groups: list = field(default_factory=list)  # AND of groups; a group is an OR of terms
    excluded: list = field(default_factory=list)
    filters: dict = field(default_factory=dict)

    @property
    def terms(self):
        return [t for g in self.groups for t in g]


def _term(text, prefix=False):
    text = text.replace('"', " ").strip()
    return Term(text, prefix) if _WORD.search(text) else None


def parse(raw: str) -> Query:
    if len(raw) > MAX_QUERY:
        raise ValidationError({"q": "must be at most %d characters" % MAX_QUERY})
    items, filters = [], {}
    for m in _TOKEN.finditer(raw):
        negated, phrase, word = m.group(1), m.group(2), m.group(3)
        if word is None:
            term = _term(phrase)
            if term:
                items.append(("not" if negated else "term", term))
            continue
        if word == "OR":
            items.append(("or", None))
            continue
        minus = word.startswith("-") and len(word) > 1
        body = word[1:] if minus else word
        name, colon, value = body.partition(":")
        if colon and value and name.lower() in FILTERS:
            if minus:
                raise ValidationError({"q": "the filter %s: cannot be negated" % name.lower()})
            filters.setdefault(name.lower(), []).extend(v for v in value.split(",") if v)
            continue
        star = body.endswith("*")
        term = _term(body.rstrip("*"), star)
        if term:
            items.append(("not" if minus else "term", term))
    query = Query(raw, filters=filters)
    join = False
    for kind, term in items:
        if kind == "or":
            join = bool(query.groups)
        elif kind == "not":
            query.excluded.append(term)
            join = False
        else:
            if join:
                query.groups[-1].append(term)
            else:
                query.groups.append([term])
            join = False
    if sum(len(g) for g in query.groups) + len(query.excluded) > MAX_TERMS:
        raise ValidationError({"q": "at most %d words and phrases" % MAX_TERMS})
    return query


def _normalise_filters(ctx, filters):
    """Validate and canonicalise the filter values. Every problem is reported at once."""
    out, errors = {}, []

    def pick(name, allowed, lower=True):
        values = []
        for v in filters.get(name, []):
            v = v.lower() if lower else v
            if v not in allowed:
                errors.append("%s:%s is not valid (use %s)" % (name, v, ", ".join(allowed)))
            elif v not in values:
                values.append(v)
        return values

    if "in" in filters:
        kinds = []
        for v in filters["in"]:
            v = v.lower().rstrip("s") if v.lower() in ("tasks", "comments", "projects") else v.lower()
            if v not in KINDS:
                errors.append("in:%s is not valid (use %s)" % (v, ", ".join(KINDS)))
            elif v not in kinds:
                kinds.append(v)
        out["in"] = kinds
    if "type" in filters:
        out["type"] = pick("type", TASK_TYPES)
    if "priority" in filters:
        out["priority"] = pick("priority", PRIORITIES)
    statuses = []
    for v in filters.get("status", []):  # "open" and "closed" are shortcuts for the whole group, like is:open / is:closed
        low = v.lower()
        group = OPEN_STATUSES if low == "open" else CLOSED_STATUSES if low == "closed" else (low,)
        if group == (low,) and low not in STATUSES:
            errors.append("status:%s is not valid (use %s, open or closed)" % (low, ", ".join(STATUSES)))
        else:
            statuses += [x for x in group if x not in statuses]
    if "is" in filters:
        for v in pick("is", ("open", "closed")):
            statuses += [s for s in (OPEN_STATUSES if v == "open" else CLOSED_STATUSES) if s not in statuses]
    if statuses:
        out["status"] = statuses
    if "project" in filters:
        keys = []
        for v in filters["project"]:
            if not _PROJECT.match(v):
                errors.append("project:%s is not a project key" % truncate(v, 20))
            elif v.upper() not in keys:
                keys.append(v.upper())
        out["project"] = keys
    if "label" in filters:
        labels = []
        for v in filters["label"]:
            if not _LABEL.match(v.lower()):
                errors.append("label:%s is not a valid label" % truncate(v, 20))
            elif v.lower() not in labels:
                labels.append(v.lower())
        out["label"] = labels
    for name, allow_none in (("assignee", True), ("reporter", False)):
        if name in filters:
            names = []
            for v in filters[name]:
                low = v.lower()
                if low == "none" and not allow_none:
                    errors.append("%s:none is not valid" % name)
                elif low == "me" and ctx.user is None:
                    errors.append("%s:me needs a signed-in user" % name)
                elif low not in ("me", "none") and not _USER.match(v):
                    errors.append("%s:%s is not a user name" % (name, truncate(v, 20)))
                elif low not in names:
                    names.append(low)
            out[name] = names
    if errors:
        raise ValidationError({"q": "; ".join(errors)})
    return out


# ------------------------------------------------------------------------------ SQL pieces

_JOINS = (
    " LEFT JOIN tasks t ON t.id = {T}.task_id"
    " LEFT JOIN projects p ON p.id = {T}.project_id"
    " LEFT JOIN users a ON a.id = t.assignee_id"
    " LEFT JOIN users r ON r.id = t.reporter_id"
    " LEFT JOIN comments c ON c.id = {T}.ref AND {T}.kind = 'comment'"
    " LEFT JOIN users cu ON cu.id = c.author_id"
)
_COMMON = (
    "t.number AS number, t.title AS task_title, t.status AS status, t.priority AS priority, a.username AS assignee, "
    "p.key AS project_key, p.name AS project_name, c.id AS comment_id, cu.username AS author, c.created_at AS commented_at"
)
_SRC = """WITH src(kind, ref, task_id, project_id, title, labels, body) AS (
    SELECT 'task', t.id, t.id, t.project_id, t.title,
           COALESCE((SELECT group_concat(label, ' ') FROM task_labels WHERE task_id = t.id), ''), t.description FROM tasks t
    UNION ALL SELECT 'comment', c.id, c.task_id, t.project_id, '', '', c.body FROM comments c JOIN tasks t ON t.id = c.task_id
    UNION ALL SELECT 'project', p.id, NULL, p.id, p.key || ' ' || p.name, '', p.description FROM projects p
)"""


def _filter_sql(ctx, f, table):
    """WHERE fragments for the filters. Anything about a task implies the hit has a task (so projects drop out)."""
    conds, args, about_tasks = [], [], False

    def any_of(column, values):
        conds.append("%s IN (%s)" % (column, ",".join("?" * len(values))))
        args.extend(values)

    if f.get("in"):
        any_of(table + ".kind", f["in"])
    if f.get("project"):
        any_of("p.key", f["project"])
    for name, column in (("type", "t.type"), ("status", "t.status"), ("priority", "t.priority")):
        if f.get(name):
            about_tasks = True
            any_of(column, f[name])
    for name, column, alias in (("assignee", "t.assignee_id", "a"), ("reporter", "t.reporter_id", "r")):
        if f.get(name):
            about_tasks = True
            parts = []
            for v in f[name]:
                if v == "none":
                    parts.append("%s IS NULL" % column)
                elif v == "me":
                    parts.append("%s = ?" % column)
                    args.append(ctx.user["id"])
                else:
                    parts.append("%s.username = ?" % alias)
                    args.append(v)
            conds.append("(" + " OR ".join(parts) + ")")
    if f.get("label"):
        about_tasks = True
        conds.append("EXISTS (SELECT 1 FROM task_labels l WHERE l.task_id = t.id AND l.label IN (%s))" % ",".join("?" * len(f["label"])))
        args.extend(f["label"])
    if about_tasks:
        conds.append("t.id IS NOT NULL")
    return conds, args, about_tasks


def _fts_term(term):
    return '"%s"%s' % (term.text.replace('"', '""'), "*" if term.prefix else "")


def fts_expression(q: Query) -> str:
    """The FTS5 query for a parsed query. Each word is a quoted string, so user text cannot be search syntax."""
    parts = []
    for group in q.groups:
        terms = [_fts_term(t) for t in group]
        parts.append(terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")")
    expr = " AND ".join(parts)
    if q.excluded:
        expr = "(%s) NOT (%s)" % (expr, " OR ".join(_fts_term(t) for t in q.excluded))
    return expr


# ------------------------------------------------------------------------------ highlighting


def segments(marked: str):
    """Split text carrying highlight markers into ``[{"text", "match"}]`` - plain data, never markup."""
    out, pos = [], 0
    for m in re.finditer(ON + "(.*?)" + OFF, marked, re.S):
        if m.start() > pos:
            out.append({"text": marked[pos : m.start()], "match": False})
        out.append({"text": m.group(1), "match": True})
        pos = m.end()
    if pos < len(marked):
        out.append({"text": marked[pos:], "match": False})
    return out or [{"text": "", "match": False}]


def _mark(text, terms, window=0):
    """Highlight ``terms`` in ``text`` (the like engine's counterpart of FTS5's highlight() / snippet())."""
    text = (text or "").replace(ON, "").replace(OFF, "")
    pattern = re.compile("|".join(re.escape(t.text) for t in sorted(terms, key=lambda t: -len(t.text)))) if terms else None
    pattern = re.compile(pattern.pattern, re.I) if pattern else None
    if window and len(text) > window:
        first = pattern.search(text) if pattern else None
        start = max(0, first.start() - window // 4) if first else 0
        chunk = text[start : start + window]
        text = ("\u2026" if start else "") + chunk + ("\u2026" if start + window < len(text) else "")
    return pattern.sub(lambda m: ON + m.group(0) + OFF, text) if pattern else text


# ------------------------------------------------------------------------------ running a search


def _int(params, name, default, low, high):
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValidationError({name: "must be an integer"}) from None
    if not low <= value <= high:
        raise ValidationError({name: "must be between %d and %d" % (low, high)})
    return value


def _like_terms(q, args):
    """(score SQL, where SQL) for the like engine; appends their arguments in the order they appear."""
    score_parts, where_parts = [], []
    for group in q.groups:
        for t in group:
            pat = "%" + escape_like(t.text) + "%"
            score_parts.append("((src.title LIKE ? ESCAPE '\\') * 10 + (src.labels LIKE ? ESCAPE '\\') * 4 + (src.body LIKE ? ESCAPE '\\'))")
            args["score"] += [pat] * 3
    for group in q.groups:
        ors = []
        for t in group:
            pat = "%" + escape_like(t.text) + "%"
            ors.append("src.title LIKE ? ESCAPE '\\' OR src.labels LIKE ? ESCAPE '\\' OR src.body LIKE ? ESCAPE '\\'")
            args["where"] += [pat] * 3
        where_parts.append("(" + " OR ".join(ors) + ")")
    for t in q.excluded:
        pat = "%" + escape_like(t.text) + "%"
        where_parts.append("NOT (src.title LIKE ? ESCAPE '\\' OR src.labels LIKE ? ESCAPE '\\' OR src.body LIKE ? ESCAPE '\\')")
        args["where"] += [pat] * 3
    return (" + ".join(score_parts) if score_parts else "0"), where_parts


def _relevance(score, fts):
    """Higher is better. FTS5's bm25 is negative-is-better, so flip it; the like engine's score already is."""
    if score is None:
        return None
    return round((-score if fts else score) + 0.0, 6)


def _item(r, engine, terms, browse):
    kind = r["kind"]
    fts = engine == "fts5" and not browse
    if fts:
        h_title, h_labels, h_body = r["h_title"], r["h_labels"], r["h_body"]
    else:
        raw_body = r["raw_body"] or ""
        h_title = _mark(r["raw_title"], terms)
        h_labels = _mark(r["raw_labels"], terms)
        h_body = _mark(raw_body, terms, SNIPPET_CHARS)
    matched = [name for name, text in (("title", h_title), ("labels", h_labels), ("body", h_body)) if ON in (text or "")]
    item = {"type": kind, "project": r["project_key"], "matched_in": matched, "score": _relevance(r["score"], fts)}
    if kind == "task":
        item.update(
            key="%s-%d" % (r["project_key"], r["number"]), title=r["task_title"], title_match=segments(h_title),
            status=r["status"], priority=r["priority"], assignee=r["assignee"],
        )  # fmt: skip
    elif kind == "comment":
        item.update(
            key="%s-%d" % (r["project_key"], r["number"]), title=r["task_title"], comment_id=r["comment_id"], author=r["author"], created_at=r["commented_at"],
        )  # fmt: skip
    else:
        item.update(key=r["project_key"], title=r["project_name"], title_match=segments(h_title.split(" ", 1)[1] if " " in h_title else h_title))
    snippet = h_body if ON in (h_body or "") or ON not in (h_labels or "") else h_labels
    item["snippet"] = segments(snippet) if snippet else []
    return item


def _pinned(ctx, raw):
    """A query that is just a task key goes straight to that task."""
    project, _, number = raw.rpartition("-")
    row = ctx.conn.execute(
        "SELECT t.id, t.number, t.title, t.status, t.priority, t.description, p.key AS project_key, a.username AS assignee "
        "FROM tasks t JOIN projects p ON p.id = t.project_id LEFT JOIN users a ON a.id = t.assignee_id WHERE p.key = ? AND t.number = ?",
        (project.upper(), int(number)),
    ).fetchone()
    if row is None:
        return None
    return {
        "type": "task", "project": row["project_key"], "matched_in": ["key"], "score": None, "exact_key": True,
        "key": "%s-%d" % (row["project_key"], row["number"]), "title": row["title"], "title_match": [{"text": row["title"], "match": False}],
        "status": row["status"], "priority": row["priority"], "assignee": row["assignee"],
        "snippet": segments(truncate(row["description"] or "", SNIPPET_CHARS)),
        "_task_id": row["id"],
    }  # fmt: skip


def search(ctx, params):
    raw = (params.get("q") or "").strip()
    if not raw:
        raise ValidationError({"q": "required"})
    limit, offset = _int(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT), _int(params, "offset", 0, 0, MAX_OFFSET)
    q = parse(raw)
    filters = _normalise_filters(ctx, q.filters)
    browse = not q.groups
    if browse and not filters:
        raise ValidationError({"q": "needs at least one word to look for, or a filter such as status:open"})
    engine = db.search_engine(ctx.conn) or "like"
    if browse or engine == "like":
        engine_used = "like"
    else:
        engine_used = "fts5"
    conds, fargs, about_tasks = _filter_sql(ctx, filters, "src" if engine_used == "like" else "search_index")
    if browse and not filters.get("in"):
        conds.append("src.kind = 'task'")
    pin = _pinned(ctx, raw) if _KEY.match(raw) and not filters else None  # a bare task key goes straight to that task
    exclude_id = pin["_task_id"] if pin else None
    # The pinned task is the first result of the whole list, so it takes one slot of page one and every later page
    # starts one row earlier in the ordinary results.
    sql_limit = limit - 1 if pin and offset == 0 else limit
    sql_offset = max(offset - 1, 0) if pin else offset

    if engine_used == "fts5":
        table = "search_index"
        where = ["search_index MATCH ?"] + conds
        wargs = [fts_expression(q)] + fargs
        if exclude_id is not None:
            where.append("search_index.rowid != ?")
            wargs.append(exclude_id * 4)
        body = " FROM search_index" + _JOINS.format(T=table) + " WHERE " + " AND ".join(where)
        select = (
            "SELECT search_index.kind AS kind, search_index.ref AS ref, bm25(search_index, 0, 0, 0, 0, 10.0, 4.0, 1.0) AS score, "
            "highlight(search_index, 4, char(2), char(3)) AS h_title, highlight(search_index, 5, char(2), char(3)) AS h_labels, "
            "snippet(search_index, 6, char(2), char(3), '\u2026', %d) AS h_body, %s" % (SNIPPET_WORDS, _COMMON)
        ) + body + " ORDER BY score, search_index.rowid DESC LIMIT ? OFFSET ?"
        rows = ctx.conn.execute(select, wargs + [sql_limit, sql_offset]).fetchall()
        counted = ctx.conn.execute("SELECT COUNT(*) FROM (SELECT 1" + body + " LIMIT ?)", wargs + [COUNT_CAP + 1]).fetchone()[0]
    else:
        args = {"score": [], "where": []}
        score_sql, term_where = _like_terms(q, args)
        args["where"] += fargs
        where = term_where + conds
        if exclude_id is not None:
            where.append("NOT (src.kind = 'task' AND src.ref = ?)")
            args["where"].append(exclude_id)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        joins = " FROM src" + _JOINS.format(T="src")
        order = " ORDER BY t.updated_at DESC, src.ref DESC" if browse else " ORDER BY score DESC, src.kind, src.ref DESC"
        select = (
            _SRC + " SELECT src.kind AS kind, src.ref AS ref, %s AS score, src.title AS raw_title, src.labels AS raw_labels, src.body AS raw_body, %s"
            % (score_sql if not browse else "NULL", _COMMON)
        ) + joins + where_sql + order + " LIMIT ? OFFSET ?"
        rows = ctx.conn.execute(select, args["score"] + args["where"] + [sql_limit, sql_offset]).fetchall()
        counted = ctx.conn.execute(_SRC + " SELECT COUNT(*) FROM (SELECT 1" + joins + where_sql + " LIMIT ?)", args["where"] + [COUNT_CAP + 1]).fetchone()[0]

    terms = q.terms
    items = [_item(r, engine_used, terms, browse) for r in rows]
    if pin is not None:
        counted += 1
        if offset == 0:
            items.insert(0, {k: v for k, v in pin.items() if k != "_task_id"})
    total = min(counted, COUNT_CAP)
    return {
        "query": raw,
        "engine": engine_used,
        "interpreted": {
            "all_of": [[t.shown() for t in g] for g in q.groups],
            "none_of": [t.shown() for t in q.excluded],
            "filters": filters,
        },
        "total": total,
        "total_capped": counted > COUNT_CAP,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


# ------------------------------------------------------------------------------ administration

_KIND_SOURCES = {"task": ("tasks", 4, 0), "comment": ("comments", 4, 1), "project": ("projects", 4, 2)}


def status(ctx):
    """Which engine this database uses and, for FTS5, whether the index matches the data."""
    engine = db.search_engine(ctx.conn)
    out = {"engine": engine, "fts5_available": db.fts5_available(), "limits": limits()}
    counts = {k: ctx.conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] for k, (table, _, _) in _KIND_SOURCES.items()}
    out["documents"] = counts
    if engine == "fts5":
        problems = {}
        for kind, (table, mult, add) in _KIND_SOURCES.items():
            missing = ctx.conn.execute(
                "SELECT COUNT(*) FROM %s WHERE id * %d + %d NOT IN (SELECT rowid FROM search_index)" % (table, mult, add)
            ).fetchone()[0]
            orphaned = ctx.conn.execute(
                "SELECT COUNT(*) FROM search_index WHERE kind = ? AND ref NOT IN (SELECT id FROM %s)" % table, (kind,)
            ).fetchone()[0]
            problems[kind] = {"missing": missing, "orphaned": orphaned}
        out["indexed"] = {r["kind"]: r["n"] for r in ctx.conn.execute("SELECT kind, COUNT(*) AS n FROM search_index GROUP BY kind")}
        out["problems"] = problems
        out["in_sync"] = all(v["missing"] == 0 and v["orphaned"] == 0 for v in problems.values())
    else:
        out["in_sync"] = True  # nothing to get out of step: the like engine reads the tables directly
    return out


def limits():
    return {"max_query_length": MAX_QUERY, "max_terms": MAX_TERMS, "default_limit": DEFAULT_LIMIT, "max_limit": MAX_LIMIT, "max_offset": MAX_OFFSET, "count_cap": COUNT_CAP}


def reindex(ctx):
    """Rebuild the index from the data (a repair tool; the triggers normally keep it exact)."""
    if db.search_engine(ctx.conn) != "fts5":
        raise Conflict("This database uses the substring search engine, which has no index to rebuild.", code="no_search_index")
    ctx.conn.execute("DELETE FROM search_index")
    for statement in db.SEARCH_BACKFILL_STATEMENTS:
        ctx.conn.execute(statement)
    counts = {r["kind"]: r["n"] for r in ctx.conn.execute("SELECT kind, COUNT(*) AS n FROM search_index GROUP BY kind")}
    activity.log(ctx, "system.search_reindexed", details={"rows": sum(counts.values())})
    return {"engine": "fts5", "indexed": counts}


def rebuild_database(path):
    """The command-line rebuild. Also moves a database that fell back to the substring engine onto FTS5 when this
    Python can do that. Returns what was done."""
    conn = db.connect(path)
    try:
        engine = db.search_engine(conn)
        if engine is None:
            raise RuntimeError("This database has no search support yet; start the server or run `zylker-pm init` to upgrade it.")
        switched = False
        if engine == "like" and db.fts5_available():
            db.switch_search_to_fts5(conn)
            engine, switched = "fts5", True
        if engine != "fts5":
            return {"engine": "like", "switched": False, "indexed": None, "note": "no index to rebuild: this SQLite has no FTS5"}
        if not switched:
            with db.transaction(conn, write=True):
                conn.execute("DELETE FROM search_index")
                for statement in db.SEARCH_BACKFILL_STATEMENTS:
                    conn.execute(statement)
        counts = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM search_index GROUP BY kind")}
        return {"engine": "fts5", "switched": switched, "indexed": counts}
    finally:
        conn.close()
