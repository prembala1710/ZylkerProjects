"""HTTP handlers for project templates. Thin adapters over ``services.templates``."""

from __future__ import annotations

from .app import route
from .errors import NotFound
from .services import templates


def _id(value):
    try:
        return int(value)
    except ValueError:
        raise NotFound("Template not found", code="template_not_found") from None


@route("GET", "/api/templates")
def list_templates(ctx, req):
    return {"items": templates.list_templates(ctx)}


@route("GET", "/api/templates/{template_id}")
def get_template(ctx, req, template_id):
    return templates.get_template(ctx, _id(template_id))


@route("PATCH", "/api/templates/{template_id}", role="manager")
def update_template(ctx, req, template_id):
    return templates.update_template(ctx, _id(template_id), req.json())


@route("DELETE", "/api/templates/{template_id}", role="manager")
def delete_template(ctx, req, template_id):
    templates.delete_template(ctx, _id(template_id))
    return 204, None


@route("POST", "/api/templates/{template_id}/preview", role="manager")
def preview_template(ctx, req, template_id):
    return templates.preview(ctx, _id(template_id), req.json())


@route("POST", "/api/templates/{template_id}/instantiate", role="manager")
def instantiate_template(ctx, req, template_id):
    return 201, templates.instantiate(ctx, _id(template_id), req.json())


@route("POST", "/api/projects/{key}/save-as-template", role="manager")
def save_as_template(ctx, req, key):
    return 201, templates.save_from_project(ctx, key, req.json())
