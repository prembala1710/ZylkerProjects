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
