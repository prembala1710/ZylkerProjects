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
- **Workload and capacity** - estimated work due in the next N weeks vs each person's weekly capacity, with overallocation flagged.
- **Burndown, critical path, timesheets, digests** - computed live from the data.
- **Roles and an audit trail** - viewer / member / manager / admin, per-project membership, and a field-level history of every change.
- **Zoho Cliq notifications** (optional) - assignments, completions, blockers, @mentions and a scheduled overdue digest.
- **CSV import / export** - bring in a Jira-style export (with a dry run) or take your data out.
- **Custom project fields** - admins define extra text, number and URL fields (a client, a budget, a wiki link); managers fill them in
  per project (see [Custom project fields](#custom-project-fields)).
- **Admin audit log** - one screen for administrators showing every change plus failed sign-ins, permission denials, exports and server
  errors, with filters, a live view and CSV export (see [Audit log](#audit-log)).
- **Brute-force protection and rate limits** - failed sign-ins block the address with growing waits, and each user has a request budget; both
  answer `429` with `Retry-After` (see [Throttling](#throttling)).
- **Webhooks** - signed, retried, logged event delivery to other systems (chat, CI, your own service) with protection against calls to internal addresses (see [Webhooks](#webhooks)).
- **Search** - one box for tasks, comments and projects: ranked, stemmed, with phrases, `OR`, exclusions and filters such as `status:open assignee:me`, highlighted snippets and a jump-to-key shortcut (see [Search](#search)).
- **Recurring tasks** - "every other Monday", "the last day of each month": managers define a schedule and a task template, the system creates the
  tasks on time without ever duplicating or flooding (see [Recurring tasks](#recurring-tasks)).
- **Sprints** - time-boxed iterations: plan tasks against each person's capacity, start (the commitment is fixed), watch scope change and a burndown with a
  working-day ideal line, close with unfinished work carried over, and get velocity and a delivery forecast from the sprints that have closed (see [Sprints](#sprints)).
- **Project templates** - save the shape of a project (milestones, task tree, dependencies, estimates, labels, who-does-what as role slots) and start new projects from it:
  pick a start day and every date moves, fill the slots with real people, preview first (see [Project templates](#project-templates)).
- **Scoped, expiring API tokens** - credentials for scripts and integrations that expire, can be capped to a lower role, made read-only or limited to
  certain addresses, and can be revoked at any time (see [API tokens](#api-tokens)).

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
| `zylker-pm create-token USER --name N [--days 30] [--role-cap ROLE] [--read-only] [--ip CIDR ...]` | Mint an expiring, restricted [API token](#api-tokens) for a user (for example a service account) and print it once |
| `zylker-pm list-tokens [USER] [--status active,expired,revoked]` | List API tokens (never their values), everyone's unless a user is named |
| `zylker-pm revoke-tokens USER` | Revoke every active API token of a user (their primary token is unchanged) |
| `zylker-pm digest [--days 3] [--project KEY] [--post]` | Print the overdue / due-soon digest; `--post` sends it to Cliq |
| `zylker-pm recur-run [--project KEY] [--dry-run]` | Create the recurring tasks that are due (safe to run from cron as often as you like) |
| `zylker-pm webhook-run [--limit N]` | Deliver the queued webhooks that are due, retries included (for installations that run `serve` with the delivery loop off) |
| `zylker-pm search-rebuild` | Rebuild the full-text search index from the data (and move a database onto FTS5 if it had fallen back to substring search) |
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
| `ZYLKER_PROXY_HOPS` | `0` | How many reverse proxies you trust in front of the app (0-5). It decides which client address the [audit log](#audit-log) records: with `0` the connection's address is used and `X-Forwarded-For` is ignored (a client can forge it); with `1` the last entry of `X-Forwarded-For` is used, with `2` the second from the right, and so on |
| `ZYLKER_AUTH_FAILURE_LIMIT` | `10` | Failed sign-ins from one address within five minutes before it is blocked (0-1000; `0` turns the protection off) |
| `ZYLKER_RATE_LIMIT_PER_MINUTE` | `600` | Sustained requests per user per minute (0-100000; `0` turns the limit off) |
| `ZYLKER_WEBHOOK_INTERVAL_SECONDS` | `2` | How often `serve` delivers queued [webhooks](#webhooks) (0-3600; `0` turns the loop off and leaves it to `zylker-pm webhook-run`) |
| `ZYLKER_WEBHOOK_ALLOW_PRIVATE` | *(off)* | Set to `1` to let webhooks call private, loopback and link-local addresses and use plain `http` - only for receivers on a trusted internal network |
| `ZYLKER_RECURRENCE_CHECK_MINUTES` | `15` | How often `serve` looks for recurring tasks that are due (0-1440; `0` turns the check off and leaves it to `zylker-pm recur-run`) |

## Custom project fields

Every organisation has different things it wants to know about a project. Admins define **fields** under *Admin -> Project fields*
(or `POST /api/project-fields`); every project then carries a value for each one, edited by managers on the project's *Settings* tab
and shown in the project header. At most 20 fields.

| Type | Holds | Rules you can set |
|---|---|---|
| **Text** | One line of text | Maximum length (1-2000, default 255) |
| **Number** | Integers or decimals | Decimal places (0-3, default 2), optional minimum and maximum |
| **URL** | A web address | `http://` or `https://` only |

- **Strict, server-side validation.** Wrong types, control characters, too many decimal places (refused, never rounded), values outside
  the range, and any URL that is not plain `http(s)` with a host name - `javascript:`, `data:`, `//host`, embedded passwords,
  backslashes, spaces - are all rejected with a per-field message. The browser checks the same rules first for instant feedback.
- **Safe display.** Values are inserted as text; a URL becomes a link (opening in a new tab with `rel="noopener noreferrer nofollow"`) only
  if it is `http(s)`.
- **Required fields** must be filled in when a project is created and can never be cleared. Projects that existed before the field was
  made required stay editable until someone fills it in.
- **Safe changes.** The key and type of a field are permanent. A limit cannot be tightened past values projects already hold, and deleting
  a field needs the key typed back because it removes that value from every project.
- **Audited.** Value changes appear in the project's activity feed (`Client: Acme -> Beta`); definition changes in the admin audit trail.
- **Searchable.** The project search box also matches text and URL values.

Databases created by an earlier release are upgraded automatically the first time they are opened (schema v1 to v2, in one transaction;
a failed upgrade changes nothing).

## Audit log

Administrators get one place to see **everything that happens**: *Admin -> Audit log* in the UI, `GET /api/audit` for scripts.

- **What is recorded.** Every change to data (who, what, when, old and new values) plus the events that are not data changes but matter to an
  administrator: **failed sign-ins**, **permission denials**, **sessions starting**, **data exports** (task CSV and the audit log itself) and
  **server errors**. Routine reads are deliberately not recorded; they would bury the events that matter. The full list is in
  [docs/API.md](docs/API.md#audit-log).
- **What the screen gives you.** Counts for the period, a per-day chart, the busiest people and the addresses with the most failed sign-ins;
  filters for text, category, severity, person, project, address and dates; click any event for its request id, address and raw details; a
  **Live** switch that adds new events as they happen; and **Export CSV** of whatever the filters show.
- **Following one request.** Every response carries an `X-Request-Id` header. The same id is in the access log and in the audit log, and
  a 500 error body quotes it, so "something broke at 10:42" becomes one search.
- **Safe by construction.** The log never stores a token, request body, password or exception message. Text that comes from a client
  (user-agent, path) is stripped of control characters and truncated, so it cannot forge log lines, and the UI shows it as text.
- **A flood cannot fill the disk.** Repeats of the same failed sign-in, denial or error from the same address within five minutes update one
  row (`count`) instead of adding another.
- **It cannot get in the way.** Audit writes happen after the request's own transaction, on their own short connection; if one fails, the request
  still succeeds and a warning is logged.
- **History is not erased.** Deleting a project or a task keeps its history (events keep their `project` / `task` keys). Earlier releases
  deleted a project's history with it, and a task created after a delete could inherit the deleted task's history; the automatic schema
  upgrade repairs existing databases.

Behind a reverse proxy, set `ZYLKER_PROXY_HOPS` so the log records the real client rather than the proxy.
This is a record for people to read, not an alerting system, and it does not throttle: do that at the proxy.

## Throttling

The server defends itself against guessing and runaway clients; nothing needs configuring.

- **Guessing tokens.** Tokens are 256 random bits, so guessing is hopeless, but hammering is still noise and load. Ten failed sign-ins from one address
  in five minutes block that address for a minute; each repeat doubles the wait (up to an hour). Requests with a **valid** token are still served from a
  blocked address, so a shared office address is not locked out by one person's typo.
- **Runaway clients.** Each user - and each [API token](#api-tokens), separately - has a budget of 600 requests a minute (with a burst of 120). Past it they get `429` and a `Retry-After`; responses carry
  `X-RateLimit-Limit` and `X-RateLimit-Remaining`. The page itself and `GET /api/health` are never limited.
- **Visible.** Blocks and refusals appear in the [audit log](#audit-log) (`auth.throttled`, warnings, with repeats merged), the audit summary counts them, and
  admins can list or lift blocks with `GET` / `DELETE /api/security/blocks`.
- **Honest limits.** State is in memory and per process (a restart clears it; several processes each count on their own). Behind a reverse proxy set
  `ZYLKER_PROXY_HOPS`, otherwise every client looks like the proxy. This is a safety net, not a firewall: keep throttling at the proxy too if the app is exposed.

## Webhooks

Tell other systems when something happens. An administrator registers a URL with `POST /api/webhooks` (there is no screen for it yet), the events it wants (`task.created`, `task.*`, `project.*`, `auth.*`, or `*`), and optionally one project.

- **Signed.** Every request carries `X-Zylker-Signature` (HMAC-SHA256 over timestamp and body with the webhook's secret) and a timestamp, so the receiver can prove it came from here and
  refuse replays. The secret is shown once, when the webhook is created or its secret rotated. A short verifier is in [docs/API.md](docs/API.md#webhooks).
- **Never lost, never announced early.** The event is queued in the same transaction as the change: a rolled-back change sends nothing, a crash loses nothing.
- **Retried.** Anything but a 2xx is retried after 1 min, 5 min, 30 min, 2 h and 6 h. Delivery is at least once, so receivers ignore repeats of the same `X-Zylker-Delivery` id.
- **Self-protecting.** A receiver that fails 15 times in a row (or answers `410 Gone`) is switched off and an audit warning says so; the queue per webhook is capped; the log is pruned after 30 days.
- **Cannot be turned on your own network.** URLs must be `https`; private, loopback and cloud-metadata addresses are refused when saved *and* again at delivery (the name is resolved once and that address is
  used, which defeats DNS rebinding), redirects are not followed, and response bodies are not kept. `ZYLKER_WEBHOOK_ALLOW_PRIVATE=1` is the explicit opt-out for internal receivers.
- **Inspectable.** Every delivery, with its attempts, status codes and error codes, is in the delivery log; failed ones can be redelivered, and a test ping checks a new receiver.

Webhook events are not themselves delivered (no loops). The secret is stored in the database because it is needed to sign; protect backups as you would the database. Delivery state lives in the database, so
several `serve` processes or a `webhook-run` cron job can share the work without sending anything twice.

## Search

`GET /api/search?q=...` searches tasks, comments and projects at once and returns ranked results with highlighted snippets.

- **A small query language.** `login crash` (all words, stemmed), `"login page"` (phrase), `log*` (prefix), `login OR signin`, `login -safari` (exclude), and filters: `project:WEB status:open assignee:me priority:high type:bug label:auth is:closed in:comment`. Filters on their own list matching tasks, newest change first. Typing a task key such as `WEB-12` goes straight to it.
- **Safe by construction.** Every word is quoted before it reaches the search engine, so nothing a user types can be search syntax (or SQL); highlights come back as plain `{text, match}` segments, never markup.
- **Always current.** The index is kept by database triggers, in the same transaction as the change: CSV imports, recurring tasks and the CLI are covered as much as the API, a rolled-back change never shows up, deletes clean up after themselves. `GET /api/search/status` (admin) says whether the index matches the data; `POST /api/search/reindex` or `zylker-pm search-rebuild` repairs it.
- **Works without FTS5.** If the SQLite behind your Python lacks FTS5 the database falls back to case-insensitive substring search with the same filters and result shape (no stemming, cruder ranking).

Everyone who is signed in can search. The full syntax and the result format are in [docs/API.md](docs/API.md#search).

## Recurring tasks

For work that comes round again - the weekly report, the month-end close, the quarterly access review - a manager defines a **rule** on the project's
*Settings* tab (or with `POST /api/projects/{key}/recurrences`): a schedule, and the task to create.

- **Schedules:** daily, weekly (any days, every N weeks) and monthly (a day of the month, or the last day; the 31st becomes the 28th/29th/30th in short
  months), with an optional end date or maximum number of tasks. The form shows the next dates before you save.
- **Template:** title (`{date}` becomes the date), description, type, priority, assignee, estimate, labels, and how many days after it starts the task is due.
- **Runs by itself.** `serve` checks every 15 minutes (`ZYLKER_RECURRENCE_CHECK_MINUTES`) and once at start-up, so a restart over a due date catches up.
  Without `serve`, schedule `zylker-pm recur-run` from cron, e.g. `5 0 * * *`.
- **Never twice.** Every date is recorded the moment it is handled, so repeated or simultaneous runs cannot duplicate a task.
- **Never a flood.** After downtime only the latest 5 missed dates become tasks; the rest are recorded as skipped and show up in the audit log. New, resumed and
  edited rules start from today and never backfill.
- **Sensible about people and projects.** Archived and completed projects get nothing (the dates wait); if the assignee has left the project the task is created
  unassigned and says why; if the rule's creator is deactivated the project owner becomes the reporter.
- **Visible.** Rules show their next dates and every handled date (with the task it made, or why it was skipped); changes and generated tasks appear in the audit
  log under *Recurring tasks*. Deleting a rule keeps the tasks it made.

Day boundaries follow `ZYLKER_TIMEZONE`. Reference: [docs/API.md](docs/API.md#recurring-tasks).

## API tokens

The token you were given when your account was created is *you*: every power of your role, no expiry. Do not hand it to scripts. Mint an **API token** instead
(`POST /api/tokens`; an administrator can also run `zylker-pm create-token USER --name ci-nightly --days 30` for a service account):

- **It expires.** 1-365 days, 30 by default. After that it answers `401 token_expired`; there is no "forever".
- **Never more than its owner.** A token can be capped to a lower role (`role_cap`: a `member` token for a manager's script). It acts with the lower of the cap and
  the owner's *current* role, so demoting the owner demotes the token, and promoting the owner does not widen it.
- **Read-only** tokens refuse everything but `GET` and `HEAD` (`403 token_read_only`): for dashboards and reports.
- **Address allow-list.** Up to ten addresses or networks such as `203.0.113.0/24`; from anywhere else the answer is `401 token_ip_not_allowed`.
- **It cannot breed.** An API token cannot create, list or revoke tokens, nor rotate the primary token (`403 primary_token_required`): a leaked key can neither
  mint its own successor nor lock its owner out.
- **Revocable and visible.** Revoke one token or all of them at once (`DELETE /api/tokens`, `zylker-pm revoke-tokens`); deactivating the user disables every one. The
  listing shows when and from where each was last used. Creation, revocation and every refused use are in the [audit log](#audit-log).
- **Its own budget.** Each token has its own request budget, so a runaway job cannot starve its owner.
- **Stored as a hash.** Like primary tokens, only the SHA-256 digest is kept; the value is shown once, with a short `hint` (the last four characters) to tell tokens apart.

Limits: ten active tokens per user; expired and revoked ones stay listed for 30 days. Usage figures are batched (at most one write a minute per token), so they can lag by a minute.
There is no screen for tokens yet, and the primary token itself still never expires (rotate it with `rotate-token`). Reference: [docs/API.md](docs/API.md#api-tokens).

## Sprints

A manager creates a sprint for a project (`POST /api/projects/{key}/sprints`: a name, two dates, optionally a goal); project members plan tasks into it.

- **Planned against capacity.** For every project member the sprint shows hours of capacity (weekly capacity per working day, Monday to Friday) against the hours of their planned tasks, who is overallocated, and how much work is unassigned or unestimated.
- **Never counted twice.** A task is in one live sprint at a time, only open tasks of the same project can join, and a task and its parent or subtask cannot be in the same sprint.
- **A commitment, then scope change.** Starting a sprint fixes what was committed. Tasks added or removed afterwards are recorded with the time, so the sprint can say how much it grew, and a sprint that grows by more than a quarter is flagged *at risk*.
- **Burndown that is honest about time.** Remaining work per day, with an ideal line over the working days from the day the sprint really started and a scope line that moves when work is added or taken out.
- **Closing decides every task.** Done, dropped (cancelled) or carried. Carried work goes back to the backlog, or into the next planned sprint (or a chosen one), re-estimated, all in one transaction.
- **Velocity and forecast.** From the closed sprints: hours finished per sprint, completion rate, and - from the latest three - how many more sprints the remaining work needs, with an optimistic and a pessimistic case and a finish date.
- **Enforced by the database.** One active sprint per project and one live sprint per task are unique indexes, not only checks in code.

Sprints are managed through the API only (no screen yet), and sizes are the tasks' hour estimates (there are no story points). Reference: [docs/API.md](docs/API.md#sprints).

## Project templates

Every consultancy, product team or IT department starts the same kinds of project again and again. Save a good one once and reuse it.

- **Save** (`POST /api/projects/{key}/save-as-template`): the milestones, the task tree, the "blocked by" links, estimates, labels, priorities and descriptions. Only open work by default, every task starting again as *todo*. Who does what is kept as **slots**
  ("designer", "reviewer"): you name them, and fill them with real people when you start a project.
- **Start** (`POST /api/templates/{id}/instantiate`): choose a key, a name and a **start day**. Every date in the template is stored as "days from the anchor day", so the whole plan shifts to your start day; optionally weekend dates roll to Monday. People who get a slot join the project.
- **Preview first.** `.../preview` takes the same request and returns every project, milestone and task date it would create, without creating anything.
- **All or nothing.** A taken key, an unknown person, a missing required custom field - anything that would stop the run - is reported (all problems at once) and nothing is created. A template that somehow holds a dependency loop is refused.
- **Tidy about what it skips.** Closed tasks are left out (or included on request); their dependencies go with them and their subtasks move up to the nearest task that stays.
- **No flood.** Starting a project does not send a Cliq message per assigned task; the tasks, milestones and dependencies are in the audit log like hand-made ones, plus one `template.instantiated` event.

A template is a snapshot: later changes to the project it came from do not reach it, and deleting a template leaves its projects alone. Templates are managed through the API only (no screen yet) and their structure cannot be edited, only renamed.
Reference: [docs/API.md](docs/API.md#project-templates).

## Roles

| Role | Can |
|---|---|
| **viewer** | Read everything: projects, tasks, reports, exports |
| **member** | Viewer + create/edit tasks, comment, log time and link dependencies **in projects they belong to** |
| **manager** | Member + everything in *any* project: create and edit projects, manage members, milestones and recurring tasks, delete tasks, import CSV, see everyone's timesheet |
| **admin** | Manager + manage users and roles, define custom project fields, delete projects, read the audit log |

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

## Zoho Cliq

Set `ZYLKER_CLIQ_ENDPOINT` to your channel's incoming-webhook URL and the server posts, best effort and in the background (a
Cliq outage never fails a request, and nothing is sent for a request that was rolled back):

- a task is assigned to someone other than the person who assigned it
- a task is completed, or marked blocked
- someone is `@mentioned` in a comment
- a project's status changes
- the digest, when you run `zylker-pm digest --post`

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
- **Upgrades:** the schema version is stored in the database and older databases are upgraded automatically, one all-or-nothing step at a
  time; a database newer than the code is refused rather than modified. Take a `backup` first if the data matters. The audit log table can
  grow large over years; there is no pruning command, deliberately, because deleting an audit trail should be a conscious decision.

## Security notes

- Authentication is a bearer token per user: 256 random bits, stored only as a SHA-256 hash, shown once, rotatable, and dead the
  moment the user is deactivated. The browser keeps it in `sessionStorage` (gone when the tab closes) unless "Remember me" is ticked,
  which uses `localStorage`. Scripts should use [API tokens](#api-tokens) instead: they expire, can be restricted and are revocable.
- All SQL is parameterised; sort and filter values are validated against whitelists. Request bodies are size-limited and
  content-type checked. Error responses never include stack traces, and tokens are never logged or echoed.
- The UI builds the DOM with `textContent` only (no `innerHTML` anywhere) and the server sends a strict Content-Security-Policy -
  no inline scripts or styles, no external resources, no framing - so stored text cannot become markup. A browser test submits
  `<script>`/`<img onerror>` payloads in titles, descriptions, comments and project names and checks that none executes.
- Static files are served from a fixed whitelist of three paths; there is no filesystem lookup from the URL.
- **Not covered:** single sign-on (Zoho or otherwise), expiry of the primary token (API tokens do expire), failed-login throttling beyond the per-process limits described under [Throttling](#throttling) (keep throttling at the proxy too), e-mail notifications, per-project read restrictions (every signed-in user can read every project), file attachments.

## Development

```bash
python -m unittest discover -s tests -v     # ~760 tests, no dependencies
pip install playwright && playwright install chromium
python -m unittest discover -s tests -p "test_ui_*.py" -v   # 38 real-browser tests (skipped automatically without Playwright)
```

The backend tests include hand-computed expectations for the critical path, workload, burndown and health rules, deadlock
detection, concurrency (parallel writers get unique, gapless task numbers), rollback-on-failure, and a docs-consistency test that
fails if an endpoint, setting or CLI command is undocumented. `docs/API.md` is the endpoint reference.

```
zylker_pm/
  app.py routes.py     WSGI app, routing, auth, per-request transactions, security headers
  services/            all business rules (tasks, graph, projects, reports, csvio, ...)
  notify.py            Cliq notifier; db.py schema + migrations; seed.py demo data; cli.py commands
  static/              the single-page UI (index.html, app.js, styles.css)
tests/                 unit, HTTP, CLI and browser tests
```

## Limitations and ideas for later

Dependencies are finish-to-start and within one project; there are no custom fields on tasks (only on projects), task templates or
attachments; custom project fields cannot be reordered or filtered on yet; recurring tasks have no yearly or n-th-weekday schedules and make one task per date (no subtasks or dependencies); the audit log has no retention policy or alerting; search is available through the API only (the search box in the page still uses the older project and task filters); webhooks are managed through the API only (no admin screen yet), carry the audit event as it is (no templates), and may arrive out of order; API tokens, sprints and project templates are managed through the API (tokens also the command line) with no screen yet, sprints are sized in hours rather than points, and the primary token cannot be given an expiry; the burndown does not remember reopened tasks; the timeline draws no dependency arrows; SSO and per-project read
permissions would be the natural next steps for a larger organisation.

*The files `anand` and `test.ds` at the repository root are pre-existing test artefacts and are not part of the application.*
