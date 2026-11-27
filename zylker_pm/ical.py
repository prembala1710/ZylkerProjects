"""A small iCalendar (RFC 5545) writer: just enough to publish all-day events as a subscription feed.

It knows nothing about projects or the database. Callers hand it Event objects and get text back, so the parts calendar
apps are picky about - escaping, line folding and date arithmetic - can be tested on their own.

The rules it keeps, each of which some client enforces:

* every line ends with CRLF and none is longer than 75 octets: longer lines are folded (CRLF + one space) and a fold
  never falls inside a multi-byte UTF-8 sequence;
* TEXT values escape backslash, semicolon, comma and newline and lose every other control character, so a task title can
  never end a line early and inject a property of its own;
* all-day events carry DTSTART;VALUE=DATE and an *exclusive* DTEND (the day after the last day);
* the output is a pure function of its input: nothing in it depends on the moment it was produced, so two requests for the
  same data return the same bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

CRLF = "\r\n"
MAX_OCTETS = 75  # per physical line, not counting the CRLF
PRODID = "-//Zylker Projects//Calendar feed//EN"

# C0 and C1 controls (except tab and newline, handled separately) and the Unicode line/paragraph separators.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_UID_UNSAFE = re.compile(r"[^A-Za-z0-9._@-]")


def escape_text(value) -> str:
    """Escape a TEXT value (RFC 5545 section 3.3.11)."""
    text = "" if value is None else str(value)
    text = text.encode("utf-8", "replace").decode("utf-8")  # a lone surrogate would otherwise break the whole feed
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = _CONTROL.sub("", text)
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Fold one content line so that no physical line exceeds 75 octets (RFC 5545 section 3.1)."""
    if len(line.encode("utf-8")) <= MAX_OCTETS:
        return line
    parts, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > MAX_OCTETS:
            parts.append(current)
            current, size = " ", 1  # a continuation line starts with one space, which counts towards the 75
        current += char
        size += width
    parts.append(current)
    return CRLF.join(parts)


def format_date(day: date) -> str:
    return "%04d%02d%02d" % (day.year, day.month, day.day)


def format_utc(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return "%04d%02d%02dT%02d%02d%02dZ" % (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)


def _uid(value: str) -> str:
    return _UID_UNSAFE.sub("-", str(value))


def _uri(value: str) -> str:
    """A URI value: anything that is not already URL-safe is percent-encoded (spaces, controls, non-ASCII)."""
    return quote(str(value), safe=":/?#[]@!$&'()*+,;=%-._~")


def _duration(minutes: int) -> str:
    return "PT%dH" % (minutes // 60) if minutes % 60 == 0 else "PT%dM" % minutes


@dataclass(frozen=True)
class Event:
    """One all-day event. last_day is inclusive; the writer emits the exclusive DTEND."""

    uid: str
    summary: str
    first_day: date
    last_day: date
    modified: datetime
    description: str = ""
    url: str = ""
    status: str = "CONFIRMED"

    def lines(self) -> list[str]:
        if self.last_day < self.first_day:
            raise ValueError("an event cannot end before it starts")
        out = [
            "BEGIN:VEVENT",
            "UID:" + _uid(self.uid),
            "DTSTAMP:" + format_utc(self.modified),
            "LAST-MODIFIED:" + format_utc(self.modified),
            "DTSTART;VALUE=DATE:" + format_date(self.first_day),
            "DTEND;VALUE=DATE:" + format_date(self.last_day + timedelta(days=1)),
            "SUMMARY:" + escape_text(self.summary),
        ]
        if self.description:
            out.append("DESCRIPTION:" + escape_text(self.description))
        if self.url:
            out.append("URL:" + _uri(self.url))
        # Free, not busy: a task that is due on a day must not mark the whole day as taken in anybody's calendar.
        out += ["STATUS:" + self.status, "TRANSP:TRANSPARENT", "END:VEVENT"]
        return out


def render_calendar(name: str, events, *, description: str = "", refresh_minutes: int = 60) -> str:
    """The complete iCalendar text (CRLF line endings, ending with a CRLF)."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:" + PRODID,
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "NAME:" + escape_text(name),
        "X-WR-CALNAME:" + escape_text(name),
    ]
    if description:
        lines.append("X-WR-CALDESC:" + escape_text(description))
    lines += ["REFRESH-INTERVAL;VALUE=DURATION:" + _duration(refresh_minutes), "X-PUBLISHED-TTL:" + _duration(refresh_minutes)]
    for event in events:
        lines += event.lines()
    lines.append("END:VCALENDAR")
    return CRLF.join(fold(line) for line in lines) + CRLF
