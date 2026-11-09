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

## Reports

| Endpoint | Role | Notes |
|---|---|---|
| `GET /api/reports/portfolio` | viewer | Every non-archived project with progress, health, overdue, next milestone - worst first |
| `GET /api/reports/workload` | viewer | `?weeks=1..12` (default 2) `?project=`. Per person: scheduled hours vs capacity, utilisation, overallocation |
| `GET /api/reports/timesheet` | viewer | `?from=&to=&project=&user=`. Members and viewers only ever see their own time; managers see everyone |
| `GET /api/reports/digest` | viewer | `?days=0..30` (default 3) `?project=`. Overdue and due-soon tasks grouped by assignee |
