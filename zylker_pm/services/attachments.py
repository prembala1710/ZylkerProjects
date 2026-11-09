"""File attachments on tasks.

Files are stored *inside* the SQLite database (``attachment_blobs``), next to their metadata (``attachments``).
That keeps the "everything in one file" promise: ``zylker-pm backup`` captures the files consistently, deleting a
task or a project removes its files in the same transaction, and there is no directory to keep in step with the
database. The price is database size, so uploads are capped per file, per task and in total.

The tables are created with ``CREATE TABLE IF NOT EXISTS`` by :func:`ensure_schema` whenever an ``App`` starts,
independently of ``PRAGMA user_version``; they are purely additive, so existing databases gain them without a
migration step and the feature cannot collide with other changes that bump the schema version.

Nothing here ever interprets a file: names are cleaned, the declared type is only a label, and the one thing that
looks at the bytes is the image sniffer that decides whether the UI may show a preview.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import unicodedata
from urllib.parse import quote

from ..db import connect
from ..errors import Conflict, Forbidden, NotFound, PayloadTooLarge, ValidationError
from ..util import escape_like
from . import activity, projects, taskrefs, users

MAX_PER_TASK = 25
MAX_FILENAME_CHARS = 180
MAX_EXTENSION_CHARS = 20
DEFAULT_FILENAME = "file"

ATTACHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size > 0),
    sha256 TEXT NOT NULL,
    preview TEXT,
    uploader_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments (task_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_project ON attachments (project_id, id);
CREATE TABLE IF NOT EXISTS attachment_blobs (
    attachment_id INTEGER PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
    data BLOB NOT NULL
);
"""


def ensure_schema(path: str) -> None:
    """Create the attachment tables in an initialised database file (no-op for new, missing or in-memory ones)."""
    if path == ":memory:" or not os.path.exists(path):
        return
    conn = connect(path)
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] > 0:
            conn.executescript(ATTACHMENT_SCHEMA)
    finally:
        conn.close()


def _require_schema(ctx):
    found = ctx.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'attachments'").fetchone()
    if found is None:
        raise Conflict(
            "The attachment tables are missing from this database. Restart the server (or run `zylker-pm init`) to create them.",
            code="attachments_not_initialised",
        )


# ------------------------------------------------------------------------------ names and types

# Control characters and invisible direction overrides ("invoice\u202egnp.exe" would display as "invoiceexe.png"). The
# zero-width joiners (U+200C, U+200D) stay: some scripts cannot be spelled without them.
_UNSAFE_NAME_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u200b\u200e\u200f\u2028\u2029\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}$")


def clean_filename(raw) -> str:
    """The name a file is stored and shown under: no directories, no control or direction characters, bounded length.

    Whatever the client sent is only ever a label; it is never used as a path. An unusable name becomes ``file``.
    """
    name = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(raw or "")))  # tabs and line breaks become single blanks
    name = _UNSAFE_NAME_CHARS.sub("", name)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # a browser or script may send a whole path
    name = name.strip().rstrip(" .")  # trailing dots and blanks mean nothing (or something odd) on Windows
    if not name:
        return DEFAULT_FILENAME
    if len(name) > MAX_FILENAME_CHARS:
        stem, ext = os.path.splitext(name)
        if not stem or len(ext) > MAX_EXTENSION_CHARS:
            stem, ext = name, ""
        name = stem[: MAX_FILENAME_CHARS - len(ext)].rstrip(" .") + ext
    return name or DEFAULT_FILENAME


def clean_content_type(declared, filename) -> str:
    """The declared ``type/subtype`` when it is well formed, else a guess from the extension, else octet-stream.

    The result is a label for API clients. It is never trusted to decide how a file is displayed.
    """
    value = (declared or "").split(";")[0].strip().lower()
    if _TYPE_RE.match(value) and value != "application/octet-stream":
        return value
    guessed = mimetypes.guess_type(filename, strict=False)[0]
    return guessed if guessed and _TYPE_RE.match(guessed) else "application/octet-stream"


def sniff_image(data: bytes):
    """The image type of ``data`` by its magic bytes, for the few raster formats that are safe to preview, else None.

    SVG is deliberately absent: it can carry script. A file that merely *claims* to be a PNG is not previewed.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def content_disposition(filename: str) -> str:
    """Always ``attachment``: a stored file is handed to the browser to save, never to render."""
    fallback = re.sub(r'[^\x20-\x7e]|["\\%]', "_", filename)
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (fallback, quote(filename, safe=""))


# ------------------------------------------------------------------------------ output

SELECT = (
    "SELECT a.*, u.username, u.display_name, t.number, t.title AS task_title, p.key AS project_key "
    "FROM attachments a JOIN tasks t ON t.id = a.task_id JOIN projects p ON p.id = a.project_id "
    "LEFT JOIN users u ON u.id = a.uploader_id "
)


def to_dict(row, *, task_title=False) -> dict:
    data = {
        "id": row["id"],
        "task": "%s-%d" % (row["project_key"], row["number"]),
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "sha256": row["sha256"],
        "preview": row["preview"],
        "uploaded_by": users.ref(row["uploader_id"], row["username"], row["display_name"]),
        "created_at": row["created_at"],
    }
    if task_title:
        data["task_title"] = row["task_title"]
    return data


def limits(ctx) -> dict:
    return {"max_file_bytes": ctx.config.attachment_max_bytes, "max_per_task": MAX_PER_TASK}


def _task_and_project(ctx, key):
    row = taskrefs.get_row(ctx.conn, key)
    return row, projects.get_row(ctx.conn, row["project_key"])


def _row(conn, attachment_id):
    row = conn.execute(SELECT + "WHERE a.id = ?", (attachment_id,)).fetchone()
    if row is None:
        raise NotFound("Attachment not found", code="attachment_not_found")
    return row


# ------------------------------------------------------------------------------ reads


def list_for_task(ctx, key):
    _require_schema(ctx)
    task = taskrefs.get_row(ctx.conn, key)
    rows = ctx.conn.execute(SELECT + "WHERE a.task_id = ? ORDER BY a.id", (task["id"],)).fetchall()
    return {"items": [to_dict(r) for r in rows], "total_bytes": sum(r["size"] for r in rows), "limits": limits(ctx)}


def list_for_project(ctx, key, params):
    _require_schema(ctx)
    project = projects.get_row(ctx.conn, key)
    try:
        limit, offset = int(params.get("limit", 25)), int(params.get("offset", 0))
    except ValueError:
        raise ValidationError({"limit": "limit and offset must be integers"}) from None
    if limit < 1 or offset < 0:
        raise ValidationError({"limit": "limit must be >= 1 and offset >= 0"})
    limit = min(limit, 100)
    where, args = "WHERE a.project_id = ?", [project["id"]]
    q = (params.get("q") or "").strip()
    if q:
        where += " AND a.filename LIKE ? ESCAPE '\\'"
        args.append("%" + escape_like(q) + "%")
    total, total_bytes = ctx.conn.execute("SELECT COUNT(*), COALESCE(SUM(a.size), 0) FROM attachments a " + where, args).fetchone()
    rows = ctx.conn.execute(SELECT + where + " ORDER BY a.id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    return {
        "items": [to_dict(r, task_title=True) for r in rows],
        "total": total,
        "total_bytes": total_bytes,
        "limit": limit,
        "offset": offset,
    }


def get_content(ctx, attachment_id):
    """The metadata row and the file's bytes."""
    _require_schema(ctx)
    row = _row(ctx.conn, attachment_id)
    blob = ctx.conn.execute("SELECT data FROM attachment_blobs WHERE attachment_id = ?", (attachment_id,)).fetchone()
    if blob is None:  # cannot happen through this code (one transaction writes both), but a damaged file must not 500
        raise NotFound("The file's content is missing", code="attachment_content_missing")
    return row, bytes(blob["data"])


# ------------------------------------------------------------------------------ writes


def add(ctx, key, filename, content_type, data: bytes):
    _require_schema(ctx)
    task, project = _task_and_project(ctx, key)
    projects.require_write(ctx, project)
    if not isinstance(filename, str) or not filename.strip():
        raise ValidationError({"filename": "required: pass the file name as ?filename=..."})
    if not data:
        raise ValidationError({"file": "is empty: send the file as the request body"})
    maximum = ctx.config.attachment_max_bytes
    if len(data) > maximum:
        raise PayloadTooLarge("The file is %d bytes; attachments can be at most %d bytes" % (len(data), maximum), code="attachment_too_large")
    count = ctx.conn.execute("SELECT COUNT(*) FROM attachments WHERE task_id = ?", (task["id"],)).fetchone()[0]
    if count >= MAX_PER_TASK:
        raise Conflict("%s already has %d attachments, the most a task can hold" % (taskrefs.key_of(task), MAX_PER_TASK), code="too_many_attachments")
    quota = ctx.config.attachment_quota_bytes
    if quota:
        used = ctx.conn.execute("SELECT COALESCE(SUM(size), 0) FROM attachments").fetchone()[0]
        if used + len(data) > quota:
            raise Conflict(
                "The attachment storage limit of %d bytes has been reached. Delete files you no longer need." % quota,
                code="storage_quota_exceeded",
            )
    name = clean_filename(filename)
    cur = ctx.conn.execute(
        "INSERT INTO attachments (project_id, task_id, filename, content_type, size, sha256, preview, uploader_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task["project_id"], task["id"], name, clean_content_type(content_type, name), len(data),
            hashlib.sha256(data).hexdigest(), sniff_image(data), ctx.actor_id, ctx.now_iso,
        ),
    )  # fmt: skip
    ctx.conn.execute("INSERT INTO attachment_blobs (attachment_id, data) VALUES (?, ?)", (cur.lastrowid, data))
    activity.log(
        ctx, "attachment.added", project_id=task["project_id"], task_id=task["id"], task_key=taskrefs.key_of(task),
        details={"attachment_id": cur.lastrowid, "filename": name, "size": len(data)},
    )  # fmt: skip
    return to_dict(_row(ctx.conn, cur.lastrowid))


def delete(ctx, attachment_id):
    _require_schema(ctx)
    row = _row(ctx.conn, attachment_id)
    if ctx.user is not None and ctx.user["id"] != row["uploader_id"] and not ctx.has_role("manager"):
        raise Forbidden("Only the person who attached a file, or a manager, can delete it", code="not_author")
    projects.require_write(ctx, projects.get_row(ctx.conn, row["project_key"]))
    ctx.conn.execute("DELETE FROM attachments WHERE id = ?", (row["id"],))  # the blob goes with it (ON DELETE CASCADE)
    activity.log(
        ctx, "attachment.deleted", project_id=row["project_id"], task_id=row["task_id"], task_key="%s-%d" % (row["project_key"], row["number"]),
        details={"attachment_id": row["id"], "filename": row["filename"], "size": row["size"]},
    )  # fmt: skip
