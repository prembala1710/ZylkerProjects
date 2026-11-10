"""HTTP handlers for the Microsoft Project export and import (registered by importing this module from ``routes``)."""

from __future__ import annotations

import re

from .app import Raw, route
from .errors import UnsupportedMediaType
from .services import msproject
from .util import truthy

XML_TYPES = ("application/xml", "text/xml")


@route("GET", "/api/projects/{key}/export/msproject")
def export_msproject(ctx, req, key):
    body = msproject.export_xml(ctx, key)
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", key.upper()) + "-msproject.xml"
    return Raw(body, "application/xml; charset=utf-8", headers=[("Content-Disposition", 'attachment; filename="%s"' % filename)])


# The body is the XML file itself. It is read before the transaction starts, so a slow upload never holds the database's
# write lock, and the size limit is the one the CSV import has.
@route("POST", "/api/projects/{key}/import/msproject", role="manager", body_limit="max_import_bytes", prefetch=True)
def import_msproject(ctx, req, key):
    if req.content_type not in XML_TYPES:
        raise UnsupportedMediaType("Content-Type must be application/xml or text/xml")
    return msproject.import_xml(
        ctx,
        key,
        req.body(),
        dry_run=truthy(req.query.get("dry_run")),
        add_missing_members=truthy(req.query.get("add_missing_members")),
    )
