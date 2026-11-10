# ZylkerProjects

A project management tool developed by Zylker Corporation: one place to plan projects, break them into tasks, see who is
overloaded, and spot slipping work early. It is a small web application (REST API + browser UI) that runs from the Python
standard library alone - **no packages to install, no build step** - and stores everything in a single SQLite file.

## What it does

- **Projects and tasks** - projects with owners, members, dates and a lifecycle (planning, active, on hold, completed, archived);
  tasks with type, priority, assignee, estimate, labels, due/start dates, subtasks (up to 5 levels), comments with `@mentions`,
  and time logging.
- **A workflow that enforces itself** - allowed status moves are defined once; a task cannot start or finish while a blocking task
  is open, and a parent cannot close while subtasks are open. The UI shows *why* a move was refused.
- **Dependencies with deadlock protection** - "blocked by" links between tasks of a project. Loops are refused, including the
  sneaky kind that run through parent/subtask relationships.
- **Boards, lists and a timeline** - drag-and-drop Kanban board, sortable/filterable list, and a Gantt-style timeline with
  milestones and the critical path highlighted.
- **Health you can trust** - every project gets an on-track / at-risk / off-track status with the *reasons* spelled out
  (see [How health is decided](#how-health-is-decided)); a portfolio dashboard sorts the worst first.
- **Delivery projection** - health tells you how a project looks today; the projection plays the open work forward, using
  dependencies, priorities and each person's capacity across all projects at once, and tells you which tasks *will* miss their
  dates, by how many days and why, while there is still time to act, and which single reassignment would help most.
  See [Delivery projection](#delivery-projection).
- **Workload and capacity** - estimated work due in the next N weeks vs each person's weekly capacity, with overallocation flagged.
- **Burndown, critical path, timesheets, digests** - computed live from the data.
- **Roles and an audit trail** - viewer / member / manager / admin, per-project membership, and a field-level history of every change.
- **Zoho Cliq notifications** (optional) - assignments, completions, blockers, @mentions and a scheduled overdue digest.
- **CSV import / export** - bring in a Jira-style export (with a dry run) or take your data out.

## Quick start

Requires Python 3.10 or newer.

```bash
python -m zylker_pm init --admin yourname --name "Your Name"   # creates zylker.db and prints your API token ONCE
python -m zylker_pm serve                                       # http://127.0.0.1:8080
```

Open the page and paste the token. Optional: `python -m zylker_pm seed-demo` loads a clearly fictional organisation
(three projects, five users) so you can click around first - run it on an empty database only.

The same commands are available as `zylker-pm ...` after `pip install .`. Everything the CLI can do:

| Command | Purpose |
|---|---|
| `zylker-pm init [--admin USER --name NAME --email E]` | Create the database; optionally create the first admin and print its token |
| `zylker-pm serve [--host H --port P]` | Run the web UI and API (threaded; logs one line per request, never headers or tokens) |
| `zylker-pm create-user USER --role member --name "..." [--capacity 30]` | Add a person and print their token |
| `zylker-pm rotate-token USER` | Issue a new token; the old one stops working immediately |
| `zylker-pm digest [--days 3] [--project KEY] [--post]` | Print the overdue / due-soon digest; `--post` sends it to Cliq |
| `zylker-pm projection [--project KEY] [--post] [--fail-if-late]` | Print which projects and tasks are projected to miss their dates, and why; `--post` sends it to Cliq, `--fail-if-late` exits with code 3 when anything is late |
| `zylker-pm backup FILE [--force]` | Write a consistent copy of the database (safe while the server runs) |
| `zylker-pm import-csv FILE --project KEY --as USER [--dry-run] [--add-missing-members]` | Import tasks; exit code 3 means "finished, but some rows had errors" |
| `zylker-pm seed-demo` | Load fictional sample data (refuses unless there are no projects) |

Global option: `--db PATH` (otherwise `$ZYLKER_DB`, then `./zylker.db`).

## Configuration

All settings are environment variables; nothing is read from the repository.

| Variable | Default | Meaning |
|---|---|---|
| `ZYLKER_DB` | `zylker.db` | SQLite file |
| `ZYLKER_HOST` | `127.0.0.1` | Listen address. Anything other than loopback prints a warning: put a TLS-terminating reverse proxy in front |
| `ZYLKER_PORT` | `8080` | Listen port |
| `ZYLKER_TIMEZONE` | `UTC` | IANA name, e.g. `Asia/Kolkata`. Decides what "today" is for overdue checks, digests and burndown days. Timestamps are always stored in UTC |
| `ZYLKER_PUBLIC_URL` | *(empty)* | Public base URL (`https://pm.example.com`); makes task keys in Cliq messages clickable |
| `ZYLKER_CLIQ_ENDPOINT` | *(empty = notifications off)* | A Cliq **incoming webhook URL**. It contains a key - treat it as a secret. Must be `https` (plain `http` only for localhost) |
| `ZYLKER_CLIQ_BOT_UNIQUE_NAME` | *(empty)* | Post as this bot (appended as `bot_unique_name`, the same convention as the repository's GitHub Informer workflow) |

## Roles

| Role | Can |
|---|---|
| **viewer** | Read everything: projects, tasks, reports, exports |
| **member** | Viewer + create/edit tasks, comment, log time and link dependencies **in projects they belong to** |
| **manager** | Member + everything in *any* project: create and edit projects, manage members and milestones, delete tasks, import CSV, see everyone's timesheet |
| **admin** | Manager + manage users and roles, delete projects, read the global audit trail |

Project membership also controls who can be *assigned* work. Archived projects are read-only for all roles.
The server is the authority: the UI hides controls a role cannot use, but every rule is enforced in the API.

## Rules the system enforces

**Workflow.** `todo -> in_progress -> in_review -> done`, plus `blocked` and `cancelled`; done and cancelled tasks can be
reopened to `todo` (and done to `in_progress`). The complete map is returned by `GET /api/meta`. Moving a task to
`in_progress`, `in_review` or `done` is refused while any of its blockers is open (done or cancelled blockers do not block).
Setting a task to `blocked` is always allowed - it is the manual "waiting on something outside the system" flag.

**Hierarchy.** Subtasks nest up to five levels. A parent cannot be closed while a subtask is open, an open subtask cannot be
added under (or reopened beneath) a closed parent, and deleting a parent needs `?cascade=true`.

**Dependencies.** Finish-to-start, same project only. A *deadlock* is a cycle in the combined graph of dependencies and
"child must finish before parent". Checking the combined graph is what catches `parent -> X -> child` loops: a parent can
never be made to wait for its own subtask. The refusal names the loop, e.g. `WEB-3 -> WEB-1 -> WEB-2 -> WEB-3`.

**Auditability.** Every create, update, status change, link, comment and time entry is recorded with who and when; edits store
old and new values.

## How health is decided

Metrics count **leaf tasks** (those without subtasks) that are not cancelled, so parents are containers and nothing is counted twice.

| Status | When |
|---|---|
| `on_hold` / `complete` / `not_started` | Project is on hold; or completed / every task is closed; or it has no tasks |
| **off track** | The project due date has passed with open tasks, **or** any *critical*-priority task is overdue, **or** >= 25% of open tasks are overdue |
| **at risk** | Not off track, and any of: any task overdue; >= 25% of open tasks are *flagged* `blocked`; a task is waiting on a predecessor that is itself overdue; progress is more than 15 points behind the share of time elapsed; the project is due within 14 days and under 70% complete |
| **on track** | Otherwise |

Waiting on a predecessor is normal sequencing, so by itself it is **not** a risk - otherwise every dependency chain would
look "at risk". It is still shown as "waiting / blocked" so people can see what cannot start yet. The `reasons` array on
`GET /api/projects/{key}/summary` lists every rule that fired.

**Workload.** For each person: remaining estimate (`estimate - logged`, never below zero) of their open leaf tasks that are due
within the window (overdue work included), against `weekly_capacity_hours x weeks`. Tasks without a due date are reported
separately as *unscheduled*, tasks without an estimate as *unestimated*. Over 100% is flagged as overallocated.

**Critical path.** Classic critical-path method over open tasks that take part in a dependency. Duration is the remaining
estimate; an unestimated task counts as 8 hours (stated in the response), and a parent lasts as long as its open subtasks combined.
Hours are effort, not calendar time. Tasks with no slack form the critical path.

**Burndown.** Daily remaining work (task count or estimated hours) from task creation and closing timestamps, with the
ideal line drawn from the scope on the first day and a *scope* line that rises when tasks are added. A task that is closed and
later reopened is shown as open for the whole range, because only the latest closing time is stored.

## Delivery projection

Most trackers tell you a task is late once its due date has passed. The projection plays the plan forward instead, so you hear about a
slip while there is still time to do something about it.

It takes every open leaf task of every project that is *planning* or *active*, the hours each still needs, who is assigned, what each
task is blocked by and how many hours a week each person can work, and schedules **all of it together**: a person who is shared between
two projects is never promised to both. For every task you get a projected start and finish, how many days late it will be, and a
plain-language reason, for example:

> *APP-7 Release notes: waits for APP-6 (Beta feedback fixes), which finishes Tue 24 Nov. The delay starts in Carol Diaz's queue: busy
> with APP-2 until Fri 20 Nov.*

Following the links back from a late task is how the cause of a slip is found: here it is not APP-7, nor APP-6, but one person's queue.

**Where you see it**

- **Dashboard:** a *Projected finish* column ("4 days after the due date", "4 tasks will be late").
- **Board and list:** a red clock chip such as `+8d` on every task that is projected to miss its due date.
- **Task dialog:** the projected finish and the reason, plus the assumptions that were made for that task.
- **Reports tab:** the project's headline, the tasks that will slip and why, **what would help** (see below), milestones at risk, who is
  carrying the work, and a **What if...** form: *what if Carol is only 50% available? what if everything takes 20% longer?* The answer
  shows how far the finish moves and how many more tasks become late. The same options are available as `?capacity=carol:50&overrun=20`.
- **Terminal and Cliq:** `zylker-pm projection` prints the early warning (only the projects that need attention, worst first, each of
  the worst three with its best suggested fix); `--post` sends it to the Cliq channel, so a cron job can announce it on Monday morning;
  `--fail-if-late` makes the exit code 3 for scripts.

**How it schedules.** The rules are deliberately simple, so that every date can be explained:

- Work days are Monday to Friday and there is no holiday or leave calendar. The plan starts at the beginning of the first working day
  on or after today.
- A person works their weekly capacity spread evenly over the five days, on **one task at a time**, and is not pulled off a task they
  have started. When they are free they take the ready task with the highest priority, then the earliest due date. Nobody sits idle if
  there is something they could be doing.
- A task is *ready* when everything it is blocked by has finished and its start date has arrived. Something that waits for a parent task
  waits for all of the parent's open subtasks and for whatever the parent itself waits for.
- Remaining work is the estimate minus the time already logged. A task without an estimate counts `default_task_hours` (8), the same
  assumption the critical path makes. A task with nothing left but to close it finishes as soon as it is ready.
- A task with nobody assigned (or whose assignee is deactivated) proceeds at 8 hours a day without queueing. A person with a capacity
  of 0 cannot finish anything, and neither can what waits for them; the projection says so instead of guessing.
- Projects that are on hold, completed or archived are left out, and so is their work, which no longer takes anyone's time.

**What would help.** A warning is more useful with a way out. For a project that is going to slip, the advice looks at the tasks that
the late work had to wait for - the late tasks themselves and whatever stood in front of them in somebody's queue - and tries giving each
of them, one at a time, to every other member of the project who has capacity. It plays the schedule again and keeps the moves that make
things better: fewer tasks that cannot be scheduled, or fewer days late, or fewer late tasks. A move is dropped if it would make a task in
another project late or unschedulable, so the problem is never just pushed onto somebody else. The best move for each task is listed best
first, with its effect in words:

> *Give APP-2 (Push notifications) to Alice Morgan instead of Carol Diaz: the project would finish Mon 16 Nov, 4 days before the due date,
> and 2 tasks would be late; now it finishes Tue 24 Nov, 4 days after the due date, and 4 tasks are late.*

**Preview** shows the whole picture with that move (`?reassign=APP-2:alice`, which also works for any task and member you like) and **Reset**
puts the plan back. Nothing is ever changed: you make the move yourself in the task. At most 60 reassignments are tried, and fewer when
each one means scheduling a great many tasks again, so the answer is never slow; the work that is late or cannot be scheduled goes first,
and the answer says when the search was cut short. The advice looks only at capacity and queues, not at skills or at who knows the
code: it is a starting point for a conversation.

**What it is not.** It is a projection, not a promise. Estimates are taken at face value, tasks are not split or pre-empted, there are no
holidays, and a task flagged *blocked* is assumed to unblock now (the task says so). The what-if options exist to show how sensitive the
answer is. It does not change health (which stays rule-based and explainable), nothing is stored, and every number is computed from live
data when you ask. More than 10,000 open tasks are refused with a clear error rather than answered slowly.

## Zoho Cliq

Set `ZYLKER_CLIQ_ENDPOINT` to your channel's incoming-webhook URL and the server posts, best effort and in the background (a
Cliq outage never fails a request, and nothing is sent for a request that was rolled back):

- a task is assigned to someone other than the person who assigned it
- a task is completed, or marked blocked
- someone is `@mentioned` in a comment
- a project's status changes
- the digest, when you run `zylker-pm digest --post`
- the delivery projection, with a suggested fix for the worst projects, when you run `zylker-pm projection --post`

Schedule the digest with whatever you already use, for example cron on weekdays at 09:00:

```cron
0 9 * * 1-5  cd /srv/zylker && ZYLKER_CLIQ_ENDPOINT='https://cliq.zoho.com/...' ZYLKER_TIMEZONE=Asia/Kolkata zylker-pm digest --post
```

Task titles are stripped of Cliq markup before posting, so a title cannot inject links, bold text or mentions into a channel.
Delivery is covered by tests against a local stand-in server, **not** against live Cliq: after configuring, run
`zylker-pm digest --post` once and check the channel.

## CSV import and export

Export: **List -> Export CSV** (or `GET /api/projects/KEY/export.csv`). Cells that start with `= + - @` are prefixed with `'` so a
spreadsheet never executes task text as a formula; the importer strips that prefix again, so a round trip is lossless.

Import: **Project settings -> Import tasks from CSV** (or `import-csv`). Recognised columns (case-insensitive, extra columns ignored):

| Field | Accepted headers |
|---|---|
| title (required) | `title`, `summary`, `name`, `task`, `task name` |
| description | `description` |
| type | `type`, `issue type`, `work type`, `work item type` (task / bug / story / epic, plus common synonyms) |
| status | `status` (`To Do`, `In Progress`, `In Review`, `Blocked`, `Done`, `Cancelled`, and common synonyms) |
| priority | `priority` (`Lowest`..`Highest`, `Blocker`, `Critical`, ... mapped to low / medium / high / critical) |
| assignee, reporter | `assignee`, `assigned to`, `owner` / `reporter`, `creator`, `created by` - matched by username, e-mail or display name |
| dates | `start date`, `due date` - `2026-07-30`, `30-Jul-26`, `30/Jul/2026 12:00 AM`. Purely numeric `07/30/2026` is **refused** as ambiguous |
| estimate | `estimate (hours)`, `estimate hours`, `estimate` |
| labels | `labels`, `label`, `tags` (repeated columns are merged) |
| key | `issue key`, `key`, `external ref` - stored as `external_ref` and used to skip rows that were already imported |

Valid rows are imported and bad rows are reported by row number - one bad row never blocks the rest. Use the dry run first.
Unknown assignees are left unassigned with a warning. Subtask/parent links and milestones are not imported.
`examples/jira_style_import.csv` is a small fictional file in this layout.

## Operations

- **Backups:** `zylker-pm backup /backups/zylker-$(date +%F).db` uses SQLite's online backup, so it is safe while the server runs.
  Backups contain real project data and token *hashes*; protect them like the database. `.gitignore` keeps `*.db` files out of git.
- **Deploying:** run behind a reverse proxy that terminates TLS and (if exposed beyond a trusted network) rate-limits. The app
  speaks plain HTTP, sends no HSTS header, and has no CORS (same-origin only). It is also a standard WSGI callable
  (`zylker_pm.app.App`) if you prefer gunicorn or waitress.
- **Scale:** SQLite in WAL mode with one writer at a time suits a team or department on a single host. It has **not** been load-tested.
- **Upgrades:** the schema version is stored in the database; a database newer than the code is refused rather than modified.

## Security notes

- Authentication is a bearer token per user: 256 random bits, stored only as a SHA-256 hash, shown once, rotatable, and dead the
  moment the user is deactivated. The browser keeps it in `sessionStorage` (gone when the tab closes) unless "Remember me" is ticked,
  which uses `localStorage`.
- All SQL is parameterised; sort and filter values are validated against whitelists. Request bodies are size-limited and
  content-type checked. Error responses never include stack traces, and tokens are never logged or echoed.
- The UI builds the DOM with `textContent` only (no `innerHTML` anywhere) and the server sends a strict Content-Security-Policy -
  no inline scripts or styles, no external resources, no framing - so stored text cannot become markup. A browser test submits
  `<script>`/`<img onerror>` payloads in titles, descriptions, comments and project names and checks that none executes.
- Static files are served from a fixed whitelist of three paths; there is no filesystem lookup from the URL.
- **Not covered:** single sign-on (Zoho or otherwise), token expiry, failed-login throttling (tokens are unguessable, but throttle at
  the proxy anyway), e-mail notifications, per-project read restrictions (every signed-in user can read every project), file attachments.

## Development

```bash
python -m unittest discover -s tests -v     # ~180 tests, no dependencies
pip install playwright && playwright install chromium
python -m unittest tests.test_ui_e2e -v      # 18 real-browser tests (skipped automatically without Playwright)
```

The backend tests include hand-computed expectations for the critical path, workload, burndown and health rules, deadlock
detection, concurrency (parallel writers get unique, gapless task numbers), rollback-on-failure, and a docs-consistency test that
fails if an endpoint, setting or CLI command is undocumented. `docs/API.md` is the endpoint reference.

```
zylker_pm/
  app.py routes.py     WSGI app, routing, auth, per-request transactions, security headers
  services/            all business rules (tasks, graph, projects, reports, csvio, ...)
  notify.py            Cliq notifier; db.py schema; seed.py demo data; cli.py commands
  static/              the single-page UI (index.html, app.js, styles.css)
tests/                 unit, HTTP, CLI and browser tests
```

## Limitations and ideas for later

Dependencies are finish-to-start and within one project; there are no custom fields, recurring tasks, task templates or
attachments; the burndown does not remember reopened tasks; the timeline draws no dependency arrows; SSO and per-project read
permissions would be the natural next steps for a larger organisation.

*The files `anand` and `test.ds` at the repository root are pre-existing test artefacts and are not part of the application.*
