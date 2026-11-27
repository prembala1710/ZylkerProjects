"""HTTP handlers for calendar subscription feeds (see services/calendar_feeds.py for what a feed is)."""

from __future__ import annotations

from .app import Raw, route
from .errors import NotFound
from .services import calendar_feeds

SQLITE_MAX_ID = 2**63 - 1


def _feed_id(value):
    """Unknown, non-numeric and out-of-range ids are all the same 404 (a huge id must not reach SQLite)."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= SQLITE_MAX_ID:
        raise NotFound("Calendar feed not found", code="feed_not_found")
    return number


@route("GET", "/api/calendar/feeds")
def list_feeds(ctx, req):
    return calendar_feeds.list_feeds(ctx)


@route("POST", "/api/calendar/feeds")
def create_feed(ctx, req):
    return 201, calendar_feeds.create_feed(ctx, req.json())


@route("POST", "/api/calendar/feeds/{feed_id}/rotate")
def rotate_feed(ctx, req, feed_id):
    return calendar_feeds.rotate_feed(ctx, _feed_id(feed_id))


@route("DELETE", "/api/calendar/feeds/{feed_id}")
def delete_feed(ctx, req, feed_id):
    calendar_feeds.delete_feed(ctx, _feed_id(feed_id))
    return 204, None


@route("GET", "/api/calendar/{token}.ics", public=True)
def read_feed(ctx, req, token):
    """The subscription itself. The address is the credential, so there is no Authorization header to check."""
    body = calendar_feeds.render_feed(ctx, token).encode("utf-8")
    return Raw(body, "text/calendar; charset=utf-8", headers=[("Content-Disposition", 'inline; filename="zylker-calendar.ics"')])
