"""HTTP handlers for task attachments (registered by importing this module from ``routes``)."""

from __future__ import annotations

from .app import Raw, route
from .errors import BadRequest, NotFound
from .services import attachments


def _attachment_id(value):
    try:
        return int(value)
    except ValueError:
        raise NotFound("Attachment not found", code="attachment_not_found") from None


@route("GET", "/api/tasks/{key}/attachments")
def list_task_attachments(ctx, req, key):
    return attachments.list_for_task(ctx, key)


# The body is the file itself (no multipart envelope). It is read before the transaction starts so that a slow upload
# never holds the database's write lock, and the route's size limit is the configured attachment limit.
@route("POST", "/api/tasks/{key}/attachments", role="member", body_limit="attachment_max_bytes", prefetch=True)
def upload_attachment(ctx, req, key):
    if not req.length and req.transfer_encoding:  # e.g. `curl -T -`: this server needs to know the size up front
        raise BadRequest("Chunked uploads are not supported: send a Content-Length (curl --data-binary @file does)", code="length_required")
    return 201, attachments.add(ctx, key, req.query.get("filename"), req.content_type, req.body())


@route("GET", "/api/attachments/{attachment_id}/content")
def download_attachment(ctx, req, attachment_id):
    row, data = attachments.get_content(ctx, _attachment_id(attachment_id))
    return Raw(data, row["content_type"], headers=[("Content-Disposition", attachments.content_disposition(row["filename"]))])


@route("DELETE", "/api/attachments/{attachment_id}", role="member")
def delete_attachment(ctx, req, attachment_id):
    attachments.delete(ctx, _attachment_id(attachment_id))
    return 204, None


@route("GET", "/api/projects/{key}/attachments")
def list_project_attachments(ctx, req, key):
    return attachments.list_for_project(ctx, key, req.query)
