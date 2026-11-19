"""HTTP handlers for the notification inbox and watching. Thin adapters over ``services.inbox``.

Everything here belongs to the signed-in user: nobody reads or changes anyone else's inbox, watches or settings.
The fixed paths (``count``, ``read``, ``unread``) are registered before ``{item_id}`` so they are never taken for an id.
"""

from __future__ import annotations

from .app import route
from .errors import NotFound
from .services import inbox


def _id(value):
    try:
        return int(value)
    except ValueError:
        raise NotFound("Notification not found", code="notification_not_found") from None


@route("GET", "/api/inbox")
def list_inbox(ctx, req):
    return inbox.list_items(ctx, req.query)


@route("GET", "/api/inbox/count")
def inbox_count(ctx, req):
    return inbox.count(ctx)


@route("POST", "/api/inbox/read")
def inbox_read(ctx, req):
    return inbox.mark_read(ctx, req.json())


@route("POST", "/api/inbox/unread")
def inbox_unread(ctx, req):
    return inbox.mark_unread(ctx, req.json())


@route("DELETE", "/api/inbox")
def inbox_clear(ctx, req):
    return inbox.clear(ctx, req.query)


@route("GET", "/api/inbox/{item_id}")
def get_inbox_item(ctx, req, item_id):
    return inbox.get_item(ctx, _id(item_id))


@route("DELETE", "/api/inbox/{item_id}")
def delete_inbox_item(ctx, req, item_id):
    inbox.delete_item(ctx, _id(item_id))
    return 204, None


@route("GET", "/api/me/notification-settings")
def get_notification_settings(ctx, req):
    return inbox.get_settings(ctx)


@route("PUT", "/api/me/notification-settings")
def put_notification_settings(ctx, req):
    return inbox.update_settings(ctx, req.json())


@route("GET", "/api/watching")
def my_watching(ctx, req):
    return inbox.watching(ctx, req.query)


@route("GET", "/api/tasks/{key}/watchers")
def task_watchers(ctx, req, key):
    return inbox.task_watchers(ctx, key)


@route("PUT", "/api/tasks/{key}/watch")
def watch_task(ctx, req, key):
    return inbox.set_task_watch(ctx, key, req.json())


@route("DELETE", "/api/tasks/{key}/watch")
def unwatch_task(ctx, req, key):
    inbox.clear_task_watch(ctx, key)
    return 204, None


@route("PUT", "/api/projects/{key}/watch")
def watch_project(ctx, req, key):
    return inbox.set_project_watch(ctx, key, req.json())


@route("DELETE", "/api/projects/{key}/watch")
def unwatch_project(ctx, req, key):
    inbox.clear_project_watch(ctx, key)
    return 204, None
