"""Custom project fields: organisation-wide definitions (text, number, URL) and one value per project.

Definitions are managed by admins; managers fill in the values when they create or edit a project.
Everything is validated here, on the server - the UI only mirrors these rules to give faster feedback.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from ..errors import Conflict, NotFound, ValidationError
from ..util import Checker, truncate
from . import activity

FIELD_TYPES = ("text", "number", "url")

MAX_FIELDS = 20  # keeps forms, payloads and the project list manageable
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

DEFAULT_MAX_LENGTH = 255
MAX_TEXT_LENGTH = 2000
MAX_URL_LENGTH = 2000
DEFAULT_DECIMALS = 2
MAX_DECIMALS = 3
# 1e12 with 3 decimals is 1e15, still below 2**53, so SQLite REALs hold every accepted value exactly.
NUMBER_LIMIT = 1e12

LIMITS = {
    "max_fields": MAX_FIELDS,
    "default_max_length": DEFAULT_MAX_LENGTH,
    "max_text_length": MAX_TEXT_LENGTH,
    "max_url_length": MAX_URL_LENGTH,
    "default_decimals": DEFAULT_DECIMALS,
    "max_decimals": MAX_DECIMALS,
    "number_limit": NUMBER_LIMIT,
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")
# Whitespace and characters RFC 3986 forbids. The backslash matters most: browsers treat it as "/" while
# Python does not, so "https://evil.example\@trusted.example" would be read differently by the two.
_URL_FORBIDDEN = re.compile(r"[\s\\<>\"`{}|^]")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$|^[0-9A-Fa-f:.]+$")

SELECT = "SELECT * FROM project_fields ORDER BY id"


# ------------------------------------------------------------------------------ definitions


def get_row(conn, key):
    row = conn.execute("SELECT * FROM project_fields WHERE key = ?", ((key or "").lower(),)).fetchone()
    if row is None:
        raise NotFound("Project field %r not found" % truncate(key, 60), code="field_not_found")
    return row


def to_dict(row):
    kind = row["type"]
    return {
        "key": row["key"],
        "label": row["label"],
        "type": kind,
        "description": row["description"],
        "required": bool(row["required"]),
        "max_length": row["max_length"] if kind == "text" else None,
        "min_value": _num_out(row["min_value"], row["decimals"]) if kind == "number" else None,
        "max_value": _num_out(row["max_value"], row["decimals"]) if kind == "number" else None,
        "decimals": row["decimals"] if kind == "number" else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_fields(ctx):
    return [to_dict(r) for r in ctx.conn.execute(SELECT)]


def _check_definition(c, kind, existing=None):
    """Validate the type-specific settings; ``kind`` is the field's (fixed) type."""
    c.text("description", max_len=200)
    c.boolean("required")
    c.integer("max_length", minimum=1, maximum=MAX_TEXT_LENGTH)
    c.number("min_value", minimum=-NUMBER_LIMIT, maximum=NUMBER_LIMIT, nullable=True, digits=6)
    c.number("max_value", minimum=-NUMBER_LIMIT, maximum=NUMBER_LIMIT, nullable=True, digits=6)
    c.integer("decimals", minimum=0, maximum=MAX_DECIMALS)
    applies = {"max_length": "text", "min_value": "number", "max_value": "number", "decimals": "number"}
    for name, only in applies.items():
        if kind is not None and name in c.data and kind != only and name not in c.errors:
            c.errors[name] = "only applies to %s fields" % only


def create_field(ctx, data):
    c = Checker(data, {"key", "label", "type", "description", "required", "max_length", "min_value", "max_value", "decimals"})
    c.text("key", required=True, min_len=2, max_len=40)
    c.text("label", required=True, min_len=1, max_len=50)
    c.choice("type", FIELD_TYPES, required=True)
    kind = c.data.get("type") if c.data.get("type") in FIELD_TYPES else None
    _check_definition(c, kind)
    v = c.result()
    if not KEY_RE.match(v["key"]):
        raise ValidationError({"key": "2-40 characters: lower-case letters, digits and underscores, starting with a letter"})
    _check_bounds(v.get("min_value"), v.get("max_value"))
    kind = v["type"]

    if ctx.conn.execute("SELECT COUNT(*) FROM project_fields").fetchone()[0] >= MAX_FIELDS:
        raise Conflict("At most %d custom project fields are allowed. Delete one first." % MAX_FIELDS, code="field_limit_reached")
    if ctx.conn.execute("SELECT 1 FROM project_fields WHERE key = ?", (v["key"],)).fetchone():
        raise Conflict("A field with key %r already exists" % v["key"], code="field_key_taken")
    _ensure_label_free(ctx.conn, v["label"])

    cur = ctx.conn.execute(
        "INSERT INTO project_fields (key, label, type, description, required, max_length, min_value, max_value, "
        "decimals, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            v["key"],
            v["label"],
            kind,
            v.get("description", ""),
            int(v.get("required", False)),
            v.get("max_length", DEFAULT_MAX_LENGTH) if kind == "text" else None,
            v.get("min_value") if kind == "number" else None,
            v.get("max_value") if kind == "number" else None,
            v.get("decimals", DEFAULT_DECIMALS) if kind == "number" else None,
            ctx.now_iso,
            ctx.now_iso,
        ),
    )
    activity.log(ctx, "project_field.created", details={"key": v["key"], "label": v["label"], "type": kind})
    return to_dict(ctx.conn.execute("SELECT * FROM project_fields WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_field(ctx, key, data):
    row = get_row(ctx.conn, key)
    kind = row["type"]
    c = Checker(data, {"key", "label", "type", "description", "required", "max_length", "min_value", "max_value", "decimals"}, partial=True)
    for name in ("key", "type"):
        if name in c.data:  # replace the generic message with the real reason
            c.errors[name] = "cannot be changed after the field is created"
    c.text("label", min_len=1, max_len=50)
    _check_definition(c, kind)
    v = c.result()
    _check_bounds(v.get("min_value", row["min_value"]), v.get("max_value", row["max_value"]))
    if "label" in v and v["label"].lower() != row["label"].lower():
        _ensure_label_free(ctx.conn, v["label"])

    changes = {}
    for name, new in v.items():
        old = row[name] if name != "required" else bool(row["required"])
        if old != new:
            changes[name] = [old, new]
    if not changes:
        return to_dict(row)

    new_def = {**dict(row), **{n: (int(c_[1]) if n == "required" else c_[1]) for n, c_ in changes.items()}}
    if {"max_length", "min_value", "max_value", "decimals"} & changes.keys():
        _ensure_values_still_valid(ctx.conn, new_def)

    sets = ", ".join("%s = ?" % n for n in changes)
    ctx.conn.execute(
        "UPDATE project_fields SET %s, updated_at = ? WHERE id = ?" % sets,
        [new_def[n] for n in changes] + [ctx.now_iso, row["id"]],
    )
    activity.log(ctx, "project_field.updated", details={"key": row["key"], "changes": changes})
    return to_dict(get_row(ctx.conn, key))


def delete_field(ctx, key, confirm):
    row = get_row(ctx.conn, key)
    used = ctx.conn.execute("SELECT COUNT(*) FROM project_field_values WHERE field_id = ?", (row["id"],)).fetchone()[0]
    if confirm != row["key"]:
        raise Conflict(
            "Deleting this field also removes its value from %d project(s). Repeat the request with ?confirm=%s" % (used, row["key"]),
            code="confirmation_required",
            details={"projects_with_a_value": used},
        )
    ctx.conn.execute("DELETE FROM project_fields WHERE id = ?", (row["id"],))  # values go with it (ON DELETE CASCADE)
    activity.log(ctx, "project_field.deleted", details={"key": row["key"], "label": row["label"], "values_removed": used})


def _ensure_label_free(conn, label):
    if conn.execute("SELECT 1 FROM project_fields WHERE label = ?", (label,)).fetchone():  # column is NOCASE
        raise Conflict("A field labelled %r already exists" % label, code="field_label_taken")


def _check_bounds(low, high):
    if low is not None and high is not None and low > high:
        raise ValidationError({"max_value": "must not be less than min_value"})


def _ensure_values_still_valid(conn, new_def):
    """Tightening a limit must not silently orphan values that were valid when they were saved."""
    bad = []
    for r in conn.execute(
        "SELECT p.key, v.text_value, v.number_value FROM project_field_values v JOIN projects p ON p.id = v.project_id "
        "WHERE v.field_id = ? ORDER BY p.key",
        (new_def["id"],),
    ):
        stored = r["text_value"] if r["text_value"] is not None else r["number_value"]
        try:
            validate_value(new_def, stored)
        except ValueError:
            bad.append(r["key"])
    if bad:
        raise Conflict(
            "%d project(s) already hold values that break the new limits (%s). Edit those values first."
            % (len(bad), ", ".join(bad[:5]) + (", ..." if len(bad) > 5 else "")),
            code="existing_values_invalid",
            details={"projects": bad[:20], "count": len(bad)},
        )


# ------------------------------------------------------------------------------ validation


def _num_out(value, decimals):
    if value is None:
        return None
    return int(value) if not decimals else value


def validate_value(f, value):
    """Return the normalised value (``None`` means "no value") or raise ``ValueError`` with a message."""
    if value is None:
        return None
    kind = f["type"]
    if kind == "number":
        return _validate_number(f, value)
    if not isinstance(value, str):
        raise ValueError("must be a string")
    value = value.strip()
    if value == "":
        return None
    if _CONTROL.search(value):
        raise ValueError("must be a single line without control characters")
    if kind == "text":
        limit = f["max_length"] or DEFAULT_MAX_LENGTH
        if len(value) > limit:
            raise ValueError("must be at most %d characters (got %d)" % (limit, len(value)))
        return value
    return _validate_url(value)


def _validate_number(f, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("must be a finite number")
    if abs(value) > NUMBER_LIMIT:
        raise ValueError("must be between -{0:,.0f} and {0:,.0f}".format(NUMBER_LIMIT))
    decimals = f["decimals"] if f["decimals"] is not None else DEFAULT_DECIMALS
    try:
        exponent = Decimal(str(value)).normalize().as_tuple().exponent
    except InvalidOperation:  # pragma: no cover - finite numbers always parse
        raise ValueError("must be a number") from None
    if -exponent > decimals:
        raise ValueError("must have at most %d decimal place%s" % (decimals, "" if decimals == 1 else "s"))
    low, high = f["min_value"], f["max_value"]
    if low is not None and value < low:
        raise ValueError("must be at least %s" % _fmt(low))
    if high is not None and value > high:
        raise ValueError("must be at most %s" % _fmt(high))
    return float(value) + 0.0  # + 0.0 turns -0.0 into 0.0


def _fmt(number):
    return ("%.6f" % number).rstrip("0").rstrip(".")


def _validate_url(value):
    if len(value) > MAX_URL_LENGTH:
        raise ValueError("must be at most %d characters" % MAX_URL_LENGTH)
    if _URL_FORBIDDEN.search(value):
        raise ValueError("contains characters that are not allowed in a web address (spaces, backslashes, quotes, < >); percent-encode them")
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError("is not a valid web address") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("must start with http:// or https://")
    host = parts.hostname
    if not host or not _HOST_RE.match(host) or len(host) > 253 or host.startswith(".") or ".." in host:
        raise ValueError("must include a valid host name, e.g. https://example.com/page")
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not contain a username or password")
    if port == 0:
        raise ValueError("is not a valid web address")
    return value


def validate_input(conn, raw, *, creating):
    """Validate a ``custom_fields`` object. Returns ``{key: value | None}``; raises one 422 listing every problem."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValidationError({"custom_fields": "must be an object mapping field keys to values"})
    defs = {r["key"]: r for r in conn.execute(SELECT)}
    errors, bad, out = {}, set(), {}
    for key, value in raw.items():
        d = defs.get(key)
        where = "custom_fields." + truncate(key, 60)
        if d is None:
            errors[where] = "unknown custom field"
            continue
        try:
            out[key] = validate_value(d, value)
        except ValueError as exc:
            errors[where] = str(exc)
            bad.add(key)
    for key, d in defs.items():
        if not d["required"] or key in bad:
            continue
        if creating and out.get(key) is None:
            errors["custom_fields." + key] = "is required"
        elif not creating and key in out and out[key] is None:
            errors["custom_fields." + key] = "is required and cannot be cleared"
    if errors:
        raise ValidationError(errors)
    return out


# ------------------------------------------------------------------------------ values


def values_map(conn, project_ids):
    """``{project_id: {field_key: value | None}}`` with an entry for every defined field."""
    defs = conn.execute(SELECT).fetchall()
    result = {pid: {d["key"]: None for d in defs} for pid in project_ids}
    if not defs or not project_ids:
        return result
    by_id = {d["id"]: d for d in defs}
    ids = list(project_ids)
    for i in range(0, len(ids), 500):  # stay under SQLite's bound-parameter limit
        chunk = ids[i : i + 500]
        for r in conn.execute(
            "SELECT project_id, field_id, text_value, number_value FROM project_field_values WHERE project_id IN (%s)" % ",".join("?" * len(chunk)),
            chunk,
        ):
            d = by_id[r["field_id"]]
            result[r["project_id"]][d["key"]] = r["text_value"] if d["type"] != "number" else _num_out(r["number_value"], d["decimals"])
    return result


def store(ctx, project_id, validated):
    """Persist validated values. Returns the audit diff ``{"custom_fields.<key>": [old, new]}`` (changes only)."""
    if not validated:
        return {}
    defs = {d["key"]: d for d in ctx.conn.execute(SELECT)}
    current = values_map(ctx.conn, [project_id])[project_id]
    changes = {}
    for key, new in validated.items():
        d, old = defs[key], current.get(key)
        if old == new:
            continue
        ctx.conn.execute("DELETE FROM project_field_values WHERE project_id = ? AND field_id = ?", (project_id, d["id"]))
        if new is not None:
            is_number = d["type"] == "number"
            ctx.conn.execute(
                "INSERT INTO project_field_values (project_id, field_id, text_value, number_value, updated_at) VALUES (?,?,?,?,?)",
                (project_id, d["id"], None if is_number else new, new if is_number else None, ctx.now_iso),
            )
        shown = _num_out(new, d["decimals"]) if d["type"] == "number" else new
        changes["custom_fields." + key] = [activity.diff_value(old), activity.diff_value(shown)]
    return changes
