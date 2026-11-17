"""Brute-force protection and per-user rate limiting.

Two independent mechanisms share one small, thread-safe, in-memory store:

* **Failed sign-ins per address.** ``failure_limit`` failures from one address within five minutes block that address
  for a minute; each further block doubles the time (2, 4, 8 ... up to an hour) until the address has been quiet for an
  hour. While an address is blocked, requests from it that carry a *valid* token are still served - a shared office
  address must not be locked out because one visitor mistyped - and only requests that fail to authenticate get
  ``429``. Blocked attempts do not extend the block, so an attacker cannot keep a block alive for free.
* **A request budget per user** (token bucket): ``rate_per_minute`` sustained, with a burst of a fifth of that (at
  least 10). A client that polls too fast gets ``429`` with ``Retry-After`` instead of slowing everyone down.

Everything takes ``now`` (seconds) as an argument, so the behaviour is tested without sleeping. State is per process
and bounded: at most ``MAX_TRACKED`` entries, expired ones dropped first.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from datetime import datetime, timezone
from typing import NamedTuple

FAILURE_WINDOW = 300.0  # seconds over which failures are counted
BLOCK_BASE = 60.0  # the first block
BLOCK_MAX = 3600.0  # blocks double up to this
STRIKE_MEMORY = 3600.0  # an address that stays quiet this long after a block starts again at the first step
MAX_TRACKED = 10_000
UNKNOWN = "unknown"  # the key for requests whose address could not be determined

DEFAULT_FAILURE_LIMIT = 10
DEFAULT_RATE_PER_MINUTE = 600


def burst_for(rate_per_minute: int) -> int:
    return min(rate_per_minute, max(10, rate_per_minute // 5))


class Verdict(NamedTuple):
    outcome: str  # off | counted | blocked_now | blocked
    retry_after: int = 0
    strikes: int = 0
    failures: int = 0


class Block:
    __slots__ = ("until", "strikes", "since")

    def __init__(self, until, strikes, since):
        self.until, self.strikes, self.since = until, strikes, since


class Throttle:
    def __init__(self, failure_limit: int = 0, rate_per_minute: int = 0):
        self.failure_limit = failure_limit
        self.rate_per_minute = rate_per_minute
        self._lock = threading.Lock()
        self._fails: dict[str, deque] = {}
        self._blocks: dict[str, Block] = {}
        self._buckets: dict[object, list] = {}

    # -- failed sign-ins ---------------------------------------------------------------

    def on_failure(self, ip, now: float) -> Verdict:
        """Record a failed authentication and decide what to do. One atomic step: concurrent failures cannot all slip
        in just below the limit."""
        if not self.failure_limit:
            return Verdict("off")
        key = ip or UNKNOWN
        with self._lock:
            block = self._blocks.get(key)
            if block is not None and now < block.until:
                return Verdict("blocked", math.ceil(block.until - now), block.strikes)
            recent = self._fails.setdefault(key, deque())
            while recent and recent[0] <= now - FAILURE_WINDOW:
                recent.popleft()
            recent.append(now)
            if len(recent) < self.failure_limit:
                self._trim(now)
                return Verdict("counted")
            strikes = block.strikes + 1 if block is not None and now < block.until + STRIKE_MEMORY else 1
            seconds = min(BLOCK_BASE * 2 ** (strikes - 1), BLOCK_MAX)
            failures = len(recent)
            self._blocks[key] = Block(now + seconds, strikes, now)
            del self._fails[key]
            self._trim(now)
            return Verdict("blocked_now", math.ceil(seconds), strikes, failures)

    def blocked(self, now: float):
        """Active blocks, longest remaining first."""
        with self._lock:
            rows = [
                {
                    "ip": ip,
                    "retry_after": math.ceil(b.until - now),
                    "blocked_until": datetime.fromtimestamp(b.until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "blocked_since": datetime.fromtimestamp(b.since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "strikes": b.strikes,
                }
                for ip, b in self._blocks.items()
                if now < b.until
            ]
        return sorted(rows, key=lambda r: (-r["retry_after"], r["ip"]))

    def unblock(self, ip=None) -> int:
        """Lift the block (and forget the history) of one address, or of all. Returns how many blocks were lifted."""
        with self._lock:
            keys = [ip] if ip else list(self._blocks)
            removed = 0
            for key in keys:
                if self._blocks.pop(key, None) is not None:
                    removed += 1
                self._fails.pop(key, None)
            return removed

    # -- request budget ----------------------------------------------------------------

    def take(self, user_id, now: float):
        """Spend one request from the user's budget. Returns ``(allowed, retry_after, rate, remaining)``."""
        rate = self.rate_per_minute
        if not rate:
            return True, 0, 0, 0
        capacity, per_second = burst_for(rate), rate / 60.0
        with self._lock:
            tokens, updated = self._buckets.get(user_id, (float(capacity), now))
            tokens = min(float(capacity), tokens + max(0.0, now - updated) * per_second)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[user_id] = [tokens, now]
            self._trim(now)
            retry = 0 if allowed else math.ceil((1.0 - tokens) / per_second)
            return allowed, retry, rate, int(tokens)

    # -- housekeeping ------------------------------------------------------------------

    def _trim(self, now: float) -> None:
        """Keep memory bounded (called with the lock held): expired entries first, then the oldest idle ones.
        Active blocks are the last thing to go."""
        if len(self._fails) + len(self._blocks) + len(self._buckets) <= MAX_TRACKED:
            return
        for key in [k for k, q in self._fails.items() if not q or q[-1] <= now - FAILURE_WINDOW]:
            del self._fails[key]
        for key in [k for k, b in self._blocks.items() if now >= b.until + STRIKE_MEMORY]:
            del self._blocks[key]
        capacity_refill = 3600.0  # an idle bucket is full again long before this
        for key in [k for k, (_, updated) in self._buckets.items() if now - updated >= capacity_refill]:
            del self._buckets[key]
        target = int(MAX_TRACKED * 0.9)
        for store in (self._buckets, self._fails):  # dicts keep insertion order: the first keys are the oldest
            while len(self._fails) + len(self._blocks) + len(self._buckets) > target and store:
                del store[next(iter(store))]
        while len(self._fails) + len(self._blocks) + len(self._buckets) > target and self._blocks:
            del self._blocks[next(iter(self._blocks))]

    def tracked(self) -> int:
        with self._lock:
            return len(self._fails) + len(self._blocks) + len(self._buckets)

    def settings(self) -> dict:
        return {
            "failure_limit": self.failure_limit,
            "failure_window_seconds": int(FAILURE_WINDOW),
            "first_block_seconds": int(BLOCK_BASE),
            "longest_block_seconds": int(BLOCK_MAX),
            "rate_limit_per_minute": self.rate_per_minute,
            "burst": burst_for(self.rate_per_minute) if self.rate_per_minute else 0,
        }
