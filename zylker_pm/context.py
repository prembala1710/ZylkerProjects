"""Per-request context passed to every service function."""

from __future__ import annotations

from .domain import ROLE_RANK
from .errors import Forbidden
from .util import iso


class Context:
    """Bundles the DB connection, acting user and clock for one request.

    ``user`` is None for system actions (CLI bootstrap, seeding); services treat
    that as fully privileged. Events are collected here and only delivered to
    the notifier after the surrounding transaction commits.
    """

    def __init__(self, conn, user, now, tz, config):
        self.conn = conn
        self.user = user
        self.now = now
        self.tz = tz
        self.config = config
        self.events: list[tuple[str, dict]] = []

    @property
    def now_iso(self) -> str:
        return iso(self.now)

    @property
    def today(self):
        return self.now.astimezone(self.tz).date()

    @property
    def actor_id(self):
        return self.user["id"] if self.user is not None else None

    @property
    def actor_name(self) -> str:
        return self.user["display_name"] if self.user is not None else "System"

    def emit(self, event: str, **payload) -> None:
        self.events.append((event, payload))

    def require_role(self, role: str) -> None:
        if self.user is None:
            return
        if ROLE_RANK[self.user["role"]] < ROLE_RANK[role]:
            raise Forbidden("This action requires the %r role or higher" % role, code="insufficient_role")

    def has_role(self, role: str) -> bool:
        return self.user is None or ROLE_RANK[self.user["role"]] >= ROLE_RANK[role]
