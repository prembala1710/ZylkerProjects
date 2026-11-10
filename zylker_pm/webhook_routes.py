"""HTTP handlers for outbound webhooks. Everything here is admin-only: a webhook URL makes the server call out,
and its secret signs what is sent."""

from __future__ import annotations

from .app import route
from .routes import _id
from .services import webhooks

# "/events" must be registered before "/{webhook_id}" so the literal path wins.


@route("GET", "/api/webhooks/events", role="admin")
def webhook_events(ctx, req):
    return webhooks.catalog()


@route("GET", "/api/webhooks", role="admin")
def list_webhooks(ctx, req):
    return webhooks.list_webhooks(ctx)


@route("POST", "/api/webhooks", role="admin")
def create_webhook(ctx, req):
    return 201, webhooks.create_webhook(ctx, req.json())


@route("GET", "/api/webhooks/{webhook_id}", role="admin")
def get_webhook(ctx, req, webhook_id):
    return webhooks.get_webhook(ctx, _id(webhook_id, "webhook"))


@route("PATCH", "/api/webhooks/{webhook_id}", role="admin")
def update_webhook(ctx, req, webhook_id):
    return webhooks.update_webhook(ctx, _id(webhook_id, "webhook"), req.json())


@route("DELETE", "/api/webhooks/{webhook_id}", role="admin")
def delete_webhook(ctx, req, webhook_id):
    webhooks.delete_webhook(ctx, _id(webhook_id, "webhook"))
    return 204, None


@route("POST", "/api/webhooks/{webhook_id}/rotate-secret", role="admin")
def rotate_webhook_secret(ctx, req, webhook_id):
    return webhooks.rotate_secret(ctx, _id(webhook_id, "webhook"))


@route("POST", "/api/webhooks/{webhook_id}/test", role="admin")
def test_webhook(ctx, req, webhook_id):
    return 202, webhooks.send_test(ctx, _id(webhook_id, "webhook"))


@route("GET", "/api/webhooks/{webhook_id}/deliveries", role="admin")
def list_webhook_deliveries(ctx, req, webhook_id):
    return webhooks.list_deliveries(ctx, _id(webhook_id, "webhook"), req.query)


@route("GET", "/api/webhooks/{webhook_id}/deliveries/{delivery_id}", role="admin")
def get_webhook_delivery(ctx, req, webhook_id, delivery_id):
    return webhooks.get_delivery(ctx, _id(webhook_id, "webhook"), _id(delivery_id, "delivery"))


@route("POST", "/api/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver", role="admin")
def redeliver_webhook_delivery(ctx, req, webhook_id, delivery_id):
    return 202, webhooks.redeliver(ctx, _id(webhook_id, "webhook"), _id(delivery_id, "delivery"))
