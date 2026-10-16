"""Command line interface: ``python -m zylker_pm <command>``."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sqlite3
import sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from . import __version__, notify
from .app import App
from .config import LOOPBACK_HOSTS, Config
from .db import connect, init_db
from .domain import ROLES
from .errors import ApiError
from .services import csvio, reports, users


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - the app writes its own access log
        return


def make_threaded_server(app, host, port):
    return make_server(host, port, app, server_class=ThreadingWSGIServer, handler_class=QuietHandler)


def _show_token(username, token):
    print("API token for %r (shown once - store it somewhere safe):\n  %s" % (username, token))


def cmd_init(args, cfg):
    init_db(cfg.db_path)
    print("Database ready: %s" % cfg.db_path)
    if not args.admin:
        return 0
    app = App(cfg)
    with app.session() as ctx:
        if users.find_by_username(ctx.conn, args.admin):
            print("User %r already exists; nothing changed." % args.admin)
            return 0
        _, token = users.create_user(
            ctx, {"username": args.admin, "display_name": args.name or args.admin, "role": "admin", **({"email": args.email} if args.email else {})}
        )
    print("Created admin %r." % args.admin)
    _show_token(args.admin, token)
    return 0


def cmd_serve(args, cfg):
    init_db(cfg.db_path)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = App(cfg)
    if cfg.host not in LOOPBACK_HOSTS:
        print(
            "WARNING: listening on %s without TLS. Put a TLS-terminating reverse proxy in front "
            "before exposing this beyond a trusted network." % cfg.host,
            file=sys.stderr,
        )
    server = make_threaded_server(app, cfg.host, cfg.port)
    print("Zylker Projects %s on http://%s:%d  (db: %s, timezone: %s, Cliq: %s)" % (
        __version__, cfg.host, cfg.port, cfg.db_path, cfg.timezone, "on" if cfg.cliq_endpoint else "off"))  # fmt: skip
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


def cmd_create_user(args, cfg):
    app = App(cfg)
    body = {"username": args.username, "display_name": args.name or args.username, "role": args.role}
    if args.email:
        body["email"] = args.email
    if args.capacity is not None:
        body["weekly_capacity_hours"] = args.capacity
    with app.session() as ctx:
        user, token = users.create_user(ctx, body)
    print("Created %s %r." % (user["role"], user["username"]))
    _show_token(user["username"], token)
    return 0


def cmd_rotate_token(args, cfg):
    app = App(cfg)
    with app.session() as ctx:
        user = users.find_by_username(ctx.conn, args.username)
        if user is None:
            print("No such user: %s" % args.username, file=sys.stderr)
            return 1
        token = users.rotate_token(ctx, user["id"])
    _show_token(args.username, token)
    return 0


def cmd_digest(args, cfg):
    app = App(cfg)
    params = {"days": str(args.days)}
    if args.project:
        params["project"] = args.project
    with app.session(write=False) as ctx:
        text = notify.render_digest(reports.digest(ctx, params), cfg.public_url)
    print(text)
    if args.post:
        if not cfg.cliq_endpoint:
            print("\nCannot post: set ZYLKER_CLIQ_ENDPOINT to a Cliq incoming-webhook URL.", file=sys.stderr)
            return 2
        app.notifier.send(text)
        app.notifier.flush(10)
        print("\nPosted to Cliq (delivery is best effort; check the channel).")
    return 0


def cmd_backup(args, cfg):
    import os

    if os.path.exists(args.path) and not args.force:
        print("%s already exists (use --force to overwrite)." % args.path, file=sys.stderr)
        return 1
    if os.path.exists(args.path):
        os.remove(args.path)
    src, dst = connect(cfg.db_path), sqlite3.connect(args.path)
    try:
        src.backup(dst)  # consistent even while the server is running
    finally:
        dst.close()
        src.close()
    print("Backup written to %s" % args.path)
    return 0


def cmd_import_csv(args, cfg):
    app = App(cfg)
    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8-sig").read()
    with app.session() as ctx:
        row = users.find_by_username(ctx.conn, args.as_user)
    if row is None:
        print("No such user: %s" % args.as_user, file=sys.stderr)
        return 1
    with app.session(user=row) as ctx:
        report = csvio.import_csv(ctx, args.project, text, dry_run=args.dry_run, add_missing_members=args.add_missing_members)
    print(json.dumps(report, indent=2))
    return 0 if not report["errors"] else 3


def cmd_seed_demo(args, cfg):
    from .seed import seed_demo

    init_db(cfg.db_path)
    tokens = seed_demo(App(cfg))
    print("Demo data created (clearly fictional: users, projects and tasks for trying the UI).")
    for username, token in tokens.items():
        _show_token(username, token)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="zylker-pm", description="Zylker Projects - project & task management")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", help="SQLite database path (default: $ZYLKER_DB or ./zylker.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the database (and optionally the first admin)")
    p.add_argument("--admin", metavar="USERNAME")
    p.add_argument("--name", help="admin display name")
    p.add_argument("--email")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("serve", help="run the web UI and API")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("create-user", help="create a user and print their API token")
    p.add_argument("username")
    p.add_argument("--name")
    p.add_argument("--email")
    p.add_argument("--role", choices=ROLES, default="member")
    p.add_argument("--capacity", type=float, help="weekly capacity in hours (default 40)")
    p.set_defaults(fn=cmd_create_user)

    p = sub.add_parser("rotate-token", help="issue a new API token for a user (invalidates the old one)")
    p.add_argument("username")
    p.set_defaults(fn=cmd_rotate_token)

    p = sub.add_parser("digest", help="print (and optionally post to Cliq) overdue / due-soon tasks")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--project")
    p.add_argument("--post", action="store_true", help="post to the configured Cliq webhook")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("backup", help="write a consistent copy of the database")
    p.add_argument("path")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("import-csv", help="import tasks from a CSV file (e.g. a Jira export)")
    p.add_argument("file", help="CSV path, or - for stdin")
    p.add_argument("--project", required=True, help="target project key")
    p.add_argument("--as", dest="as_user", required=True, metavar="USERNAME", help="user performing the import")
    p.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    p.add_argument("--add-missing-members", action="store_true", help="add assignees who are not yet project members")
    p.set_defaults(fn=cmd_import_csv)

    p = sub.add_parser("seed-demo", help="load fictional sample data (empty databases only)")
    p.set_defaults(fn=cmd_seed_demo)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = Config.from_env()
        overrides = {}
        if args.db:
            overrides["db_path"] = args.db
        if getattr(args, "host", None):
            overrides["host"] = args.host
        if getattr(args, "port", None):
            overrides["port"] = args.port
        cfg = dataclasses.replace(cfg, **overrides)
        cfg.validate()
        return args.fn(args, cfg)
    except ApiError as exc:
        details = "; ".join("%s: %s" % kv for kv in (exc.details or {}).items())
        print("Error: %s%s" % (exc.message, " (%s)" % details if details else ""), file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
