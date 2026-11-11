# API reference

Base path `/api`. JSON in, JSON out (`Content-Type: application/json`), except CSV and Microsoft Project import/export and attachment upload/download.
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
| `GET /api/meta` | viewer | Enumerations, the workflow transition map, server `today` and timezone, custom-field types and limits, dependency and link types, checklist / watcher / bulk limits |
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
| `GET /api/projects/{key}/dependencies` | viewer | Every dependency inside the project in one answer: both tasks, type, lag, whether it is holding the blocked task back, and the date hint - see [Listing a project's dependencies](#listing-a-projects-dependencies). This is what the timeline draws its arrows from |
| `GET /api/projects/{key}/baselines` | viewer | The project's baselines, newest first, each with what it recorded; `limits.max_baselines` - see [Baselines](#baselines) |
| `POST /api/projects/{key}/baselines` | manager | `{name?, note?}`: freezes the plan as it is now (`201`). The name defaults to `Baseline N`, is unique within the project (case-insensitive) and at most 60 characters; the note is at most 500. At most 10 per project |
| `GET /api/projects/{key}/baselines/{baseline_id}` | viewer | The baseline and how the project compares with it today: task by task, milestones and the whole project - see [Baselines](#baselines) |
| `DELETE /api/projects/{key}/baselines/{baseline_id}` | manager | Deletes the baseline (`204`); tasks and milestones are not touched |
| `GET /api/projects/{key}/burndown` | viewer | `?metric=tasks\|hours` `?from=` `?to=` (YYYY-MM-DD, max 367 days). Daily remaining / scope / completed / ideal |
| `GET /api/projects/{key}/activity` | viewer | `?limit=` (max 200) `?before=<activity id>`; `next_before` pages backwards |
| `GET /api/projects/{key}/milestones` | viewer | With progress and `achieved` / `overdue` / `open` status |
| `POST /api/projects/{key}/milestones` | manager | `{name, due_date, description?}` |
| `GET /api/projects/{key}/export.csv` | viewer | All tasks as CSV (UTF-8 with BOM). Cells that could be read as spreadsheet formulas are prefixed with `'` |
| `POST /api/projects/{key}/import` | manager | Body is CSV (`Content-Type: text/csv`, up to 5 MB / 5,000 rows). `?dry_run=true` validates without saving, `?add_missing_members=true` |
| `GET /api/projects/{key}/export/msproject` | viewer | The project as a Microsoft Project XML file (`application/xml`, downloaded as `KEY-msproject.xml`); see [Microsoft Project](#microsoft-project) |
| `POST /api/projects/{key}/import/msproject` | manager | Body is a Microsoft Project XML file (`Content-Type: application/xml` or `text/xml`, up to 5 MB / 5,000 tasks). `?dry_run=true` validates without saving, `?add_missing_members=true`; see [Microsoft Project](#microsoft-project) |
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

## Tasks

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/tasks` | viewer | Filters and sorting below |
| `GET /api/tasks/{key}` | viewer | Adds `blocked_by`, `blocks` (with dependency `type`, `lag_days` and a date `schedule` hint), `links`, `checklist_items`, `watchers`, `watching`, `subtask_list`, `allowed_transitions`, `project_status` |
| `POST /api/tasks/bulk` | member | Apply one set of changes to up to 100 tasks - see [Bulk actions](#bulk-actions) |
| `PATCH /api/tasks/{key}` | member | Any subset of the task fields; status changes are checked against the workflow rules |
| `DELETE /api/tasks/{key}` | manager | Refused when the task has subtasks unless `?cascade=true` |
| `GET /api/tasks/{key}/activity` | viewer | Field-level history: who changed what, from and to |
| `POST /api/tasks/{key}/dependencies` | member | `{"blocked_by": "WEB-1"}` or `{"blocks": "WEB-3"}`, plus optional `type` and `lag_days` - see [Dependency types](#dependency-types-and-lag). Same project only; cycles are refused with the loop in `details.cycle` |
| `PATCH /api/tasks/{key}/dependencies/{other}` | member | Change the `type` and/or `lag_days` of the dependency between the two tasks (either direction). A type change that would create a deadlock is refused |
| `DELETE /api/tasks/{key}/dependencies/{other}` | member | Removes the link between the two tasks in either direction |
| `POST /api/tasks/{key}/links` | member | `{"type": "relates_to", "task": "WEB-7"}` - see [Task links](#task-links). Returns the updated task (201). Needs write access to both tasks' projects |
| `DELETE /api/tasks/{key}/links/{other}` | member | Removes the link between the two tasks, whichever way round it was made. Returns the updated task |
| `GET /api/tasks/{key}/checklist` | viewer | The task's checklist items in order - see [Checklists](#checklists) |
| `POST /api/tasks/{key}/checklist` | member | `{text, done?, position?}`. At most 50 items per task |
| `PATCH /api/checklist/{item_id}` | member | Any of `text`, `done`, `position` (1 = top). Ticking records who and when |
| `DELETE /api/checklist/{item_id}` | member | Removes the item; the rest close up |
| `GET /api/tasks/{key}/watchers` | viewer | `{items: [users], watching: bool}` - see [Watchers](#watchers) |
| `POST /api/tasks/{key}/watchers` | viewer | `{}` follows the task yourself; `{"username": "bob"}` adds someone else (needs write access to the project). Idempotent |
| `DELETE /api/tasks/{key}/watchers/{username}` | viewer | Stop following. Anyone may remove themselves; removing someone else needs write access |
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
`assignee=` / `reporter=` (usernames, `me`, and for assignee `none`) · `watcher=` (usernames or `me`; with several, the task must be watched by all of them) · `label=` (any of) · `milestone=<id>|none` ·
`parent=<key>|none` · `leaf=true` · `q=` (title, description or exact key) · `due_before=` / `due_after=` ·
`overdue=true` · `external_ref=` · `sort=` (`key number title status priority due_date start_date created_at updated_at
estimate_hours assignee`, comma-separated, `-` prefix for descending; dates sort empty-last) · `limit=` (default 50, max
200) · `offset=`. Unknown values are rejected with 422 rather than silently ignored.

### Dependency types and lag

A dependency says that the *blocked* task has to wait for the *blocker*. The `type` decides what it waits for:

| `type` | The blocked task cannot... | until the blocker... |
|---|---|---|
| `finish_to_start` (default) | start (move to in progress, in review or done) | is finished (done or cancelled) |
| `start_to_start` | start (move to in progress, in review or done) | has started - any status except to do and blocked (a task flagged *blocked* counts as not started) |
| `finish_to_finish` | finish (move to done) | is finished. It may start and go to review earlier |

A cancelled or done blocker never holds anything up. The refusal is a `409 blocked_by_open_tasks`; `details.blockers` lists the keys and
`details.links` adds each one's type. `blocked_by_open` on a task counts the dependencies that are holding it back *right now*, and project health
counts waiting the same way.

`lag_days` (integer, -365..365, default 0) is a lag when positive and a lead when negative. It is **informational about dates**: it never moves a
task and never blocks a status change. Each entry in `blocked_by` / `blocks` carries `schedule`, or `null` when the blocker lacks the date it hangs on:

```json
{"key": "WEB-1", "type": "finish_to_start", "lag_days": 2, "schedule": {"applies_to": "start", "not_before": "2026-10-22", "conflict": true}}
```

`not_before` is the earliest date the blocked task's start (or, for `finish_to_finish`, its due date) should have: the blocker's due date
(`finish_to_start`, `finish_to_finish`) or start date (`start_to_start`) plus the lag. Calendar days, and the same day is allowed. `conflict` is true when the
blocked task's planned date is earlier. The critical path only follows `finish_to_start` links (it works in effort hours, not dates).

Deadlock detection understands all three types: it treats every task as a *start* and a *finish* event, so "B starts after A starts" together with "A finishes
after B finishes" is accepted, while loops such as "A finishes before B starts, B starts before A starts" are refused with the loop in `details.cycle`.
Subtasks still have to finish before their parent.

### Listing a project's dependencies

`GET /api/projects/{key}/dependencies` (viewer) returns every dependency of one project, ordered by blocker number and then blocked number. It is read-only and writes nothing to the audit trail.

```json
{
  "project": "WEB",
  "truncated": false,
  "items": [
    {
      "blocker": {"key": "WEB-1", "title": "Design", "status": "in_progress", "start_date": "2026-10-05", "due_date": "2026-10-20"},
      "blocked": {"key": "WEB-2", "title": "Build", "status": "todo", "start_date": "2026-10-12", "due_date": "2026-10-30"},
      "type": "finish_to_start",
      "lag_days": 0,
      "holding": true,
      "schedule": {"applies_to": "start", "not_before": "2026-10-20", "conflict": true}
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `blocker`, `blocked` | The two tasks: `key`, `title`, `status`, `start_date`, `due_date` (dates are `null` when not set) |
| `type`, `lag_days` | As in [Dependency types and lag](#dependency-types-and-lag) |
| `holding` | True while this link holds the blocked task back right now: the blocked task is still open and the blocker has not reached what the type waits for (not finished for `finish_to_start` and `finish_to_finish`, not started for `start_to_start`). It is the rule behind `blocked_by_open`, so for an open task the number of `holding` links pointing at it equals its `blocked_by_open` |
| `schedule` | The same hint `blocked_by` / `blocks` carry on a task: `null` when the blocker lacks the date the link hangs on, otherwise `applies_to`, `not_before` and `conflict` |

`truncated` is true when the project has more than 5,000 dependencies; the first 5,000 are returned. An unknown project is a `404 project_not_found`.

### Task links

Informational links between tasks - for navigation only. They never block work, never take part in deadlock detection and never change health. They may join tasks of
different projects. A task pair can have **one** link, whichever way round (`409 link_exists`; remove it first to change it).

`type` is the relationship *as seen from the task in the URL*: `relates_to` (same from both ends), `duplicates` / `duplicated_by`, `cloned_from` / `cloned_to`. Each task's
`links` shows the same link in its own words:

```json
{"links": [{"type": "duplicated_by", "task": {"key": "WEB-2", "title": "...", "status": "todo"}}]}
```

### Checklists

Lightweight items inside a task: text plus a tick box, in an order you can change. Items have no assignee, dates or status, and **never gate the workflow** - unticked
items do not stop a task closing and ticking all of them does not close it (use subtasks for work that needs an owner or a due date). Item text is 1-200 characters.
An item is `{id, task, position, text, done, done_by, done_at, created_at}`; tasks list `checklist: {total, done}` and the detail view adds `checklist_items`. Checklists follow
the task's project: read by anyone, changed by members of the project (and managers), read-only once the project is archived.

### Watchers

People who follow a task without being its assignee. Anyone signed in - even a viewer - may follow or unfollow *themselves*; adding someone else needs write access to the
project. Nobody can start following a task in an archived project (but anyone can stop). Up to 50 per task; deactivated users drop out. Tasks list `watcher_count`; the detail
view has `watchers` and `watching` (are *you* one). The task list filter `watcher=me` shows what you follow.

When a watched task changes (any field, including status) or gets a comment, the Zoho Cliq message about it ends with `Watching: Name, Name`. The webhook posts to one channel, so this
names people rather than messaging them privately. The person who made the change is never named, and neither is someone already told by the same change (the new assignee, or an
@mentioned user).

### Bulk actions

`POST /api/tasks/bulk` applies the same changes to many tasks:

```json
{"tasks": ["WEB-1", "WEB-2", "API-7"], "changes": {"status": "done", "assignee": "bob", "add_labels": ["q4"]}, "atomic": false}
```

| Field | Notes |
|---|---|
| `tasks` | 1-100 task keys (any projects; duplicates and case are ignored) |
| `changes` | At least one of `status`, `priority`, `assignee` (username or `null`), `milestone` (id or `null`), `due_date` (date or `null`), `labels` (replace), `add_labels`, `remove_labels` (`labels` cannot be combined with the other two). Other fields are refused with 422 |
| `atomic` | Optional, default `false`. `true` undoes everything and returns `409 bulk_failed` if any task could not be changed |

Every task goes through exactly the rules of `PATCH /api/tasks/{key}` (workflow, blockers, open subtasks, project membership, archived projects, per-field validation) and gets its own history
entry. A malformed request is refused as a whole with 422. Otherwise the response is `200` even when some tasks were refused:

```json
{"requested": 3, "updated": 2, "unchanged": 0, "failed": 1, "atomic": false,
 "results": [{"key": "WEB-1", "result": "updated"}, {"key": "WEB-2", "result": "updated"},
             {"key": "API-7", "result": "failed", "error": {"code": "not_project_member", "message": "..."}}]}
```

A refused task changes nothing (not even the other fields of the same request). Tasks refused only because of the order they were processed in - closing a parent
that is selected together with its subtasks - are retried after the others, so the order of `tasks` does not matter. One summary message goes to Cliq, not one per task.

## Milestones

| Endpoint | Role | Notes |
|---|---|---|
| `PATCH /api/milestones/{milestone_id}` | manager | `{name?, due_date?, description?}` |
| `DELETE /api/milestones/{milestone_id}` | manager | Linked tasks keep existing and lose the link |

## Attachments

Files are attached to tasks. Unlike the rest of the API, the request body is **the file itself** (no multipart form, no base64) and the
name travels in the query string. `Expect:` stops curl waiting for a `100 Continue` the server does not send.

```bash
curl -X POST "http://127.0.0.1:8080/api/tasks/WEB-5/attachments?filename=login-error.png" \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: image/png" -H "Expect:" --data-binary @login-error.png

curl -H "Authorization: Bearer $TOKEN" -o login-error.png http://127.0.0.1:8080/api/attachments/7/content
```

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/tasks/{key}/attachments` | viewer | `{items, total_bytes, limits: {max_file_bytes, max_per_task}}`, oldest first |
| `POST /api/tasks/{key}/attachments` | member | Body: the file's bytes. `?filename=` is required. 201 with the attachment. Needs write access to the project |
| `GET /api/attachments/{attachment_id}/content` | viewer | The bytes, always with `Content-Disposition: attachment` |
| `DELETE /api/attachments/{attachment_id}` | member | The person who attached the file, or a manager. Write access to the project is needed |
| `GET /api/projects/{key}/attachments` | viewer | Every file of the project, newest first, each with `task` and `task_title`. `?q=` (file name contains, case-insensitive) `?limit=` (default 25, max 100) `?offset=`. Also `total` and `total_bytes` (of the matches) |

### Attachment fields

`id` · `task` (key) · `filename` · `content_type` · `size` (bytes) · `sha256` (hex, of the content) · `preview` · `uploaded_by` (`id`, `username`,
`display_name`) · `created_at`. `preview` is `image/png`, `image/jpeg`, `image/gif` or `image/webp` when the first bytes of the file really are
that kind of image, and `null` for everything else: it is what the UI may display, whatever the file claims to be.

### Rules

- **File name.** Reduced to a plain name: directories (`/` and `\`), control characters and invisible direction overrides are removed,
  blanks collapse, trailing dots and blanks go, Unicode is normalised (NFC), and it is shortened to 180 characters keeping the extension.
  Nothing usable left means `file`. The response shows the name that was stored.
- **Type.** `Content-Type` of the upload is stored as the file's type when it is a well-formed `type/subtype` (parameters dropped); when it is
  missing, malformed or `application/octet-stream`, the type is guessed from the extension, else `application/octet-stream`. It is a label
  for API clients and is returned as the `Content-Type` of the download; it never decides how the UI shows a file.
- **Limits.** `ZYLKER_ATTACHMENT_MAX_MB` per file (413 `payload_too_large`; the body is not read, so some clients see a reset connection
  instead of the answer), 25 files per task (409 `too_many_attachments`), `ZYLKER_ATTACHMENT_QUOTA_MB` in total (409
  `storage_quota_exceeded`). A missing `filename` or an empty body is a 422. A connection that ends before the whole body has arrived is a
  400 `incomplete_body`, and nothing is stored. The size must be known up front: a chunked upload (`Transfer-Encoding: chunked`, e.g. `curl -T -`)
  is a 400 `length_required`.
- **Downloads** carry `Content-Disposition: attachment` with an ASCII `filename` and a UTF-8 `filename*`, `X-Content-Type-Options: nosniff`
  and `Cache-Control: no-store`, like every other API response. Authentication is the bearer header, so a download cannot be started from a
  plain link.
- **Deleting** a task or project deletes its attachments. Uploads and deletions (`attachment.added`, `attachment.deleted`) are audited, without the content.

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
| `task` | `task.created` `task.updated` `task.status_changed` `task.deleted` `task.dependency_added` `task.dependency_updated` `task.dependency_removed` `task.link_added` `task.link_removed` |
| `checklist` | `checklist.added` `checklist.removed` |
| `watcher` | `watcher.added` `watcher.removed` |
| `project_field` | `project_field.created` `project_field.updated` `project_field.deleted` |
| `comment` | `comment.added` `comment.edited` `comment.deleted` |
| `attachment` | `attachment.added` `attachment.deleted` |
| `time` | `time.logged` `time.deleted` |
| `baseline` | `baseline.created` `baseline.deleted` |
| `import` | `import.completed` (one event per CSV or Microsoft Project import, not one per row; `details` has `created`, `skipped_duplicates` and `errors`, and `"source": "msproject"` for Microsoft Project) |

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

## Delivery projection

Plays the open work forward and says which tasks will miss their due dates, by how many days and why. It is read-only and stores
nothing. Every project that is `planning` or `active` is scheduled together, so a person shared between projects is not promised to
both; the README lists the scheduling rules.

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/projects/{key}/projection` | viewer | The project's projection. `?capacity=bob:50,carol:80` (percent of normal availability, 0-200), `?overrun=20` (percent extra effort on every task, 0-300) and `?reassign=WEB-3:carol,WEB-4:dave` (somebody else does that task) are what-if options; with any of them the answer carries a `scenario` block comparing it with the unchanged plan |
| `GET /api/projects/{key}/projection/advice` | viewer | Which single reassignment would help most: `?limit=1..10` suggestions (default 3), see below |
| `GET /api/reports/projection` | viewer | One line per project that is not archived (worst first): `state`, `finish`, `due_date`, `late_by_days`, `verdict`, `headline`, `open_tasks`, `late_tasks`. Takes the same what-if options |

`GET /api/projects/{key}/projection` returns `state` (`projected`, `complete` when nothing is open, `not_projected` for projects on hold,
completed or archived, with a `reason`), `finish` (the day the last task is done), `due_date`, `late_by_days` (days after the project's due
date, 0 when it fits, `null` when there is nothing to compare), `verdict` (`on_track`, `will_miss`, `no_due_date`, `unknown` when some
work cannot be scheduled), a plain-English `headline`, and `counts` (`open_tasks`, `late_tasks`, `unprojectable`, `overdue_tasks`).
It also lists:

- `tasks`: every open leaf task, late ones first (at most 1,000; `tasks_truncated` says whether the list was cut). Each has
  `projected_start`, `projected_finish`, `margin_days` (negative when late), `late_by_days`, `remaining_hours`, `overdue`, and `state`
  (`scheduled`, `no_capacity`, `waiting_for_unschedulable`, `beyond_horizon`). `why` is structured (`kind`: `immediately`, `blocker`,
  `queue` or `start_date`, with the `task` it followed and the date `until` which it was busy) and `explanation` says the same in a
  sentence, including the root cause of a delay. `notes` lists the assumptions made for this task (no estimate, unassigned, flagged blocked...).
- `milestones`: each with its `open_tasks`, `projected_finish` and `late_by_days`.
- `people`: whoever is assigned work in the project: `hours_per_day`, `capacity_pct` (what-if), `open_hours` (all projects) and
  `open_hours_here`, `late_tasks` and `busy_until` (all projects).
- `assumptions`: the rules in force (work days, default hours, the pace of unassigned work, the horizon of 1,300 working days).

Each `reassign` item names an open task of a project that is being worked on (not a parent task) and a user who is an active member of that
project; anything else is a `422` that says what is wrong. With the option the task is scheduled as if it were assigned to that person
(the task itself is not changed), and `scenario.reassign` lists `task`, `to` and `to_name`.

`GET /api/projects/{key}/projection/advice` takes the tasks of the project that the late work had to wait for (the late tasks, the tasks
that end a late project and those that cannot be scheduled, and everything they followed in somebody's queue or waited on), tries giving each of
them, one at a time, to every other member of the project who has capacity, schedules again, and keeps the moves that improve the project -
fewer tasks that cannot be scheduled, or fewer days late, or fewer late tasks - without making a task in another project late or unschedulable. It
returns `suggestions` (best first: the best move for each task, ranked by the same three measures and then by when everything would be
done), `tried` (how many reassignments were scheduled), `limited` (true when the search was cut short: at most 60 reassignments are tried,
fewer when each means scheduling a great many tasks again) and `reason` (why there are no suggestions: nothing is late, the project is not being worked on, or no
single move helps). Each suggestion has `task`, `title`, `from`, `to` (with `open_hours` and `busy_until`, the person's other work),
`before` and `after` (`finish`, `late_by_days`, `late_tasks`, `unprojectable`, the numbers the projection would report), a `summary`
sentence and `reassign`, the value to pass as `?reassign=` to see the whole picture with that move. The advice looks only at capacity and
queues, never at skills, and never changes anything.

Errors: `422` for a malformed `capacity`, `overrun`, `reassign` or `limit`; `409 projection_too_large` when there are more than 10,000 open tasks.

## Baselines

A baseline is a frozen copy of a project's plan: for every task that is not cancelled, its title, start and due date, estimate, status and (when
it is done) the day it was completed, and the project's milestones with their due dates, as they were when the baseline was set. It is never
edited, only deleted. Comparing it with the project as it is now says what moved, which the projection (what *will* happen) cannot.

`GET /api/projects/{key}/baselines` returns `{"items": [...], "limits": {"max_baselines": 10}}`, newest first. Each item has `id`, `project`,
`name`, `note`, `created_at`, `created_by` (a user reference, `null` when no user is recorded), `tasks` (how many tasks it holds),
`leaf_tasks`, `milestones`, `estimate_hours` (the scope it recorded) and `finish` (its last finish date, `null` when no task had a date).

`POST /api/projects/{key}/baselines` takes `{"name": "Kickoff", "note": "Agreed with the client"}`, both optional, and returns the new item with `201`.
It is refused on an archived project (`409 project_archived`), when the name is already used in the project (`409 baseline_name_taken`), when the
project already has 10 baselines (`409 too_many_baselines`) and when the project has more than 10,000 tasks (`409 baseline_too_large`); a name or
note with control or direction-override characters, or one that is too long, is a `422`. Nothing is stored when it is refused.

`GET /api/projects/{key}/baselines/{baseline_id}` returns `baseline` (the item above), `project`, `as_of` (today in the organisation's time zone) and:

- `summary`: `tasks` (`planned` and `now` count leaf tasks; `compared`, `on_plan`, `slipped`, `ahead`, `no_dates`, `added`, `dropped`, `split`),
  `slip` (`max_days` and `average_days` over the tasks that slipped), `overdue` (open tasks past their due date now), `finish` (`baseline`,
  `now`, `shift_days`: the project's last finish date then and now), `scope_hours` (`baseline`, `now`, `change`) and `milestones` (`planned`,
  `on_plan`, `slipped`, `ahead`, `dropped`, `added`).
- `tasks`: the tasks that are in both, worst slip first, then the ones that moved earlier, then the ones on plan, then those without dates (at most
  5,000; `tasks_truncated` says whether the list was cut). Each has `key`, `number`, `title`, `status`, `assignee`, `state` (`slipped`, `ahead`, `on_plan`,
  `no_dates`), `slip_days`, `overdue`, `start_shift_days`, `due_shift_days`, `baseline` (`status`, `start_date`, `due_date`, `finish`, `estimate_hours`) and
  `now` (`start_date`, `due_date`, `finish`, `finished`, `estimate_hours`). A shift is `null` when either side has no date.
- `added` and `dropped`: tasks that are new since the baseline and tasks that were cancelled or deleted (with a `reason`), at most 200 of each.
- `milestones`: `items` (each with `state`: `slipped`, `ahead`, `on_plan` or `dropped`, `baseline_due_date`, `due_date` and `slip_days`) and `added`.

The rules:

- Only leaf tasks (tasks without subtasks) are compared. A parent has no work of its own, so a baseline that held a parent and its subtasks is not counted twice.
- A task's *finish* is the day it was completed when it is done (in the organisation's time zone), otherwise its due date. `slip_days` is the finish
  now minus the finish in the baseline, in calendar days: positive is late, negative is early. A task that was finished late has slipped even though
  nobody moved its due date, and a task that was already done when the baseline was set cannot slip.
- An open task that is past its due date is `overdue` but has not slipped yet: nothing says when it will really finish, and the projection does that.
- A leaf that has been split into subtasks since is counted in `split`, and its subtasks are `added`. A task that has been cancelled or deleted is `dropped`.
  A cancelled task comes back as new work if it is reopened.
- Scope is the sum of the estimates of the leaf tasks; a task without an estimate counts as 0 hours.
- The tables are created when the server starts; until then every baseline endpoint answers `409 baselines_not_initialised`.

Setting and deleting a baseline are written to the project's activity and can be delivered to webhooks (`baseline.created`, `baseline.deleted`).
An unknown baseline, or one that belongs to another project, is a `404 baseline_not_found`.

## Microsoft Project

Plans are exchanged as the XML that Microsoft Project writes with *File > Save As > XML Format* (MSPDI, namespace
`http://schemas.microsoft.com/project`); other planning tools read and write it too. The binary `.mpp` format is not supported. The two
endpoints are in the [Projects](#projects) table; the README ("Microsoft Project import and export") has the field-by-field mapping.

`GET /api/projects/{key}/export/msproject` answers with `Content-Type: application/xml; charset=utf-8` and
`Content-Disposition: attachment; filename="KEY-msproject.xml"`. Task *n* of the project is UID *n* (`WEB-12` is UID 12), the project row is UID 0,
and milestones follow the tasks with the next UIDs. Nothing is stored or changed by an export.

`POST /api/projects/{key}/import/msproject` takes the file itself as the request body. Tasks, milestones and dependencies are only ever
*added*: a task that is already in the project is never edited or removed, so importing the same file twice creates nothing the second time
(tasks are recognised by an `external_ref` of the form `msp:<file id>:<UID>`; a file that was exported from the same project is matched by UID =
task number). Valid tasks are imported even when others are not. The answer is `200` with:

| Field | Meaning |
|---|---|
| `dry_run` | `true` when `?dry_run=true` was given: everything is checked and counted, nothing is saved (no task numbers are used, no members added, nothing audited) |
| `tasks_read` | Tasks found in the file (the project row and blank rows are not counted) |
| `created` / `skipped_duplicates` | Tasks created now / tasks that were already in the project |
| `milestones` / `links` | Milestones and dependencies created |
| `errors` | `[{"uid": 7, "message": "..."}]`: tasks or dependencies that could not be imported, with the task's UID in the file (`null` when it is about the file as a whole) |
| `warnings` | Same shape: things that were imported differently from the file (a name cut short, a person left unassigned, a dependency type the app does not have) |
| `keys` | Keys of the tasks created, in file order (the first 100) |

At most 200 errors and 200 warnings are listed; the last entry then says how many more there were. A real import writes one `import.completed`
audit entry.

Errors: `403` below manager role; `409 project_archived`; `413` when the body exceeds 5 MB; `415` unless the content type is `application/xml`
or `text/xml`; `422` when the file as a whole cannot be used (empty, not well-formed, not a Microsoft Project document, has a `DOCTYPE` or
entity declaration, nested more than 100 levels, declares a text encoding that cannot be read, or has more than 5,000 tasks) - nothing is
imported then. The parser never expands entities and never reads anything outside the request body.
