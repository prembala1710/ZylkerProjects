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
| 429 | Throttled: too many failed sign-ins from this address (`too_many_failures`) or the user's request budget is used up (`rate_limited`). `Retry-After` says how many seconds to wait |
| 500 | Unexpected server error. The body carries a `request_id` to quote to an administrator |

Every response has an `X-Request-Id` header. A valid one sent by a reverse proxy (6-64 letters, digits, `.` `_` `-`) is kept, otherwise one is generated.
The same id is written to the access log and to the audit log, so one request can be followed everywhere.

Collections are returned as `{"items": [...]}`; `GET /api/tasks` also returns `total`, `limit`, `offset`.
Tasks are addressed by key (`WEB-12`), projects by key (`WEB`), users by numeric id or username.

## System and identity

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/health` | public | `{"status": "ok", "version": ...}` - for load balancers |
| `GET /api/meta` | viewer | Enumerations, the workflow transition map, server `today` and timezone, custom-field types and limits |
| `GET /api/me` | viewer | The authenticated user |

## Users

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/users` | viewer | Active users; `?include_inactive=true` (admins only) adds deactivated ones |
| `POST /api/users` | admin | `{username, display_name, email?, role?, weekly_capacity_hours?}` - the response contains the API token, **once** |
| `PATCH /api/users/{user_id}` | viewer | Admins: any field (`role`, `active`, `weekly_capacity_hours`, ...). Everyone else: only their own `display_name` / `email`. The last active admin cannot be demoted or deactivated |
| `POST /api/users/{user_id}/rotate-token` | viewer | Admins for anyone, others for themselves. Invalidates the old token immediately |
| `GET /api/activity` | admin | The raw global activity feed; `?limit=&before=`. Superseded by [`GET /api/audit`](#audit-log), which adds filters, search and security events |

## Projects

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/projects` | viewer | `?status=a,b` `?q=` (matches name, key and the text/URL custom fields) `?member=me` `?include_archived=true`. Archived are hidden by default |
| `POST /api/projects` | manager | `{key, name, description?, status?, owner?, start_date?, due_date?, members?: [username], custom_fields?: {field_key: value}}`. Key: 2-10 chars, letters/digits, starts with a letter. Required [custom fields](#project-custom-fields) must be supplied |
| `GET /api/projects/{key}` | viewer | Includes `members`, `task_counts` and `custom_fields` (`{field_key: value \| null}` with an entry for every defined field) |
| `PATCH /api/projects/{key}` | manager | A project cannot be set to `completed` while it has open tasks. `custom_fields` is merged: only the keys you send change, `null` (or an empty string) clears one |
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

## Project custom fields

Organisation-wide extra fields that every project can carry. Admins define them; managers fill in the values through
`custom_fields` on `POST /api/projects` and `PATCH /api/projects/{key}`. Viewers and members can read them.

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/project-fields` | viewer | All definitions, oldest first |
| `POST /api/project-fields` | admin | `{key, label, type, description?, required?, max_length?, min_value?, max_value?, decimals?}`. At most 20 fields |
| `PATCH /api/project-fields/{field_key}` | admin | Any of `label`, `description`, `required`, `max_length`, `min_value`, `max_value`, `decimals`. `key` and `type` can never change. A limit cannot be tightened past values projects already hold (409 `existing_values_invalid`) |
| `DELETE /api/project-fields/{field_key}` | admin | Also removes the field's value from every project. Requires `?confirm=<field_key>` |

Definition fields: `key` (2-40 chars, lower-case letters, digits and `_`, starting with a letter) is the stable identifier used in
`custom_fields`; `label` (1-50 chars, unique ignoring case) is what people see; `type` is one of:

| Type | Value | Settings |
|---|---|---|
| `text` | One line of text, no control characters | `max_length` 1-2000 (default 255) |
| `number` | A JSON number (not a string) | `min_value`, `max_value` (within +/-1e12), `decimals` 0-3 (default 2). More decimal places than allowed are refused, never rounded |
| `url` | An absolute `http://` or `https://` address, up to 2000 chars | none |

URLs must have a host name (use the punycode form for internationalised names), and must not contain a username or password,
whitespace, backslashes or quotes. Other schemes such as `javascript:` or `data:` are refused.

**Required fields** must be supplied when a project is created and can never be cleared afterwards. Projects that already existed
when a field was made required are left alone until someone edits that field, so adding a required field never breaks old projects.

Every problem is reported at once, keyed `custom_fields.<field_key>` in the 422 `details`. Changes to values appear in the project's
activity feed as `custom_fields.<field_key>: old -> new`; changes to definitions appear in the admin audit trail
(`project_field.created`, `.updated`, `.deleted`).

## Audit log

The admin log monitor. All three endpoints are admin-only; the same data is in the UI under **Admin -> Audit log**.

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/audit` | admin | Events, newest first. Filters below. `?limit=` (1-200, default 50), `?before=<id>` pages back, `?after=<id>` returns what is newer (used by the live view; `gap: true` means more arrived than fit, so reload) |
| `GET /api/audit/summary` | admin | `?days=1..90` (default 7): events per day (in the server's timezone), by category and severity, most active people, failed sign-ins, denials, errors and the addresses with the most failures |
| `GET /api/audit/export.csv` | admin | The events matching the same filters as a CSV (UTF-8 with BOM, up to 50,000 rows; `X-Export-Truncated: true` if cut). Spreadsheet formulas are neutralised. The export is itself recorded |

Filters (all optional, combinable, validated - an unknown value is a 422, never ignored): `q` (searches action, actor, project, task, address,
request id and the event's details) · `actor=<username>|system` · `category=a,b` · `action=task.created,...` · `severity=a,b` ·
`project=<KEY>` (also finds the history of a deleted project) · `ip=` · `request_id=` · `from=` / `to=` (`YYYY-MM-DD`, inclusive, in the server's timezone).

Each event: `id`, `action`, `category`, `severity`, `actor` (`null` for the CLI and system), `project`, `task`, `details`, `ip`, `request_id`, `created_at`.
The first page also returns `total` (exact up to 10,000, then `total_capped: true`).

### What is recorded

Every change to data, by the person who made it, with old and new values - plus the events that are not data changes but that an administrator needs:

| Category | Actions | Severity |
|---|---|---|
| `task`, `comment`, `time`, `milestone`, `project` | `*.created`, `*.updated`, `*.deleted`, `task.status_changed`, `project.member_added`, ... | info (`project.deleted` is notice) |
| `recurrence` | `created`, `updated`, `deleted`, `generated`, `skipped` (see [Recurring tasks](#recurring-tasks)) | info (`deleted` and `skipped` are notice) |
| `project_field` | `created`, `updated`, `deleted` | info (`deleted` is notice) |
| `user` | `created`, `updated` (role, active, ...), `token_rotated` | notice |
| `import` | `completed` | notice |
| `auth` | `failed` (missing or invalid token), `denied` (role too low, not a project member), `session_started`, `throttled`, `unblocked` (see [Rate limiting](#rate-limiting-and-brute-force-protection)) | warning, warning, info, warning, notice |
| `webhook` | `created`, `updated`, `deleted`, `secret_rotated`, `test`, `redelivered`, `disabled` (see [Webhooks](#webhooks)) | info (`deleted` and `secret_rotated` are notice, `disabled` is warning) |
| `data` | `exported` (task CSV export, with row count) | notice |
| `audit` | `exported` (the audit log itself) | notice |
| `system` | `error` (unhandled exception: route and exception *type* only), `search_reindexed` | error, info |

Routine reads are **not** recorded; they would bury the events that matter. Nothing in the log is a token, a request body, a password or an exception
message. User-agent and path text is stripped of control characters and truncated.

**Repeats are merged.** The same `auth.failed`, `auth.denied` or `system.error` from the same actor and address within five minutes updates one row
(`details.count`, `details.last_seen`) instead of adding another, so a flood of bad requests cannot grow the log without bound and the count still says how bad it was.

**Sessions.** The first authenticated request of a user after 30 quiet minutes records `auth.session_started` with the client's user-agent.

**Addresses.** `ip` is the connection's address. Behind a reverse proxy set `ZYLKER_PROXY_HOPS` to the number of proxies so the real client is taken
from `X-Forwarded-For`; with the default of 0 that header is ignored because a client can forge it.

**History survives deletion.** Deleting a project or task no longer erases or re-attributes its history (events keep `project` / `task` keys). Earlier
releases removed a project's history with it, and a task created after a delete could inherit the deleted task's history; the schema upgrade repairs
existing databases.

## Recurring tasks

A rule is a schedule plus a task template. The generator turns every due date into a real task (see [How tasks are generated](#how-tasks-are-generated)).
Anyone signed in can read rules; managers create and change them; only admins can trigger the generator by hand.

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/projects/{key}/recurrences` | viewer | The project's rules, each with its plain-English `schedule`, `status` (`active`, `paused`, `finished`), `next_dates` (up to 5) and `tasks_created` |
| `POST /api/projects/{key}/recurrences` | manager | Schedule and template below. At most 50 rules per project |
| `POST /api/recurrences/preview` | manager | Same schedule fields (`title` optional); `?count=` 1-24, default 10. Returns `{schedule, dates}` and saves nothing |
| `POST /api/recurrences/run` | admin | Run the generator now. `?project=KEY` limits it, `?dry_run=true` reports what would be created without creating it |
| `GET /api/recurrences/{recurrence_id}` | viewer | One rule |
| `PATCH /api/recurrences/{recurrence_id}` | manager | Any subset of the fields, plus `active` (pause / resume) |
| `DELETE /api/recurrences/{recurrence_id}` | manager | Tasks it already created stay |
| `GET /api/recurrences/{recurrence_id}/occurrences` | viewer | Every date the generator handled, newest first (`created` or `skipped` with the reason); `?limit=` 1-200 |

*Schedule:* `frequency` (`daily`, `weekly`, `monthly`; required) · `interval` (1-365, default 1: "every 2 weeks") · `weekdays` (`mon`..`sun`; weekly only,
defaults to the start date's weekday) · `month_day` (1-31, or `-1` for the last day; monthly only, defaults to the start date's day; the 31st means
"the last day" in shorter months) · `start_date` (required, 2000-2100) · `end_date` · `max_occurrences` (1-1000; whichever of the two comes first ends the rule).
Weeks are counted from the Monday of the start week. Settings that do not belong to the chosen frequency are dropped. A schedule that can never fire is a 422.

*Template:* `title` (required; `{date}` becomes the occurrence date, e.g. `Report 2026-10-16`) · `description` · `type` · `priority` · `assignee` (an active project
member) · `estimate_hours` · `labels` · `due_offset_days` (0-365: the task starts on the occurrence date and is due that many days later).

### How tasks are generated

* **Idempotent.** Each date is recorded once; running the generator twice, or two at once, never creates a task twice.
* **No backfill.** A rule that starts in the past, is resumed after a pause, or has its schedule edited begins producing tasks from today.
* **No flood.** If the generator was not run for a while, only the latest 5 missed dates become tasks; older ones are recorded as `skipped` (`catch_up_limit`)
  and an audit event is written.
* **Archived and completed projects** receive nothing; the dates are kept for later (still subject to the limit of 5) and reported as `kept_for_later`.
* **Assignee gone** (left the project or deactivated): the task is created unassigned and the occurrence is annotated `assignee_unavailable`.
* **Reporter:** the person who created the rule, or the project owner if they are no longer active.
* **One bad date does not stop the rest:** it is recorded as skipped with the reason and the run continues.
* **When it runs:** `serve` checks when it starts and then every `ZYLKER_RECURRENCE_CHECK_MINUTES`; `zylker-pm recur-run` does the same from cron.

Audit events (category `recurrence`): `created`, `updated`, `deleted` (notice), `generated` (one per task, attached to it) and `skipped` (notice).

## Rate limiting and brute-force protection

Two limits protect the server; both can be tuned or switched off with environment variables (see the README). Every throttled answer is `429` with a
`Retry-After` header (seconds) and `details: {scope, retry_after}`; `GET /api/health` and the web page are never limited.

* **Failed sign-ins per address** (`ZYLKER_AUTH_FAILURE_LIMIT`, default 10 within five minutes). The failure that reaches the limit is still answered `401`;
  the address is then blocked for 60 seconds, and each repeat doubles the time (2, 4, 8 ... up to an hour; an address that stays quiet for an hour starts
  again at 60 seconds). While blocked, requests that fail to authenticate get `429 too_many_failures` and do **not** extend the block. Requests with a
  *valid* token are still served, so a shared office address is not locked out by one person's typo. A missing token counts as a failure.
* **Request budget per user** (`ZYLKER_RATE_LIMIT_PER_MINUTE`, default 600). A token bucket: the sustained rate, with a burst of a fifth of it (at least 10,
  never more than the rate itself). Each authenticated request costs one - including ones refused with 403 - and carries `X-RateLimit-Limit` (the per-minute
  rate) and `X-RateLimit-Remaining` (requests left in the bucket). When it is empty the answer is `429 rate_limited` and nothing is executed.

The address used is the connection's (or, behind trusted proxies, the client's: see `ZYLKER_PROXY_HOPS`). State is kept in memory, per server process,
and is bounded (at most 10,000 tracked entries); it is lost on restart.

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/security/blocks` | admin | `{settings, blocked: [{ip, retry_after, blocked_until, blocked_since, strikes}]}` - the active blocks, longest first |
| `DELETE /api/security/blocks` | admin | Lift one block with `?ip=`, or all of them without it; also forgets the address's history. Returns `{removed}` |

Audit events (category `auth`): `throttled` (warning) with `reason` `blocked` (a block starts: seconds, strikes, failures), `still_blocked` or `rate_limited`
(repeats merged, with a `count`), and `unblocked` (notice). The audit summary reports `security.throttled`.

## Webhooks

Webhooks tell other systems when something happens: every audit event ([Audit log](#audit-log)) that matches a subscription is POSTed, signed, to the
URL an administrator registered. All endpoints are admin-only.

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/webhooks` | admin | `{items, categories, limits}`: every webhook with its health (`failure_streak`, `last_status`, `last_error`, `disabled_reason`) and delivery counts per status, the categories that can be subscribed to, and the retry limits |
| `POST /api/webhooks` | admin | `{name, url, events, project?, secret?, active?}`. The response contains the signing `secret` - **once**. At most 20 webhooks |
| `GET /api/webhooks/{webhook_id}` | admin | One webhook (never the secret, only `secret_hint`) |
| `PATCH /api/webhooks/{webhook_id}` | admin | Any of `name`, `url`, `events`, `project`, `active`. Switching off cancels what is queued; switching on resets the failure count |
| `DELETE /api/webhooks/{webhook_id}` | admin | Also deletes its delivery log |
| `POST /api/webhooks/{webhook_id}/rotate-secret` | admin | `{id, secret, secret_hint}`: the new secret, shown once; the old one stops signing immediately |
| `POST /api/webhooks/{webhook_id}/test` | admin | Queues a `webhook.ping` for this webhook (202). Delivered like any event, so it shows whether the receiver verifies the signature |
| `GET /api/webhooks/{webhook_id}/deliveries` | admin | The delivery log, newest first. `?status=pending,failed` `?event=task.created` `?limit=` (1-100, default 25) `?before=<id>` |
| `GET /api/webhooks/{webhook_id}/deliveries/{delivery_id}` | admin | One delivery with its `payload` and the `attempt_log` (time, HTTP status, error code, milliseconds per attempt) |
| `POST /api/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver` | admin | Sends that event again as a new delivery (`redelivery_of` points back); the original stays in the log. 409 `webhook_inactive` while the webhook is off |

*Definition:* `name` (1-60 chars, unique ignoring case) · `url` (see below) · `events`: a non-empty list (at most 30) of `*`, `category.*` or `category.action`
using the categories from [What is recorded](#what-is-recorded) (`task.created`, `project.*`, `auth.*`, ...) - webhook events themselves cannot be subscribed to ·
`project`: a project key to receive only that project's events (`null` for all) · `secret`: optional, 16-200 ASCII characters; generated (`whsec_...`) when omitted.

*URLs.* Must be `https://` with a host name, no user name or password, no `#fragment`. Literal private addresses (`10.x`, `192.168.x`, `127.x`, `169.254.169.254`, `::1`, ...) and names such
as `localhost` or `*.local` are refused when the webhook is saved. **Because a name can be re-pointed afterwards, the address is checked again at delivery:** the server resolves the
name itself, refuses the delivery (`blocked_address`) if *any* answer is not a public address, and connects to the address it checked. Redirects are not followed and response bodies are
not kept. Installations whose receivers are on the internal network set `ZYLKER_WEBHOOK_ALLOW_PRIVATE=1`, which also permits plain `http`.

### What is sent

A `POST` with `Content-Type: application/json`:

```json
{"delivery_id": 41, "attempt": 1, "webhook": {"id": 2, "name": "Ops channel"},
 "event": "task.status_changed", "event_id": 1873, "occurred_at": "2026-10-16T09:00:00Z",
 "project": "WEB", "task": "WEB-12", "actor": {"username": "alice", "display_name": "Alice"},
 "details": {"status": ["todo", "in_progress"]}}
```

`event_id` is the audit event's id (`null` for a ping); `details` is exactly what the audit log holds for the event (`{"truncated": true}` if it exceeded 64 KB). Headers:

| Header | Meaning |
|---|---|
| `X-Zylker-Event` | The event name, e.g. `task.created` |
| `X-Zylker-Delivery` | The delivery id. **The same for every attempt** - use it to ignore repeats |
| `X-Zylker-Attempt` | 1 for the first try, up to 6 |
| `X-Zylker-Timestamp` | Seconds since the epoch when this attempt was signed |
| `X-Zylker-Signature` | `v1=` + hex HMAC-SHA256 of `"<timestamp>.<raw body>"` with the webhook's secret |

*Verifying* (Python; any language with HMAC-SHA256 works the same way): compute the HMAC over the **raw** request body, compare in constant time, and refuse timestamps more than five minutes
away from your clock so a captured request cannot be replayed.

```python
import hmac, time
from hashlib import sha256

def verify(secret, headers, raw_body, tolerance=300):
    ts = headers["X-Zylker-Timestamp"]
    if abs(time.time() - int(ts)) > tolerance:
        return False
    expected = "v1=" + hmac.new(secret.encode(), ts.encode() + b"." + raw_body, sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-Zylker-Signature"])
```

### Delivery guarantees

* **Queued with the change.** The delivery row is written in the same database transaction as the audit event, so a rolled-back request announces nothing and a crash cannot lose an announcement.
* **At least once, not in order.** Any 2xx answer is success; anything else (including a timeout after 10 seconds) is retried after 1 minute, 5 minutes, 30 minutes, 2 hours and 6 hours, then the delivery is `failed`.
  Deliveries to one webhook may arrive out of order, and an attempt that timed out may still have been processed: deduplicate on `X-Zylker-Delivery` and order by `event_id`.
* **Statuses:** `pending` (waiting for its first or next attempt), `sending`, `delivered`, `failed` (gave up, or `queue_full`), `cancelled` (the webhook was switched off or disabled).
* **A failing receiver is switched off.** After 15 failed attempts in a row, or at once on `410 Gone`, the webhook is disabled (`disabled_reason`: `too_many_failures` or `gone`), its queue is cancelled and an
  `webhook.disabled` warning is written to the audit log. Switch it on again with `PATCH {"active": true}`.
* **Bounded.** At most 1,000 undelivered events per webhook; further events are recorded as `failed` with error `queue_full`. Finished deliveries are deleted after 30 days.
* **Error codes** in `error`: `http_<status>`, `timeout`, `connection_refused`, `connection_failed`, `dns_failure`, `blocked_address`, `invalid_url`, `tls_certificate`, `tls_error`, `protocol_error`, `network_error`.
* **Who delivers.** `serve` delivers every `ZYLKER_WEBHOOK_INTERVAL_SECONDS` (default 2); without it run `zylker-pm webhook-run` from cron. Several processes may run at once; each delivery is claimed by exactly one.

Audit events (category `webhook`): `created`, `updated` (with the changes; URLs appear as host names only), `deleted` (notice), `secret_rotated` (notice), `test`, `redelivered`, and `disabled` (warning, written by the system).
The secret and the URL's path and query never appear in the audit log.

## Search

One endpoint searches tasks, comments and projects together, ranked by relevance, with highlighted snippets. Everyone who is signed in can search (every signed-in user can already read every project).

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/search` | viewer | `?q=` (required, at most 200 characters) `?limit=` (1-50, default 20) `?offset=` (0-1000). See the query language below |
| `GET /api/search/status` | admin | `{engine, fts5_available, documents, indexed, problems, in_sync}`: which engine the database uses and, for FTS5, whether the index matches the data (`problems` counts `missing` and `orphaned` rows per kind) |
| `POST /api/search/reindex` | admin | Rebuilds the index from the data (a repair tool; triggers normally keep it exact). 409 `no_search_index` on the substring engine. Recorded as `system.search_reindexed` |

### Query language

| You type | It means |
|---|---|
| `login crash` | every word must match (words are stemmed on FTS5: `crashes` finds `crash`; accents are ignored) |
| `"login page"` | the exact phrase |
| `log*` | a prefix |
| `login OR signin` | either word (`OR` in capitals; it joins the words on both sides) |
| `login -safari` / `-"bad phrase"` | exclude a word or phrase |
| `WEB-12` | a task key goes straight to that task (marked `exact_key: true`, always first) |
| `status:open` and other filters | see below; filters alone list matching tasks, most recently changed first, with `score: null` |

Words never act as search syntax: `AND`, `NEAR`, quotes, brackets and `*` typed inside a word are plain text. A query needs at least one word or one filter, and at most 16 words and phrases (otherwise 422).

Filters (`name:value`, several values with commas mean "any of"; different filters combine with "and"; a filter name that is not in this list is just text):

| Filter | Values | Notes |
|---|---|---|
| `project:` | project keys | `project:web,ops` |
| `in:` | `task`, `comment`, `project` | restricts what kind of result you get (plurals accepted) |
| `status:` | `todo`, `in_progress`, `in_review`, `blocked`, `done`, `cancelled` | |
| `is:` | `open`, `closed` | shorthand for the open or closed statuses; combines with `status:` |
| `priority:` | `low`, `medium`, `high`, `critical` | |
| `type:` | `task`, `bug`, `story`, `epic` | |
| `assignee:` | user name, `me`, `none` | |
| `reporter:` | user name, `me` | |
| `label:` | label names | |

Filters about a task (`status`, `priority`, `type`, `assignee`, `reporter`, `label`) leave projects out of the results, and a comment follows the filters of the task it belongs to. A filter value that is not valid is a 422 whose `details.q` lists every problem at once; negating a filter (`-status:done`) is refused.

### Result

`{query, engine, interpreted, total, total_capped, limit, offset, items}`. `interpreted` echoes how the query was understood (`all_of`: a list of groups, each an "or" of words; `none_of`; `filters` after normalisation). `total` is exact up to 1,000, then `total_capped: true`. Each item has `type` (`task`, `comment`, `project`), `key` (the task key, or the project key), `title`, `project`, `score` (higher is better), `matched_in` (a subset of `title`, `labels`, `body`) and `snippet`; tasks add `status`, `priority`, `assignee`, comments add `comment_id`, `author`, `created_at`, and tasks and projects add `title_match`.

Highlights are **segments**, never markup: `snippet` and `title_match` are lists of `{"text": "...", "match": true|false}`; show them as text and emphasise the matching ones.

### Ranking and engines

* **FTS5 (normal).** BM25 relevance with the title weighted above labels above text. Snippets come from the index.
* **Substring ("like").** Used automatically when the SQLite behind Python has no FTS5, and recorded per database (`engine` in the response and in `/api/search/status`). Case-insensitive substring matching straight on the tables: the same filters, the same result shape, no stemming, ranking by where the words were found (title, then labels, then text). `zylker-pm search-rebuild` moves such a database onto FTS5 once it is available.
* **Always in step.** On FTS5 the index is maintained by database triggers, so every way of changing data (API, CSV import, recurring tasks, the command line, even raw SQL) is covered, inside the same transaction: a rolled-back change never appears in search, and deleting a task, a project or a comment removes its entries.
* A database that uses FTS5 is refused on a Python without it, with a message saying so, rather than failing on the first write.

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
