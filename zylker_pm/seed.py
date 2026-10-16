"""Fictional sample data for trying the UI (``zylker-pm seed-demo``).

Everything here is invented: no real people, projects or tickets. Dates are relative to
"today" and the clock is wound back while seeding so burndown charts and activity feeds
look lived-in. Refuses to run unless the database has no projects yet.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone

from . import notify
from .services import collab, deps, milestones, projects, tasks, users

DEMO_USERS = [
    ("alice", "Alice Morgan", "manager", 40),
    ("bob", "Bob Chen", "member", 40),
    ("carol", "Carol Diaz", "member", 20),  # part-time: makes the workload view interesting
    ("dave", "Dave Okafor", "member", 40),
    ("erin", "Erin Walsh", "viewer", 40),
]


def seed_demo(app):
    """Create the demo organisation. Returns ``{username: token}`` for the users created."""
    saved_clock, saved_notifier = app.clock, app.notifier
    app.notifier = notify.NullNotifier()  # never ping a real Cliq channel with fake data
    try:
        return _seed(app, saved_clock().astimezone(app.tz).date())
    finally:
        app.clock, app.notifier = saved_clock, saved_notifier


def _seed(app, today):
    def d(offset):
        return (today + timedelta(days=offset)).isoformat()

    def at(offset, hour=9):
        moment = datetime.combine(today + timedelta(days=offset), time(hour), tzinfo=app.tz).astimezone(timezone.utc)
        app.clock = lambda: moment

    tokens, rows = {}, {}
    at(-45)
    with app.session() as ctx:
        if ctx.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]:
            raise RuntimeError("Refusing to seed demo data: the database already contains projects")
        for username, name, role, capacity in DEMO_USERS:
            if users.find_by_username(ctx.conn, username) is None:
                _, tokens[username] = users.create_user(
                    ctx, {"username": username, "display_name": name, "role": role, "weekly_capacity_hours": capacity}
                )
            rows[username] = users.find_by_username(ctx.conn, username)

    @contextmanager
    def act(username, offset):
        at(offset)
        with app.session(user=rows[username]) as ctx:
            yield ctx

    def create(ctx, project, title, **fields):
        return tasks.create_task(ctx, project, {"title": title, **fields})["key"]

    def move(username, offset, key, status):
        with act(username, offset) as ctx:
            tasks.update_task(ctx, key, {"status": status})

    def block(username, offset, key, blocked_by):
        with act(username, offset) as ctx:
            deps.add_dependency(ctx, key, {"blocked_by": blocked_by})

    def log(username, offset, key, hours, note=""):
        with act(username, offset) as ctx:
            collab.log_time(ctx, key, {"hours": hours, "work_date": d(offset), "note": note})

    def comment(username, offset, key, body):
        with act(username, offset) as ctx:
            collab.add_comment(ctx, key, {"body": body})

    # ------------------------------------------------------------------ WEB: at risk
    with act("alice", -30) as ctx:
        projects.create_project(ctx, {
            "key": "WEB", "name": "Website Redesign", "start_date": d(-30), "due_date": d(30),
            "description": "Replace the marketing site with the new design system and an accessible, faster build.",
            "members": ["bob", "carol", "dave"],
        })  # fmt: skip
        m_design = milestones.create_milestone(ctx, "WEB", {"name": "Design sign-off", "due_date": d(-5)})["id"]
        m_beta = milestones.create_milestone(ctx, "WEB", {"name": "Beta launch", "due_date": d(14)})["id"]
        m_public = milestones.create_milestone(ctx, "WEB", {"name": "Public launch", "due_date": d(30)})["id"]
        epic = create(ctx, "WEB", "Design system", type="epic", priority="high", due_date=d(5))
        create(ctx, "WEB", "Audit current site", assignee="bob", estimate_hours=8, due_date=d(-20), parent=epic, milestone=m_design, labels=["research"])
        create(ctx, "WEB", "Define colour and type tokens", assignee="carol", estimate_hours=6, due_date=d(-12), parent=epic, milestone=m_design, labels=["design"])
    with act("alice", -28) as ctx:
        create(ctx, "WEB", "Build component library", type="story", priority="high", assignee="carol", estimate_hours=24, due_date=d(3), parent=epic, milestone=m_beta, labels=["frontend"])
        create(ctx, "WEB", "Homepage layout", type="story", priority="high", assignee="bob", estimate_hours=12, due_date=d(-1), milestone=m_beta, labels=["frontend", "design"])
    with act("alice", -20) as ctx:
        create(ctx, "WEB", "Responsive navigation", assignee="dave", estimate_hours=10, due_date=d(7), milestone=m_beta, labels=["frontend"])
        create(ctx, "WEB", "Accessibility pass (WCAG AA)", priority="high", estimate_hours=16, due_date=d(10), milestone=m_public, labels=["a11y"])
        create(ctx, "WEB", "Fix: contact form loses attachments", type="bug", priority="high", assignee="dave", estimate_hours=4, due_date=d(2), milestone=m_beta, labels=["bug"])
        create(ctx, "WEB", "Performance budget and CI check", assignee="bob", estimate_hours=6, due_date=d(14), milestone=m_public, labels=["perf"])
        create(ctx, "WEB", "Launch checklist", priority="low", assignee="alice", estimate_hours=3, due_date=d(28), milestone=m_public)
        create(ctx, "WEB", "Write migration runbook", assignee="carol", estimate_hours=5, due_date=d(20), milestone=m_public, labels=["docs"])
        create(ctx, "WEB", "Legacy blog import", estimate_hours=9)
    block("alice", -20, "WEB-3", "WEB-2")
    block("alice", -20, "WEB-4", "WEB-3")
    block("alice", -20, "WEB-6", "WEB-4")
    block("alice", -20, "WEB-7", "WEB-5")
    block("alice", -20, "WEB-7", "WEB-6")
    block("alice", -20, "WEB-10", "WEB-7")
    move("bob", -22, "WEB-2", "done")
    move("carol", -14, "WEB-3", "done")
    move("carol", -10, "WEB-4", "in_progress")
    move("bob", -8, "WEB-5", "in_progress")
    move("bob", -1, "WEB-5", "in_review")
    move("dave", -3, "WEB-8", "blocked")
    move("alice", -5, "WEB-12", "cancelled")
    log("bob", -23, "WEB-2", 8, "site inventory")
    log("carol", -15, "WEB-3", 6, "tokens")
    for offset, hours in ((-9, 8), (-6, 7), (-2, 6)):
        log("carol", offset, "WEB-4", hours, "buttons, inputs, cards")
    for offset, hours in ((-6, 6), (-3, 5), (-1, 3)):
        log("bob", offset, "WEB-5", hours, "hero + grid")
    log("dave", -3, "WEB-8", 2, "reproduced; upload limit")
    comment("bob", -1, "WEB-5", "Hero and grid are ready for review. @alice could you take a look?")
    comment("alice", 0, "WEB-5", "Nice work. Spacing on mobile needs another pass, @bob.")
    comment("dave", -3, "WEB-8", "Blocked on storage bucket permissions. @carol do you have access?")

    # ------------------------------------------------------------- APP: off track
    with act("alice", -45) as ctx:
        projects.create_project(ctx, {
            "key": "APP", "name": "Mobile App v2", "start_date": d(-45), "due_date": d(10),
            "description": "Second major release of the mobile app: offline mode, push notifications and a new onboarding.",
            "members": ["carol", "dave"],
        })  # fmt: skip
        m_beta = milestones.create_milestone(ctx, "APP", {"name": "TestFlight beta", "due_date": d(-7)})["id"]
        m_store = milestones.create_milestone(ctx, "APP", {"name": "Store submission", "due_date": d(10)})["id"]
        create(ctx, "APP", "Offline sync engine", type="story", priority="critical", assignee="dave", estimate_hours=40, due_date=d(-3), milestone=m_beta, labels=["backend"])
        create(ctx, "APP", "Push notifications", type="story", priority="high", assignee="carol", estimate_hours=30, due_date=d(2), milestone=m_store, labels=["backend"])
        create(ctx, "APP", "Onboarding flow", type="story", assignee="carol", estimate_hours=12, due_date=d(-15), milestone=m_beta, labels=["ux"])
        create(ctx, "APP", "App store assets", estimate_hours=6, due_date=d(8), milestone=m_store, labels=["marketing"])
        create(ctx, "APP", "Crash reporting integration", assignee="dave", estimate_hours=4, due_date=d(-10), milestone=m_beta)
        create(ctx, "APP", "Beta feedback fixes", type="bug", priority="high", assignee="carol", estimate_hours=10, due_date=d(5), milestone=m_store, labels=["bug"])
        create(ctx, "APP", "Release notes", priority="low", assignee="alice", estimate_hours=2, due_date=d(9), milestone=m_store, labels=["docs"])
    block("alice", -45, "APP-2", "APP-1")
    block("alice", -45, "APP-7", "APP-6")
    move("carol", -16, "APP-3", "done")
    move("dave", -9, "APP-5", "done")
    move("dave", -30, "APP-1", "in_progress")
    for offset, hours in ((-20, 16), (-10, 14), (-4, 8)):
        log("dave", offset, "APP-1", hours, "sync conflict resolution")
    log("carol", -17, "APP-3", 12, "flows and copy")
    comment("dave", -4, "APP-1", "Conflict resolution is harder than estimated. Two more days at least. @alice")

    # ------------------------------------------------------- INT: planning, on track
    with act("alice", -2) as ctx:
        projects.create_project(ctx, {
            "key": "INT", "name": "Cliq Integrations", "status": "planning", "start_date": d(7), "due_date": d(60),
            "description": "Scope and build the next set of chat integrations.", "members": ["bob"],
        })  # fmt: skip
        create(ctx, "INT", "Scope the integration surface", assignee="bob", estimate_hours=6, due_date=d(10))
        create(ctx, "INT", "Spike: bot authentication flow", assignee="bob", estimate_hours=8, due_date=d(14))
        create(ctx, "INT", "Design message cards", assignee="alice", estimate_hours=5, due_date=d(21))
        create(ctx, "INT", "Draft rollout plan", priority="low")
    block("alice", -2, "INT-2", "INT-1")

    at(0)
    return tokens
