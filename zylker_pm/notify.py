"""Optional Zoho Cliq notifications through an incoming-webhook URL.

The endpoint and bot conventions mirror the GitHub Informer workflow: ``ZYLKER_CLIQ_ENDPOINT``
is the webhook URL and, when ``ZYLKER_CLIQ_BOT_UNIQUE_NAME`` is set, it is appended as the
``bot_unique_name`` query parameter. Delivery is best effort on a background thread - a
failing webhook never fails an API request. URLs are never logged (they contain a key).
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

log = logging.getLogger("zylker.notify")

MAX_MESSAGE = 4800  # Cliq rejects messages above 5000 characters
_MARKUP = re.compile(r"[*_~`\[\]{}<>]")


class Notifier:
    def send(self, text: str) -> None:
        raise NotImplementedError

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until queued messages have been attempted (tests / CLI exit)."""


class NullNotifier(Notifier):
    def send(self, text: str) -> None:
        return None


class RecordingNotifier(Notifier):
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


def with_bot(endpoint: str, bot_unique_name: str = "") -> str:
    if not bot_unique_name:
        return endpoint
    parts = urlparse(endpoint)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "bot_unique_name" for k, _ in query):
        query.append(("bot_unique_name", bot_unique_name))
    return urlunparse(parts._replace(query=urlencode(query)))


class CliqNotifier(Notifier):
    def __init__(self, endpoint, bot_unique_name="", timeout=5.0, opener=None):
        self._url = with_bot(endpoint, bot_unique_name)
        self._timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        threading.Thread(target=self._run, daemon=True, name="cliq-notifier").start()

    def send(self, text: str) -> None:
        try:
            self._queue.put_nowait(text[:MAX_MESSAGE])
        except queue.Full:
            log.warning("Cliq notification queue is full; dropping a message")

    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def _run(self):
        while True:
            text = self._queue.get()
            try:
                request = urllib.request.Request(
                    self._url,
                    data=json.dumps({"text": text}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self._open(request, timeout=self._timeout) as response:
                    response.read()
            except Exception as exc:  # noqa: BLE001 - delivery must never raise
                log.warning("Cliq notification failed (%s %s)", type(exc).__name__, getattr(exc, "code", ""))
            finally:
                self._queue.task_done()


def build_notifier(config) -> Notifier:
    if config.cliq_endpoint:
        return CliqNotifier(config.cliq_endpoint, config.cliq_bot_unique_name)
    return NullNotifier()


# ------------------------------------------------------------------------ rendering


def _clean(text) -> str:
    """User text is stripped of Cliq markup so titles cannot inject formatting or links."""
    return _MARKUP.sub("", str(text or "")).strip()


def _task(key, title, public_url):
    label = "*%s*" % key
    if public_url:
        label = "[%s](%s/#/t/%s)" % (key, public_url, key)
    return "%s %s" % (label, _clean(title))


def _watching(p) -> str:
    """Trailing line naming the people who follow the task (added to whichever message covers the change)."""
    names = [_clean(n) for n in p.get("watchers") or [] if _clean(n)]
    return "\n\U0001f440 Watching: %s" % ", ".join(names) if names else ""


def _key_label(key, public_url):
    return "[%s](%s/#/t/%s)" % (key, public_url, key) if public_url else "*%s*" % key


def render(event: str, p: dict, public_url: str = "") -> str | None:
    task = _task(p["key"], p["title"], public_url) if "key" in p else ""
    if event == "task_assigned":
        extra = " \u00b7 priority %s" % p["priority"] if p.get("priority") else ""
        extra += " \u00b7 due %s" % p["due_date"] if p.get("due_date") else ""
        return "\U0001f4cc %s\nAssigned to *%s* by %s%s%s" % (task, _clean(p["assignee"]), _clean(p["actor"]), extra, _watching(p))
    if event == "task_status":
        if p["new"] == "done":
            return "\u2705 %s \u2014 completed by %s%s" % (task, _clean(p["actor"]), _watching(p))
        if p["new"] == "blocked":
            return "\u26d4 %s \u2014 marked blocked by %s%s" % (task, _clean(p["actor"]), _watching(p))
        return None
    if event == "task_watched":
        return "\U0001f4dd %s\n%s: %s%s" % (task, _clean(p["actor"]), _clean(p["summary"]), _watching(p))
    if event == "bulk_update":
        shown = [_key_label(k, public_url) for k in p["keys"][:10]]
        more = len(p["keys"]) - len(shown)
        return "\U0001f5c2 %s updated %s: %s\n%s%s%s" % (
            _clean(p["actor"]), "1 task" if len(p["keys"]) == 1 else "%d tasks" % len(p["keys"]), _clean(p["summary"]),
            ", ".join(shown), " and %d more" % more if more > 0 else "", _watching(p),
        )  # fmt: skip
    if event == "mention":
        return "\U0001f4ac %s mentioned %s on %s: \u201c%s\u201d" % (
            _clean(p["actor"]), _clean(p["mentioned"]), task, _clean(p["excerpt"]),
        )  # fmt: skip
    if event == "project_status_changed":
        return "\U0001f4c1 Project *%s* (%s) moved from %s to %s by %s" % (
            _clean(p["name"]), p["project"], p["old"], p["new"], _clean(p["actor"]),
        )  # fmt: skip
    return None


def render_digest(report: dict, public_url: str = "") -> str:
    lines = ["\U0001f4c5 *Task digest for %s*" % report["as_of"]]
    if not report["groups"]:
        lines.append("Nothing overdue or due in the next %d day(s). \U0001f389" % report["days"])
    for group in report["groups"]:
        who = _clean(group["assignee"]["display_name"]) if group["assignee"] else "Unassigned"
        lines.append("\n*%s*" % who)
        for item in group["overdue"]:
            lines.append("\U0001f534 %s \u2014 %d day(s) overdue" % (_task(item["key"], item["title"], public_url), item["days_overdue"]))
        for item in group["due_soon"]:
            when = "today" if item["days_until_due"] == 0 else "in %d day(s)" % item["days_until_due"]
            lines.append("\U0001f7e1 %s \u2014 due %s" % (_task(item["key"], item["title"], public_url), when))
    lines.append("\n%d overdue \u00b7 %d due soon" % (report["overdue_count"], report["due_soon_count"]))
    return "\n".join(lines)[:MAX_MESSAGE]


def render_projection(report: dict, public_url: str = "") -> str:
    """The early-warning message built by ``projection.early_warning`` (user text is stripped of Cliq markup, like everywhere else)."""
    lines = ["\u23f1 *Delivery projection for %s*" % report["as_of"]]
    if not report["projects"]:
        lines.append("Everything is projected to finish on time. \U0001f389")
    for project in report["projects"]:
        icon = "\U0001f534" if project["verdict"] == "will_miss" else "\U0001f7e1"
        lines.append("\n%s *%s* (%s) \u2014 %s" % (icon, _clean(project["name"]), project["project"], _clean(project["headline"])))
        for t in project["tasks"]:
            if t["projected_finish"]:
                timing = "due %s, projected %s" % (t["due_date"] or "no date", t["projected_finish"]) + (" (+%dd)" % t["late_by_days"] if t["late_by_days"] else "")
            else:
                timing = "cannot be scheduled"
            lines.append("  \u2022 %s \u2014 %s. %s" % (_task(t["key"], t["title"], public_url), timing, _clean(t["explanation"])))
        if project["more"]:
            lines.append("  \u2026and %d more" % project["more"])
        if project.get("advice"):
            lines.append("  \U0001f4a1 %s" % _clean(project["advice"]["summary"]))
    if report["on_track"]:
        lines.append("\n\U0001f7e2 On track: %s" % ", ".join(_clean(p["name"]) for p in report["on_track"]))
    return "\n".join(lines)[:MAX_MESSAGE]


def dispatch(notifier: Notifier, events, public_url: str = "") -> None:
    for event, payload in events:
        try:
            text = render(event, payload, public_url)
        except Exception:  # noqa: BLE001
            log.exception("could not render %s notification", event)
            continue
        if text:
            notifier.send(text)
