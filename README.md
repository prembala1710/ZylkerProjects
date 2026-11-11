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
- **Dependencies with deadlock protection** - "blocked by" links between tasks of a project: finish-to-start, start-to-start or
  finish-to-finish, each with an optional lag or lead in days (see [Dependencies](#dependencies)). Loops are refused, including the
  sneaky kind that run through parent/subtask relationships.
- **Checklists, watchers and related tasks** - lightweight tick-box items inside a task, people who follow a task without being its
  assignee, and "relates to" / "duplicates" / "cloned from" links that never block anything (see
  [Checklists, watchers and links](#checklists-watchers-and-links)).
- **Bulk actions** - tick tasks in the List view and change their status, assignee, priority, labels, milestone or due date in one go;
  anything the server refuses is listed with the reason (see [Bulk actions](#bulk-actions)).
- **Boards, lists and a timeline** - drag-and-drop Kanban board, sortable/filterable list, and a Gantt-style timeline with
  milestones, the critical path highlighted and an arrow for every dependency (see [Dependencies](#dependencies)).
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
- **File attachments** - attach screenshots, specs and logs to a task (pick, drop or paste), preview images, and find every file of a
  project on its Files tab; files live inside the database, so a backup captures them (see [File attachments](#file-attachments)).
- **CSV import / export** - bring in a Jira-style export (with a dry run) or take your data out.
- **Microsoft Project import / export** - take a plan to Microsoft Project as an XML file, or bring one in (outline, estimates, dates, people,
  dependencies and milestones) with a dry run; see [Microsoft Project import and export](#microsoft-project-import-and-export).
- **Custom project fields** - admins define extra text, number and URL fields (a client, a budget, a wiki link); managers fill them in
  per project (see [Custom project fields](#custom-project-fields)).

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
| `zylker-pm serve [--host H --port P] [--no-webhook-worker]` | Run the web UI and API (threaded; logs one line per request, never headers or tokens). Also delivers outbound webhooks unless told not to |
| `zylker-pm webhooks-worker [--once]` | Deliver queued outbound webhooks without serving HTTP (for gunicorn / waitress deployments); `--once` delivers what is due and exits |
| `zylker-pm create-user USER --role member --name "..." [--capacity 30]` | Add a person and print their token |
| `zylker-pm rotate-token USER` | Issue a new token; the old one stops working immediately |
| `zylker-pm digest [--days 3] [--project KEY] [--post]` | Print the overdue / due-soon digest; `--post` sends it to Cliq |
| `zylker-pm projection [--project KEY] [--post] [--fail-if-late]` | Print which projects and tasks are projected to miss their dates, and why; `--post` sends it to Cliq, `--fail-if-late` exits with code 3 when anything is late |
| `zylker-pm backup FILE [--force]` | Write a consistent copy of the database (safe while the server runs) |
| `zylker-pm import-csv FILE --project KEY --as USER [--dry-run] [--add-missing-members]` | Import tasks; exit code 3 means "finished, but some rows had errors" |
| `zylker-pm msp-export --project KEY [-o FILE]` | Write a project as a Microsoft Project XML file (`KEY-msproject.xml` unless told otherwise; `-` writes to standard output) |
| `zylker-pm msp-import FILE --project KEY --as USER [--dry-run] [--add-missing-members]` | Import tasks, milestones and dependencies from such a file (`-` reads standard input); exit code 3 means "finished, but something was left out" |
| `zylker-pm seed-demo` | Load fictional sample data (refuses unless there are no projects) |

Global option: `--db PATH` (otherwise `$ZYLKER_DB`, then `./zylker.db`).

## Configuration

All settings are environment variables; nothing is read from the repository.

| Variable | Default | Meaning |
|---|---|---|
| `ZYLKER_DB` | `zylker.db` | SQLite file |
| `ZYLKER_HOST` | `127.0.0.1` | Listen address. Anything other than loopback prints a warning: put a TLS-terminating reverse proxy in front |
| `ZYLKER_PORT` | `8080` | Listen port |
| `ZYLKER_ATTACHMENT_MAX_MB` | `10` | Largest single attachment in megabytes (fractions allowed, at most 100). An upload is held in memory while it is stored |
| `ZYLKER_ATTACHMENT_QUOTA_MB` | `2048` | Total size of all attachments. Further uploads are refused until files are deleted. `0` means unlimited |
| `ZYLKER_TIMEZONE` | `UTC` | IANA name, e.g. `Asia/Kolkata`. Decides what "today" is for overdue checks, digests and burndown days. Timestamps are always stored in UTC |
| `ZYLKER_PUBLIC_URL` | *(empty)* | Public base URL (`https://pm.example.com`); makes task keys in Cliq messages clickable |
| `ZYLKER_CLIQ_ENDPOINT` | *(empty = notifications off)* | A Cliq **incoming webhook URL**. It contains a key - treat it as a secret. Must be `https` (plain `http` only for localhost) |
| `ZYLKER_CLIQ_BOT_UNIQUE_NAME` | *(empty)* | Post as this bot (appended as `bot_unique_name`, the same convention as the repository's GitHub Informer workflow) |

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

Databases created by an earlier release are upgraded automatically the first time they are opened (schema v1 to v3, one transaction per step;
a failed upgrade changes nothing).

## Checklists, watchers and links

**Checklists.** A task can carry up to 50 checklist items: text and a tick box, in an order you can change (Task dialog -> *Checklist*). Use them for
the small steps that do not deserve a task of their own. They are deliberately *not* part of the workflow: unticked items never stop a task being closed and
ticking everything does not close it. When a step needs an owner, a due date or a gate, make it a subtask. Lists and the board show progress as `2/5`.

**Watchers.** *Watch* a task (Task dialog, right-hand side) to follow it without being the assignee. Anyone signed in, viewers included, can follow or
unfollow themselves; adding other people needs write access to the project. When a watched task changes or gets a comment, the Zoho Cliq message about it ends
with `Watching: ...` naming the followers - the webhook posts to one channel, so this is a mention by name, not a private message, and the person who made the
change is never named. The task list filter `watcher=me` shows everything you follow.

**Related tasks.** *Relates to*, *duplicates* / *is duplicated by* and *is cloned from* / *was cloned to* connect tasks for reference, even across projects (you need
write access to both). They never block anything, take no part in deadlock checks and do not affect health. Two tasks can have one link between them.

## Dependencies

A dependency makes one task (the *blocked* one) wait for another (the *blocker*), and has a type:

| Type | The blocked task cannot... | until the blocker... |
|---|---|---|
| **Finish -> Start** (default) | start | is finished |
| **Start -> Start** | start | has started |
| **Finish -> Finish** | finish (it may start earlier) | is finished |

*Lag* (days, positive) and *lead* (negative) say when the dates *should* fall, for example "start 2 days after the blocker finishes". They never move a task and never block a
status change; the task dialog flags a link with a warning when the planned dates already break it. Cancelled and done blockers never hold anybody up. Existing dependencies
become plain finish-to-start links with no lag. The critical path (effort hours, not dates) follows finish-to-start links only.

**On the timeline** every dependency between two tasks on the chart is an arrow from the blocker's bar to the blocked task's bar: finish -> start leaves the end of the blocker and
enters the start of the blocked task, start -> start joins the two starts, finish -> finish the two ends. A grey arrow holds nothing back right now; an orange one is holding the blocked
task back (the blocker is not finished, or for start -> start not started); a red dashed one means the blocked task is still open and its planned dates break the link - the same
warning the task dialog gives - and is also listed under the chart in words, with a link to each task. Hovering over a row, or tabbing to it, picks out its arrows and the tooltip of
its bar names what it waits for and what it holds up; the *Dependency arrows* box switches the arrows off. Links to a task that is not on the chart (no dates, cancelled, or a parent
task) are counted under the chart but not drawn. `GET /api/projects/{key}/dependencies` returns the same links to anyone who wants to draw them differently.

## Bulk actions

In a project's *List* tab, tick tasks (or the header box for the whole page) and use the bar that appears: pick what to change - status, priority, assignee, milestone,
due date, add a label, remove a label - and *Apply*. Every task goes through the same rules as editing it alone, so refused tasks (a blocked task, a member who is not in
the project, an archived project...) are listed with the reason and the others are saved. The same call is available as `POST /api/tasks/bulk` with up to 100 tasks, several
fields at once and an optional all-or-nothing mode (see `docs/API.md`). Cliq gets one summary message, not one per task.

## Roles

| Role | Can |
|---|---|
| **viewer** | Read everything: projects, tasks, reports, exports |
| **member** | Viewer + create/edit tasks, comment, log time and link dependencies **in projects they belong to** |
| **manager** | Member + everything in *any* project: create and edit projects, manage members and milestones, delete tasks, import CSV and Microsoft Project files, see everyone's timesheet |
| **admin** | Manager + manage users and roles, define custom project fields, delete projects, read the global audit trail |

Project membership also controls who can be *assigned* work. Archived projects are read-only for all roles.
The server is the authority: the UI hides controls a role cannot use, but every rule is enforced in the API.

## Rules the system enforces

**Workflow.** `todo -> in_progress -> in_review -> done`, plus `blocked` and `cancelled`; done and cancelled tasks can be
reopened to `todo` (and done to `in_progress`). The complete map is returned by `GET /api/meta`. Moving a task to
`in_progress`, `in_review` or `done` is refused while a dependency still holds it back - by default a blocker that is not finished; start-to-start
and finish-to-finish dependencies hold back less (see [Dependencies](#dependencies)). Done or cancelled blockers never block.
Setting a task to `blocked` is always allowed - it is the manual "waiting on something outside the system" flag.

**Hierarchy.** Subtasks nest up to five levels. A parent cannot be closed while a subtask is open, an open subtask cannot be
added under (or reopened beneath) a closed parent, and deleting a parent needs `?cascade=true`.

**Dependencies.** Same project only. A *deadlock* is a cycle in the combined graph of dependencies and "child must finish before
parent". The graph is built from *start* and *finish* events of each task, so the three dependency types combine correctly (B starts after A starts
while A finishes after B finishes is fine; A finishes before B starts while B starts before A starts is not). Checking the combined graph is what
catches `parent -> X -> child` loops: a parent can never be made to wait for its own subtask. The refusal names the loop, e.g. `WEB-3 -> WEB-1 -> WEB-2 -> WEB-3`.

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
- a task someone watches changes or is commented on (the message names the watchers)
- a bulk action changes several tasks (one summary message, not one per task)
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

## Outbound webhooks

Webhooks let other systems react to what happens here: a build server, a chat relay, a spreadsheet, another Zylker. An admin adds
one under **Webhooks** in the sidebar (or with `POST /api/webhooks`), chooses the events and, optionally, the projects, and the
server sends a signed JSON `POST` for every matching change:

```json
{"id": "evt_412", "event": "task.status_changed", "created_at": "2026-10-16T09:00:00Z",
 "project": {"key": "WEB", "name": "Website Redesign"},
 "task": {"key": "WEB-5", "title": "Homepage layout", "status": "in_review", "priority": "high", "type": "task", "assignee": "bob", "due_date": "2026-10-30"},
 "actor": {"username": "alice", "display_name": "Alice Morgan"},
 "details": {"changes": {"status": ["in_progress", "in_review"]}}}
```

- **No lost events, no phantom ones.** An event is written to a queue in the same database transaction as the change it describes, so a
  request that fails or rolls back announces nothing and one that succeeds cannot lose its event. A background worker sends the queue.
- **Signed.** `X-Zylker-Signature: v1=<HMAC-SHA256 of "timestamp.body">` with a per-webhook secret that is shown once; the signed
  timestamp lets receivers reject replays. `docs/API.md` has verification code for Python and Node.
- **Retried and logged.** Anything but a 2xx answer (or a timeout, DNS, TLS or connection error) is retried after 30 s, 2 min,
  10 min, 1 h and 6 h. Every delivery is in the log (UI: **Deliveries**), where a finished one can be sent again, and **Send test**
  checks an endpoint end to end.
- **Safe to point at the internet.** Only `http(s)`, no credentials in the URL, redirects are never followed, and a host that
  resolves to a loopback, private, link-local (cloud metadata) or otherwise non-public address is refused. The address is checked
  when the delivery is made and the connection goes to the address that was checked, so DNS tricks do not help.

| Variable | Default | Meaning |
|---|---|---|
| `ZYLKER_WEBHOOK_ALLOW_PRIVATE` | *(off)* | `1` lets webhooks target loopback and private-network addresses (an internal receiver, local testing) |
| `ZYLKER_WEBHOOK_TIMEOUT` | `10` | Seconds each network step of one delivery attempt may take (1-60) |

**The worker.** `zylker-pm serve` starts one. Under gunicorn or waitress run `zylker-pm webhooks-worker` as a second process
(`--once` delivers what is due and exits, for cron). The Webhooks page warns when no worker has checked in lately. Several workers
are safe: a delivery is claimed with a lease so two workers never send the same one, and a delivery whose worker died mid-send is
picked up again.

**Good to know.** Delivery is at-least-once and not ordered, so receivers should de-duplicate on the payload `id`. A webhook secret
has to be stored readable to sign with it: protect `*.db` files and backups like a token store. Events are the audit-trail entries;
comment text is deliberately left out (fetch it through the API), and user administration and webhook changes are never delivered.
New webhook tables are created automatically when the app starts. Delivery is tested against local stand-in servers (including TLS),
**not** against a particular third-party service: use **Send test** after setting one up.

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

## Microsoft Project import and export

Plans move between this app and Microsoft Project, and other planning tools that read its XML interchange format (MSPDI), as XML files.
In Microsoft Project save the plan with *File > Save As > XML Format*; the binary `.mpp` format is not read.

- **Export:** **Project settings -> Microsoft Project -> Export for Microsoft Project** (or `GET /api/projects/KEY/export/msproject`, or
  `zylker-pm msp-export --project KEY`). Everybody who can see the project can export it.
- **Import:** **Project settings -> Microsoft Project -> Import from Microsoft Project** (managers and admins; or `msp-import`). Like the CSV
  import it starts with a dry run (ticked by default), imports every task it can and lists the rest as `UID 7: ...` (the task's id in the file);
  exit code 3 means "finished, but something was left out".

| Here | In the file | Coming back in |
|---|---|---|
| title, description | Name, Notes | The name on one line (200 characters at most), the notes up to 20,000 |
| parents and subtasks | OutlineLevel and WBS; a task with subtasks is a summary task | Subtasks up to five levels deep (deeper ones move up a level, with a warning). A parent at the top level becomes an *epic*, one below it a *story*, the rest *tasks* |
| status | PercentComplete: to do 0, blocked 0, in progress 50, in review 80, done 100; cancelled is an inactive task | 0 is to do, 1-99 in progress, 100 done, inactive cancelled. A parent's status follows its subtasks, so *blocked* and *in review* come back as to do and in progress |
| priority | 250 low, 500 medium, 750 high, 950 critical | Below 500 low, 500-599 medium, 600-899 high, 900 and up critical |
| estimate | Duration and Work in hours (8 hours are a day); no estimate is Project's "1 day?" placeholder | Work, otherwise Duration; the placeholder and zero are no estimate |
| start date, due date | The constraint *Start No Earlier Than* and the Deadline. Start and Finish are worked out on a Monday-Friday 08:00-17:00 calendar from the estimates and are only a suggestion | From a file this app wrote: the constraint date and the Deadline only, so dates are never invented. From any other file: the constraint date or Start, and the Deadline or Finish |
| assignee | A resource (name, e-mail, capacity) and an assignment | Matched by e-mail, then by name, to an active user; with several people on a task the one with the most work is kept. Somebody who is not a project member is left unassigned (and reported) unless *Add missing people as members* is ticked |
| dependencies | Predecessor links: finish-to-start, start-to-start, finish-to-finish, with the lag in days | The same three. Start-to-finish links and links to other project files are reported and skipped; lags in other units become whole working days |
| milestones | A zero-length milestone task at the milestone's date; the tasks that belong to it are its predecessors | Milestone tasks become milestones (one with the same name and date is reused); tasks that lead into it join it; dependencies through a milestone are followed and their lags added |

**What is not carried over:** task types (they follow the outline), labels, comments, checklists, watchers, related-task links, time entries,
attachments, and custom fields; and from Project baselines, calendars, costs, resource leveling and anything else the table does not list.
A pair of tasks keeps one dependency, so a second one the other way round is reported instead of imported.

**Importing only ever adds.** A task that is already in the project is never edited or removed, so importing the same file twice creates
nothing the second time: tasks are recognised by an `external_ref` of the form `msp:<file id>:<UID>`, and a file exported from the same project is
matched by UID (UID 12 is `KEY-12`). Export, plan further in Project, import again, and only the tasks added in Project arrive, joined to the
existing ones. Dependencies follow the rules of the app: one that would close a loop - even through the outline, such as a subtask that waits for
its own parent to finish - is refused and reported, and the rest still come in. A file may hold up to 5,000 tasks and 5 MB.

**The file is not trusted.** It is read with a streaming parser that refuses a `DOCTYPE` and entity declarations (so there is nothing to expand
and nothing outside the file is ever read), limits nesting and the number of elements, keeps only the elements it needs, and every value goes
through the same validation as the API. Text from the file is shown as text, never as markup.

**How it was checked.** The export follows the element order of the MSPDI schema (a test compares it with the order the MPXJ library uses), and the
importer reads a plan that MPXJ wrote (`tests/fixtures/mpxj_sample.xml`, made by `MakeMpxjSample.java` next to it). An export was also read back with
MPXJ by hand when this was written; the test suite has no Java and does not repeat that. Nothing here has been opened in Microsoft Project itself, so
use a dry run and open the exported file in your own version of Project before relying on either direction.

## File attachments

Attach screenshots, specs and logs to the task they belong to: **task dialog -> Attachments -> Attach files**, or drop files anywhere in the
window while the task is open, or paste a screenshot (Ctrl+V / Cmd+V). Images get a **Preview** button, every file a **Download** button, and a
project's **Files** tab lists all of its files with the task each belongs to, searchable by name.

- **Who:** anyone signed in can read and download. Members of the project (and managers and admins) can attach. The person who attached a
  file, or a manager, can delete it. Archived projects are read-only.
- **Limits:** 10 MB per file (`ZYLKER_ATTACHMENT_MAX_MB`, up to 100), 25 files per task, 2,048 MB for everything
  (`ZYLKER_ATTACHMENT_QUOTA_MB`, `0` = unlimited). Empty files are refused. The UI checks the size before sending; the server enforces it.
- **Where files live:** inside the database (two extra tables, created automatically the first time the new version starts - no migration
  step). `zylker-pm backup` therefore includes them, and deleting a task or project deletes its files in the same transaction.
- **Audited:** `attachment.added` and `attachment.deleted` appear in the task's history and the project's activity feed (name and size, never content).
- **From scripts:** the request body is the file itself, not a multipart form (see the [API reference](docs/API.md#attachments)):

  ```bash
  curl -X POST "http://127.0.0.1:8080/api/tasks/WEB-5/attachments?filename=login-error.png" \
       -H "Authorization: Bearer $TOKEN" -H "Content-Type: image/png" -H "Expect:" --data-binary @login-error.png
  ```

Not included: virus scanning, file versions, thumbnails in lists, renaming (delete and attach again) and images embedded in comments.

## Operations

- **Backups:** `zylker-pm backup /backups/zylker-$(date +%F).db` uses SQLite's online backup, so it is safe while the server runs.
  Backups contain real project data and token *hashes*; protect them like the database. `.gitignore` keeps `*.db` files out of git.
- **Deploying:** run behind a reverse proxy that terminates TLS and (if exposed beyond a trusted network) rate-limits. The app
  speaks plain HTTP, sends no HSTS header, and has no CORS (same-origin only). It is also a standard WSGI callable
  (`zylker_pm.app.App`) if you prefer gunicorn or waitress.
- **Scale:** SQLite in WAL mode with one writer at a time suits a team or department on a single host. It has **not** been load-tested.
- **Attachments and size:** files are stored in the database, so the file grows with them and so does every backup. An upload is held in memory
  while it is stored (allow about the per-file limit per concurrent upload). Deleting files frees space inside the database but does not shrink
  the file: run `sqlite3 zylker.db VACUUM` while the server is stopped to reclaim it. `zylker-pm serve` disconnects a client that has been silent
  for 60 seconds, so a stalled upload cannot hold a thread for ever.
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
- **Attachments are inert.** A stored file is only ever served as a download (`Content-Disposition: attachment`, `nosniff`, the usual
  Content-Security-Policy); the UI saves it as an opaque octet stream and never opens it. The one thing that is displayed is a PNG, JPEG, GIF or
  WebP whose first bytes really are that image (the claimed type is ignored), shown through a `data:` URL in an `<img>`; SVG is never previewed.
  File names are reduced to a plain name (no directories, control or direction-override characters) and shown as text. An upload body is read
  after authentication but before the database's write lock is taken, and half an upload is never stored.
- **Imported files are untrusted.** A Microsoft Project file is parsed without ever expanding entities or reading anything outside the request
  (a `DOCTYPE` is refused), with limits on size, nesting and element counts, and every value is validated like an API request; names from the file
  are shown as text.
- **Not covered:** single sign-on (Zoho or otherwise), token expiry, failed-login throttling (tokens are unguessable, but throttle at
  the proxy anyway), e-mail notifications, per-project read restrictions (every signed-in user can read every project and its attachments), malware scanning of attachments.

## Development

```bash
python -m unittest discover -s tests -v     # ~370 tests, no dependencies
pip install playwright && playwright install chromium
python -m unittest tests.test_ui_e2e -v      # 37 real-browser tests (skipped automatically without Playwright)
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

Dependencies are within one project, and lag/lead only flags dates - it never reschedules anything, and the critical path (effort hours) follows finish-to-start links only; related-task links are
informational and cannot be listed or filtered across the whole organisation yet; checklist items have no assignee or date and are not in the CSV export; watchers are named in the shared Cliq channel
rather than messaged privately, and nobody is made a watcher automatically; bulk actions work from the List tab (not the board) and cover status, assignee, priority, labels, milestone and due date only;
there are no custom fields on tasks (only on projects), recurring tasks or task templates; custom project fields cannot be reordered or filtered on yet; the burndown does not remember reopened
tasks; the timeline's dependency arrows cannot be clicked and join only tasks that are on the chart, so a link to a parent task is counted but not drawn; SSO and per-project read permissions would be the natural next steps for a larger organisation.

*The files `anand` and `test.ds` at the repository root are pre-existing test artefacts and are not part of the application.*
