"""HTTP handlers for search. Thin adapters over ``services.search``."""

from __future__ import annotations

from .app import route
from .services import search


@route("GET", "/api/search")
def search_everything(ctx, req):
    return search.search(ctx, req.query)


@route("GET", "/api/search/status", role="admin")
def search_status(ctx, req):
    return search.status(ctx)


@route("POST", "/api/search/reindex", role="admin")
def search_reindex(ctx, req):
    return search.reindex(ctx)
