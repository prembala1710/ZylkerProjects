"""HTTP handlers for the delivery projection (registered by importing this module from ``routes``)."""

from __future__ import annotations

from .app import route
from .services import projection


@route("GET", "/api/projects/{key}/projection")
def project_projection(ctx, req, key):
    return projection.project_projection(ctx, key, req.query)


@route("GET", "/api/reports/projection")
def portfolio_projection(ctx, req):
    return projection.portfolio_projection(ctx, req.query)


@route("GET", "/api/projects/{key}/projection/advice")
def project_advice(ctx, req, key):
    return projection.project_advice(ctx, key, req.query)
