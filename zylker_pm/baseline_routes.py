"""HTTP handlers for baselines (registered by importing this module from ``routes``)."""

from __future__ import annotations

from .app import route
from .errors import NotFound
from .services import baselines


def _baseline_id(value):
    """The id in the path. Anything that is not a whole number SQLite can hold is a baseline that does not exist."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 0 < number < 2**63:
        raise NotFound("Baseline not found", code="baseline_not_found")
    return number


@route("GET", "/api/projects/{key}/baselines")
def list_baselines(ctx, req, key):
    return baselines.list_baselines(ctx, key)


@route("POST", "/api/projects/{key}/baselines", role="manager")
def create_baseline(ctx, req, key):
    return 201, baselines.create(ctx, key, req.json())


@route("GET", "/api/projects/{key}/baselines/{baseline_id}")
def get_baseline(ctx, req, key, baseline_id):
    return baselines.variance(ctx, key, _baseline_id(baseline_id))


@route("DELETE", "/api/projects/{key}/baselines/{baseline_id}", role="manager")
def delete_baseline(ctx, req, key, baseline_id):
    baselines.delete(ctx, key, _baseline_id(baseline_id))
    return 204, None
