"""HTTP handlers for MSP clients and the client portal (registered by importing this module from ``routes``)."""

from __future__ import annotations

from .app import route
from .errors import NotFound
from .services import clients, portal


def _number(value, what, code):
    """A whole number SQLite can hold; anything else is something that does not exist."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 0 < number < 2**63:
        raise NotFound(what, code=code)
    return number


def _client_id(value):
    return _number(value, "Client not found", "client_not_found")


def _contact_id(value):
    return _number(value, "This client has no such contact", "contact_not_found")


# ----------------------------------------------------------------------------------------- staff: clients and contacts


@route("GET", "/api/clients")
def list_clients(ctx, req):
    return clients.list_clients(ctx)


@route("POST", "/api/clients", role="manager")
def create_client(ctx, req):
    return 201, clients.create_client(ctx, req.json())


@route("GET", "/api/clients/{client_id}")
def get_client(ctx, req, client_id):
    return clients.get_client(ctx, _client_id(client_id))


@route("PATCH", "/api/clients/{client_id}", role="manager")
def update_client(ctx, req, client_id):
    return clients.update_client(ctx, _client_id(client_id), req.json())


@route("DELETE", "/api/clients/{client_id}", role="admin")
def delete_client(ctx, req, client_id):
    clients.delete_client(ctx, _client_id(client_id))
    return 204, None


@route("PUT", "/api/clients/{client_id}/projects/{key}", role="manager")
def put_client_project(ctx, req, client_id, key):
    return clients.put_project(ctx, _client_id(client_id), key, req.json())


@route("DELETE", "/api/clients/{client_id}/projects/{key}", role="manager")
def remove_client_project(ctx, req, client_id, key):
    clients.remove_project(ctx, _client_id(client_id), key)
    return 204, None


@route("POST", "/api/clients/{client_id}/contacts", role="admin")
def create_contact(ctx, req, client_id):
    return 201, clients.create_contact(ctx, _client_id(client_id), req.json())


@route("PATCH", "/api/clients/{client_id}/contacts/{user_id}", role="admin")
def update_contact(ctx, req, client_id, user_id):
    return clients.update_contact(ctx, _client_id(client_id), _contact_id(user_id), req.json())


@route("POST", "/api/clients/{client_id}/contacts/{user_id}/rotate-token", role="admin")
def rotate_contact_token(ctx, req, client_id, user_id):
    return clients.rotate_contact_token(ctx, _client_id(client_id), _contact_id(user_id))


# -------------------------------------------------------------------- the portal: the only routes a contact may call


@route("GET", "/api/portal/projects", client=True)
def portal_projects(ctx, req):
    return portal.list_projects(ctx, req.query)


@route("GET", "/api/portal/projects/{key}", client=True)
def portal_project(ctx, req, key):
    return portal.project_report(ctx, key, req.query)
