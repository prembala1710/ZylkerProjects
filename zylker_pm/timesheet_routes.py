"""HTTP handlers for timesheet approval. Thin adapters over ``services.timesheets``.

The fixed paths (``week``, ``missing``, ``summary``, ``submit``, ``approve-many``) are registered before the
``{sheet_id}`` ones so they are never mistaken for an id.
"""

from __future__ import annotations

from .app import route
from .errors import NotFound
from .services import timesheets


def _id(value):
    try:
        return int(value)
    except ValueError:
        raise NotFound("Timesheet not found", code="timesheet_not_found") from None


@route("GET", "/api/timesheets")
def list_timesheets(ctx, req):
    return timesheets.list_sheets(ctx, req.query)


@route("GET", "/api/timesheets/week")
def timesheet_week(ctx, req):
    return timesheets.week_view(ctx, req.query)


@route("GET", "/api/timesheets/missing", role="manager")
def timesheets_missing(ctx, req):
    return timesheets.missing(ctx, req.query)


@route("GET", "/api/timesheets/summary", role="manager")
def timesheets_summary(ctx, req):
    return timesheets.summary(ctx, req.query)


@route("POST", "/api/timesheets/submit", role="member")
def submit_timesheet(ctx, req):
    return 201, timesheets.submit(ctx, req.json())


@route("POST", "/api/timesheets/approve-many", role="manager")
def approve_many(ctx, req):
    return timesheets.approve_many(ctx, req.json())


@route("GET", "/api/timesheets/{sheet_id}")
def get_timesheet(ctx, req, sheet_id):
    return timesheets.get_sheet(ctx, _id(sheet_id))


@route("POST", "/api/timesheets/{sheet_id}/approve", role="manager")
def approve_timesheet(ctx, req, sheet_id):
    return timesheets.approve(ctx, _id(sheet_id), req.json())


@route("POST", "/api/timesheets/{sheet_id}/reject", role="manager")
def reject_timesheet(ctx, req, sheet_id):
    return timesheets.reject(ctx, _id(sheet_id), req.json())


@route("POST", "/api/timesheets/{sheet_id}/withdraw", role="member")
def withdraw_timesheet(ctx, req, sheet_id):
    return timesheets.withdraw(ctx, _id(sheet_id))


@route("POST", "/api/timesheets/{sheet_id}/reopen", role="admin")
def reopen_timesheet(ctx, req, sheet_id):
    return timesheets.reopen(ctx, _id(sheet_id), req.json())
