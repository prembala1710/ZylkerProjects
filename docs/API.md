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
| `GET /api/meta` | viewer | Enumerations, the workflow transition map, server `today` and timezone, custom-field types and limits |
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
