"""HTTP handlers for sprints. Thin adapters over ``services.sprints``."""

from __future__ import annotations

from .app import route
from .errors import NotFound
from .services import sprints


def _id(value):
    try:
        return int(value)
    except ValueError:
        raise NotFound("Sprint not found", code="sprint_not_found") from None


@route("GET", "/api/projects/{key}/sprints")
def list_sprints(ctx, req, key):
    return {"items": sprints.list_sprints(ctx, key, req.query)}


@route("POST", "/api/projects/{key}/sprints", role="manager")
def create_sprint(ctx, req, key):
    return 201, sprints.create_sprint(ctx, key, req.json())


@route("GET", "/api/projects/{key}/backlog")
def project_backlog(ctx, req, key):
    return sprints.backlog(ctx, key)


@route("GET", "/api/projects/{key}/velocity")
def project_velocity(ctx, req, key):
    return sprints.velocity(ctx, key, req.query)


@route("GET", "/api/sprints/{sprint_id}")
def get_sprint(ctx, req, sprint_id):
    return sprints.get_sprint(ctx, _id(sprint_id))


@route("PATCH", "/api/sprints/{sprint_id}", role="manager")
def update_sprint(ctx, req, sprint_id):
    return sprints.update_sprint(ctx, _id(sprint_id), req.json())


@route("DELETE", "/api/sprints/{sprint_id}", role="manager")
def delete_sprint(ctx, req, sprint_id):
    sprints.delete_sprint(ctx, _id(sprint_id))
    return 204, None


@route("POST", "/api/sprints/{sprint_id}/start", role="manager")
def start_sprint(ctx, req, sprint_id):
    return sprints.start_sprint(ctx, _id(sprint_id))


@route("POST", "/api/sprints/{sprint_id}/close", role="manager")
def close_sprint(ctx, req, sprint_id):
    return sprints.close_sprint(ctx, _id(sprint_id), req.json())


@route("POST", "/api/sprints/{sprint_id}/tasks", role="member")
def add_sprint_tasks(ctx, req, sprint_id):
    return sprints.add_tasks(ctx, _id(sprint_id), req.json())


@route("DELETE", "/api/sprints/{sprint_id}/tasks/{key}", role="member")
def remove_sprint_task(ctx, req, sprint_id, key):
    return sprints.remove_task(ctx, _id(sprint_id), key)


@route("GET", "/api/sprints/{sprint_id}/burndown")
def sprint_burndown(ctx, req, sprint_id):
    return sprints.burndown(ctx, _id(sprint_id))
