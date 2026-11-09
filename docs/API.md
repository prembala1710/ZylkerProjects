# API reference

Base path `/api`. JSON in, JSON out (`Content-Type: application/json`), except CSV import/export.
Every endpoint except `GET /api/health` needs `Authorization: Bearer <token>`.

*Role* is the minimum global role (`viewer` < `member` < `manager` < `admin`). Task-level writes by a `member`
additionally require membership of the project (managers and admins can write anywhere). Archived projects are
read-only for everyone until their status is changed.

Errors always look like `{"error": {"code": "...", "message": "...", "details": {...}}}`:

| Status | Meaning |
|---|---|
| 400 | Malformed JSON / key / header |
| 401 | Missing, invalid or revoked token |
| 403 | Role too low, or not a member of the project (`insufficient_role`, `not_project_member`, `not_author`) |
| 404 | Unknown route, project, task, ... |
| 409 | A business rule blocked the change (`invalid_transition`, `blocked_by_open_tasks`, `open_subtasks`, `dependency_cycle`, `project_archived`, ...) |
| 413 / 415 | Body too large / wrong content type |
| 422 | Field validation failed - `details` maps each field to its problem |

Collections are returned as `{"items": [...]}`; `GET /api/tasks` also returns `total`, `limit`, `offset`.
Tasks are addressed by key (`WEB-12`), projects by key (`WEB`), users by numeric id or username.

## System and identity

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/health` | public | `{"status": "ok", "version": ...}` - for load balancers |
| `GET /api/meta` | viewer | Enumerations, the workflow transition map, server `today` and timezone |
| `GET /api/me` | viewer | The authenticated user |

## Users

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/users` | viewer | Active users; `?include_inactive=true` (admins only) adds deactivated ones |
| `POST /api/users` | admin | `{username, display_name, email?, role?, weekly_capacity_hours?}` - the response contains the API token, **once** |
| `PATCH /api/users/{user_id}` | viewer | Admins: any field (`role`, `active`, `weekly_capacity_hours`, ...). Everyone else: only their own `display_name` / `email`. The last active admin cannot be demoted or deactivated |
| `POST /api/users/{user_id}/rotate-token` | viewer | Admins for anyone, others for themselves. Invalidates the old token immediately |
| `GET /api/activity` | admin | Global audit trail (user/role changes, token rotations); `?limit=&before=` |

## Projects

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/projects` | viewer | `?status=a,b` `?q=` `?member=me` `?include_archived=true`. Archived are hidden by default |
| `POST /api/projects` | manager | `{key, name, description?, status?, owner?, start_date?, due_date?, members?: [username]}`. Key: 2-10 chars, letters/digits, starts with a letter |
| `GET /api/projects/{key}` | viewer | Includes `members` and `task_counts` |
| `PATCH /api/projects/{key}` | manager | A project cannot be set to `completed` while it has open tasks |
| `DELETE /api/projects/{key}` | admin | Permanent. Requires `?confirm=<KEY>` |
| `POST /api/projects/{key}/members` | manager | `{username}`. Idempotent |
| `DELETE /api/projects/{key}/members/{username}` | manager | Refused while the member has open tasks, or is the owner |
| `GET /api/projects/{key}/summary` | viewer | Counts, progress, hours, **health** (with the reasons), next milestone |
| `GET /api/projects/{key}/board` | viewer | Tasks grouped by status column; `?closed_days=14` hides older closed tasks (0 = all) |
| `GET /api/projects/{key}/critical-path` | viewer | CPM over open, dependency-linked tasks: path, total effort, slack per task |
| `GET /api/projects/{key}/burndown` | viewer | `?metric=tasks\|hours` `?from=` `?to=` (YYYY-MM-DD, max 367 days). Daily remaining / scope / completed / ideal |
| `GET /api/projects/{key}/activity` | viewer | `?limit=` (max 200) `?before=<activity id>`; `next_before` pages backwards |
| `GET /api/projects/{key}/milestones` | viewer | With progress and `achieved` / `overdue` / `open` status |
| `POST /api/projects/{key}/milestones` | manager | `{name, due_date, description?}` |
| `GET /api/projects/{key}/export.csv` | viewer | All tasks as CSV (UTF-8 with BOM). Cells that could be read as spreadsheet formulas are prefixed with `'` |
| `POST /api/projects/{key}/import` | manager | Body is CSV (`Content-Type: text/csv`, up to 5 MB / 5,000 rows). `?dry_run=true` validates without saving, `?add_missing_members=true` |
| `POST /api/projects/{key}/tasks` | member | See *Task fields* below |

## Tasks

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/tasks` | viewer | Filters and sorting below |
| `GET /api/tasks/{key}` | viewer | Adds `blocked_by`, `blocks`, `subtask_list`, `allowed_transitions`, `project_status` |
| `PATCH /api/tasks/{key}` | member | Any subset of the task fields; status changes are checked against the workflow rules |
| `DELETE /api/tasks/{key}` | manager | Refused when the task has subtasks unless `?cascade=true` |
| `GET /api/tasks/{key}/activity` | viewer | Field-level history: who changed what, from and to |
| `POST /api/tasks/{key}/dependencies` | member | `{"blocked_by": "WEB-1"}` or `{"blocks": "WEB-3"}`. Same project only; cycles are refused with the loop in `details.cycle` |
| `DELETE /api/tasks/{key}/dependencies/{other}` | member | Removes the link between the two tasks in either direction |
| `GET /api/tasks/{key}/comments` | viewer | |
| `POST /api/tasks/{key}/comments` | member | `{body}` (<= 10,000 chars). `@username` mentions notify Cliq |
| `PATCH /api/comments/{comment_id}` | member | Author only |
| `DELETE /api/comments/{comment_id}` | member | Author, or a manager |
| `GET /api/tasks/{key}/time` | viewer | Time entries |
| `POST /api/tasks/{key}/time` | member | `{hours (0.01-24), work_date?, note?}`; logged for the calling user |
| `DELETE /api/time/{entry_id}` | member | Owner of the entry, or a manager |

### Task fields

`title` (required, <= 200) · `description` (<= 20,000) · `type` (`task` `bug` `story` `epic`) · `status`
(`todo` `in_progress` `in_review` `blocked` `done` `cancelled`) · `priority` (`low` `medium` `high` `critical`) ·
`assignee` (username or `null`; must be an active project member) · `parent` (task key or `null`) · `milestone`
(id or `null`) · `start_date` / `due_date` (`YYYY-MM-DD`, due must not precede start) · `estimate_hours` (0-10,000) ·
`labels` (<= 20 of `[a-z0-9_./-]`, <= 30 chars) · `external_ref` (<= 100, e.g. the Jira key it came from).
Unknown fields are rejected, not ignored.

### `GET /api/tasks` query parameters

`project=A,B` · `status=` · `priority=` · `type=` (comma lists) · `open=true` / `closed=true` ·
`assignee=` / `reporter=` (usernames, `me`, and for assignee `none`) · `label=` (any of) · `milestone=<id>|none` ·
`parent=<key>|none` · `leaf=true` · `q=` (title, description or exact key) · `due_before=` / `due_after=` ·
`overdue=true` · `external_ref=` · `sort=` (`key number title status priority due_date start_date created_at updated_at
estimate_hours assignee`, comma-separated, `-` prefix for descending; dates sort empty-last) · `limit=` (default 50, max
200) · `offset=`. Unknown values are rejected with 422 rather than silently ignored.

## Milestones

| Endpoint | Role | Notes |
|---|---|---|
| `PATCH /api/milestones/{milestone_id}` | manager | `{name?, due_date?, description?}` |
| `DELETE /api/milestones/{milestone_id}` | manager | Linked tasks keep existing and lose the link |

## Reports

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/reports/portfolio` | viewer | Every non-archived project with progress, health, overdue, next milestone - worst first |
| `GET /api/reports/workload` | viewer | `?weeks=1..12` (default 2) `?project=`. Per person: scheduled hours vs capacity, utilisation, overallocation |
| `GET /api/reports/timesheet` | viewer | `?from=&to=&project=&user=`. Members and viewers only ever see their own time; managers see everyone |
| `GET /api/reports/digest` | viewer | `?days=0..30` (default 3) `?project=`. Overdue and due-soon tasks grouped by assignee |

## Webhooks

Outbound webhooks call another system when something changes here. **Every endpoint in this section is admin-only.** Events are
the audit-trail entries, queued in the same database transaction as the change, and sent by a background worker
(`zylker-pm serve` runs one - see the README for other servers).

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/webhooks/events` | admin | The event catalogue: `events`, `groups`, `patterns` (everything a subscription may contain), `never_delivered` |
| `GET /api/webhooks` | admin | `{items, worker}`. `worker` is `{running, last_seen}`: whether a delivery worker has checked in during the last 30 s |
| `POST /api/webhooks` | admin | `{name, url, events, projects?, active?}`. 201 with the webhook **and its `secret`, which is shown only in this response** |
| `GET /api/webhooks/{webhook_id}` | admin | One webhook. Never contains the secret, only `secret_hint` (its last four characters) |
| `PATCH /api/webhooks/{webhook_id}` | admin | Any subset of `name`, `url`, `events`, `projects`, `active`. Pausing (`active: false`) cancels queued deliveries |
| `DELETE /api/webhooks/{webhook_id}` | admin | Deletes the webhook and its delivery log |
| `POST /api/webhooks/{webhook_id}/rotate-secret` | admin | Issues a new secret (returned once). The next attempt - including retries of queued deliveries - is signed with it |
| `POST /api/webhooks/{webhook_id}/test` | admin | 202 with a queued `webhook.ping` delivery. Tried once, and sent even while the webhook is paused |
| `GET /api/webhooks/{webhook_id}/deliveries` | admin | Newest first: `?status=` (`pending`, `sending`, `succeeded`, `failed`), `?limit=` (1-100, default 25), `?before=<delivery id>`. Returns `{items, next_before}` |
| `GET /api/webhooks/{webhook_id}/deliveries/{delivery_id}` | admin | One delivery, including its `payload` |
| `POST /api/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver` | admin | 202 with a new delivery carrying the same event id and payload. Only for finished deliveries; refused while the webhook is paused |

### Webhook fields

`name` (required, <= 80) · `url` (required, <= 2,000; `http` or `https`; no user name or password in it; loopback, private and other
non-public addresses are refused unless `ZYLKER_WEBHOOK_ALLOW_PRIVATE` is set) · `events` (required, a list of `*`, `<group>.*` or
exact names from the catalogue below) · `projects` (project keys; empty or `null` means every project, otherwise only events that
belong to one of those projects are sent) · `active` (default `true`). Unknown fields are rejected: there is deliberately no way to
choose your own secret. At most 50 webhooks exist.

A webhook response also carries `pending_deliveries` and `health` (`last_delivery_at`, `last_status`, `consecutive_failures`, which
counts failed *attempts* in a row and resets on the next success).

### Events

| Group | Events |
|---|---|
| `project` | `project.created` `project.updated` `project.deleted` `project.member_added` `project.member_removed` |
| `milestone` | `milestone.created` `milestone.updated` `milestone.deleted` |
| `task` | `task.created` `task.updated` `task.status_changed` `task.deleted` `task.dependency_added` `task.dependency_removed` |
| `comment` | `comment.added` `comment.edited` `comment.deleted` |
| `time` | `time.logged` `time.deleted` |
| `import` | `import.completed` (one event per CSV import, not one per row) |

A status change is `task.status_changed`; `task.updated` is every other change. `<group>.*` also covers actions that later versions
add to the group, and `*` covers everything. `user.*` (user administration) and `webhook.*` (webhook management) are never
delivered. `webhook.ping` is sent only by the test endpoint and cannot be subscribed to.

### Delivery format

Each event is one `POST` with `Content-Type: application/json` and these headers:

| Header | Value |
|---|---|
| `X-Zylker-Event` | The event name, e.g. `task.created` |
| `X-Zylker-Delivery` | The delivery id (differs for every delivery, including redeliveries) |
| `X-Zylker-Attempt` | 1 for the first try, 2 for the first retry, ... |
| `X-Zylker-Timestamp` | Unix seconds when this attempt was made |
| `X-Zylker-Signature` | `v1=` followed by the hex HMAC-SHA256, described below |
| `User-Agent` | `ZylkerProjects/<version>` |

The body is compact JSON with sorted keys: `id` (the event id, e.g. `evt_412` - identical for every delivery of the same event, so
use it to de-duplicate), `event`, `created_at`, `project` (`key`, `name`, and `url` when `ZYLKER_PUBLIC_URL` is set - not for a
deleted project; `null` for events that belong to no project), `task` (`key`, `title`, `status`, `priority`, `type`, `assignee`, `due_date`, `url`; only `key`
and `title` once the task is deleted; `null` for events without a task), `actor` (`username`, `display_name`; `null` for
system actions) and `details` (the audit entry's details, such as `changes`). Task data is the state *after* the change. Comment
text and descriptions are not included beyond what the audit entry already keeps (descriptions are truncated to 200 characters in
`changes`): fetch more with the REST API.

```json
{"actor":{"display_name":"Alice Morgan","username":"alice"},"created_at":"2026-10-16T09:00:00Z","details":{"changes":{"status":["in_progress","in_review"]}},"event":"task.status_changed","id":"evt_412","project":{"key":"WEB","name":"Website Redesign"},"task":{"assignee":"bob","due_date":"2026-10-30","key":"WEB-5","priority":"high","status":"in_review","title":"Homepage layout","type":"task"}}
```

### Verifying a delivery

The signature is the hex HMAC-SHA256, keyed with the webhook secret, of the timestamp header, a dot, and the **raw** request body.
Check it before trusting anything in the payload, compare in constant time, and reject timestamps older than a few minutes so a
captured request cannot be replayed. The header may hold several comma-separated `v1=` values; accept the request if any matches.

```python
import hashlib
import hmac
import time


def verify(secret: str, headers: dict, body: bytes, tolerance: int = 300) -> bool:
    """`body` must be the request bytes exactly as received, before any JSON parsing."""
    try:
        timestamp = int(headers["X-Zylker-Timestamp"])
    except (KeyError, ValueError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = "v1=" + hmac.new(secret.encode(), b"%d." % timestamp + body, hashlib.sha256).hexdigest()
    received = headers.get("X-Zylker-Signature", "").split(",")
    return any(hmac.compare_digest(expected, part.strip()) for part in received)
```

```javascript
const crypto = require('crypto');

// headers: Node lower-cases header names. rawBody: a Buffer of the bytes exactly as received.
function verify(secret, headers, rawBody, toleranceSeconds = 300) {
  const timestamp = Number(headers['x-zylker-timestamp']);
  if (!Number.isInteger(timestamp) || Math.abs(Date.now() / 1000 - timestamp) > toleranceSeconds) return false;
  const expected = Buffer.from('v1=' + crypto.createHmac('sha256', secret).update(`${timestamp}.`).update(rawBody).digest('hex'));
  return String(headers['x-zylker-signature'] || '').split(',').some((part) => {
    const given = Buffer.from(part.trim());
    return given.length === expected.length && crypto.timingSafeEqual(given, expected);
  });
}
```

### Retries and the delivery log

A delivery succeeds on any `2xx` answer. Everything else - another status, a timeout, a DNS, TLS or connection failure - is a
failed attempt and is retried: the first attempt is immediate, then after 30 s, 2 min, 10 min, 1 h and 6 h; the sixth failure is
final (about seven hours after the first). Redirects are not followed, so a `3xx` is a failure. Each attempt is bounded by
`ZYLKER_WEBHOOK_TIMEOUT`.

Delivery is **at-least-once and unordered**: a worker that dies after sending but before recording the result causes a repeat, and
deliveries run concurrently. De-duplicate on the payload `id` and order by `created_at` if it matters.

A delivery has `status` (`pending`, `sending`, `succeeded`, `failed`), `attempts` / `max_attempts`, `next_attempt_at`,
`response_status`, `duration_ms`, a short `response_excerpt` (at most 300 printable characters of the answer - never more is
stored) and `error`, which is `http_<status>` or one of `timeout`, `connection_failed`, `dns_error`, `tls_error`,
`blocked_address`, `protocol_error`, `invalid_request`, `internal_error`, `cancelled` (the webhook was paused first) or
`lease_expired` (a worker died during the final attempt). Only the latest attempt is described. The log keeps the newest 500
finished deliveries per webhook; at most 5,000 undelivered events are queued per webhook, and beyond that new events are
dropped with a warning in the server log rather than slowing requests down.

### Security

- The secret is 256 random bits shown once; it must be stored readable to sign with, so protect database files and backups.
- Admin changes to webhooks are in the audit trail without the URL (it often contains a key) or the secret; the URL is never
  written to logs either.
- Destinations are checked when saved and again at delivery time against the addresses the name resolves to (see the README),
  and the connection is made to the address that was checked. Payloads are signed, not encrypted: use `https`.
