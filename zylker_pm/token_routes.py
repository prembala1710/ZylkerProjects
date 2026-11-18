"""HTTP handlers for API tokens. Every signed-in user manages their own; administrators can manage anyone's.
Thin adapters over ``services.tokens`` (which also refuses to let an API token manage tokens)."""

from __future__ import annotations

from .app import route
from .errors import NotFound
from .services import tokens


def _id(value):
    try:
        return int(value)
    except ValueError:
        raise NotFound("Token not found", code="token_not_found") from None


@route("GET", "/api/tokens")
def list_tokens(ctx, req):
    return tokens.list_tokens(ctx, req.query)


@route("POST", "/api/tokens")
def create_token(ctx, req):
    return 201, tokens.create_token(ctx, req.json())


@route("DELETE", "/api/tokens")
def revoke_all_tokens(ctx, req):
    return tokens.revoke_all(ctx, req.query.get("user"))


# "current" must be registered before "{token_id}", which would otherwise capture it
@route("GET", "/api/tokens/current")
def current_token(ctx, req):
    return tokens.current(ctx)


@route("GET", "/api/tokens/{token_id}")
def get_token(ctx, req, token_id):
    return tokens.get_token(ctx, _id(token_id))


@route("DELETE", "/api/tokens/{token_id}")
def revoke_token(ctx, req, token_id):
    return tokens.revoke_token(ctx, _id(token_id))
