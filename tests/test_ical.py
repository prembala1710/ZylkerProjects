"""The iCalendar writer on its own: escaping, folding, dates and the shape of the whole calendar."""

import random
import unittest
from datetime import date, datetime, timedelta, timezone

import icalcheck
from helpers import ApiTestCase  # noqa: F401  (also puts the repo root on sys.path)

from zylker_pm import ical

STAMP = datetime(2026, 10, 16, 9, 0, 0, tzinfo=timezone.utc)


def event(**over):
    base = {"uid": "task-WEB-1@zylker-pm", "summary": "WEB-1 Homepage", "first_day": date(2026, 10, 19), "last_day": date(2026, 10, 23), "modified": STAMP}
    base.update(over)
    return ical.Event(**base)


class EscapeTests(unittest.TestCase):
    def test_backslash_semicolon_comma_and_newline_are_escaped(self):
        self.assertEqual(ical.escape_text("a\\b;c,d\ne"), "a\\\\b\\;c\\,d\\ne")

    def test_every_kind_of_line_break_becomes_the_same_escape(self):
        self.assertEqual(ical.escape_text("one\r\ntwo\rthree\nfour"), "one\\ntwo\\nthree\\nfour")

    def test_control_characters_are_dropped_and_tabs_become_spaces(self):
        self.assertEqual(ical.escape_text("a\x00b\x07c\x1bd\x7fe\x85f\u2028g\u2029h\ti"), "abcdefgh i")

    def test_none_is_empty_and_numbers_are_text(self):
        self.assertEqual((ical.escape_text(None), ical.escape_text(12)), ("", "12"))

    def test_a_lone_surrogate_cannot_break_the_feed(self):
        self.assertEqual(ical.escape_text("a\ud800b"), "a?b")

    def test_ordinary_unicode_passes_through(self):
        text = "\u65e5\u672c\u8a9e \u2713 caf\u00e9 \U0001f642"
        self.assertEqual(ical.escape_text(text), text)

    def test_escaping_round_trips_through_the_strict_reader(self):
        for text in ("plain", "a,b;c", "back\\slash", "line\nbreak", "\\n is not a newline", ",;\\\n", "\u2713 \U0001f642"):
            self.assertEqual(icalcheck.unescape(ical.escape_text(text)), text)


class FoldTests(unittest.TestCase):
    def test_a_short_line_is_left_alone(self):
        self.assertEqual(ical.fold("SUMMARY:short"), "SUMMARY:short")

    def test_exactly_75_octets_is_not_folded_and_76_is(self):
        self.assertEqual(ical.fold("x" * 75), "x" * 75)
        self.assertEqual(ical.fold("x" * 76), "x" * 75 + "\r\n x")

    def test_a_fold_never_splits_a_multibyte_character(self):
        line = "x" * 74 + "\u20ac"  # the euro sign is three octets and does not fit into the last one
        self.assertEqual(ical.fold(line), "x" * 74 + "\r\n \u20ac")
        for width in (2, 3, 4):  # one, two and three octets after the 75th: every alignment of a wide character
            char = {2: "\u00e9", 3: "\u20ac", 4: "\U0001f642"}[width]
            for pad in range(70, 76):
                folded = ical.fold("x" * pad + char * 30)
                for part in folded.split("\r\n"):
                    self.assertLessEqual(len(part.encode("utf-8")), 75)
                self.assertEqual(folded.replace("\r\n ", ""), "x" * pad + char * 30)

    def test_random_text_folds_into_short_lines_and_unfolds_to_itself(self):
        rng = random.Random(11)
        alphabet = ["a", "Z", " ", ",", "\u00e9", "\u20ac", "\u65e5", "\U0001f642", "\u2713"]
        for _ in range(300):
            line = "SUMMARY:" + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 400)))
            folded = ical.fold(line)
            parts = folded.split("\r\n")
            self.assertTrue(all(len(p.encode("utf-8")) <= 75 for p in parts))
            self.assertTrue(all(p.startswith(" ") for p in parts[1:]))
            self.assertEqual(parts[0] + "".join(p[1:] for p in parts[1:]), line)


class DateTests(unittest.TestCase):
    def test_dates_are_zero_padded(self):
        self.assertEqual(ical.format_date(date(2026, 1, 5)), "20260105")
        self.assertEqual(ical.format_date(date(999, 1, 2)), "09990102")

    def test_a_timestamp_is_written_in_utc(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(ical.format_utc(datetime(2026, 10, 16, 14, 30, 5, tzinfo=ist)), "20261016T090005Z")

    def test_the_end_of_an_all_day_event_is_the_day_after_its_last_day(self):
        cases = [
            (date(2026, 10, 19), date(2026, 10, 23), "20261024"),  # an ordinary span
            (date(2026, 10, 31), date(2026, 10, 31), "20261101"),  # month end
            (date(2026, 12, 31), date(2026, 12, 31), "20270101"),  # year end
            (date(2027, 2, 28), date(2027, 2, 28), "20270301"),  # no leap day in 2027
            (date(2028, 2, 28), date(2028, 2, 28), "20280229"),  # leap day follows
            (date(2028, 2, 29), date(2028, 2, 29), "20280301"),
            (date(2026, 10, 30), date(2026, 11, 2), "20261103"),  # across a month end
        ]
        for first, last, expected in cases:
            lines = event(first_day=first, last_day=last).lines()
            self.assertIn("DTSTART;VALUE=DATE:" + ical.format_date(first), lines)
            self.assertIn("DTEND;VALUE=DATE:" + expected, lines, (first, last))

    def test_an_event_cannot_end_before_it_starts(self):
        with self.assertRaises(ValueError):
            event(first_day=date(2026, 10, 20), last_day=date(2026, 10, 19)).lines()


class RenderTests(unittest.TestCase):
    def test_the_exact_text_of_a_small_calendar(self):
        text = ical.render_calendar(
            "My tasks", [event(description="Project: Web (WEB)\nStatus: To do", url="https://pm.example.com/#/t/WEB-1")], description="About it"
        )
        expected = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Zylker Projects//Calendar feed//EN", "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH", "NAME:My tasks", "X-WR-CALNAME:My tasks", "X-WR-CALDESC:About it",
            "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
            "BEGIN:VEVENT", "UID:task-WEB-1@zylker-pm", "DTSTAMP:20261016T090000Z", "LAST-MODIFIED:20261016T090000Z",
            "DTSTART;VALUE=DATE:20261019", "DTEND;VALUE=DATE:20261024", "SUMMARY:WEB-1 Homepage",
            "DESCRIPTION:Project: Web (WEB)\\nStatus: To do", "URL:https://pm.example.com/#/t/WEB-1",
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT", "END:VCALENDAR",
        ]  # fmt: skip
        self.assertEqual(text, "\r\n".join(expected) + "\r\n")

    def test_an_empty_calendar_is_valid(self):
        calendar = icalcheck.parse(ical.render_calendar("Nothing", []))
        self.assertEqual(calendar["children"], [])
        self.assertEqual(icalcheck.props(calendar)["VERSION"]["value"], "2.0")

    def test_optional_properties_are_left_out_when_empty(self):
        found = icalcheck.events(ical.render_calendar("c", [event()]))[0]
        self.assertNotIn("DESCRIPTION", found)
        self.assertNotIn("URL", found)

    def test_the_refresh_hint(self):
        self.assertIn("X-PUBLISHED-TTL:PT1H\r\n", ical.render_calendar("c", []))
        self.assertIn("X-PUBLISHED-TTL:PT90M\r\n", ical.render_calendar("c", [], refresh_minutes=90))
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT2H\r\n", ical.render_calendar("c", [], refresh_minutes=120))

    def test_identifiers_and_addresses_are_made_safe(self):
        found = icalcheck.events(ical.render_calendar("c", [event(uid="task a/b:c\r\nX@host", url="https://x.example/a b/\u00e9?q=1#frag")]))[0]
        self.assertEqual(found["UID"], "task-a-b-c--X@host")
        self.assertEqual(found["URL"], "https://x.example/a%20b/%C3%A9?q=1#frag")

    def test_text_cannot_inject_a_property(self):
        hostile = "x\r\nATTENDEE:mailto:evil@example.com\r\nEND:VEVENT"
        found = icalcheck.events(ical.render_calendar(hostile, [event(summary=hostile, description=hostile)]))
        self.assertEqual(len(found), 1)
        self.assertNotIn("ATTENDEE", found[0])
        self.assertEqual(found[0]["SUMMARY"], "x\nATTENDEE:mailto:evil@example.com\nEND:VEVENT")

    def test_long_unicode_text_survives_the_round_trip(self):
        title = ("Plan, review; ship \u2713 \u65e5\u672c\u8a9e \U0001f642 " * 12).strip()
        text = ical.render_calendar(title, [event(summary=title, description=title)])
        found = icalcheck.events(text)[0]  # the strict reader also checks every physical line
        self.assertEqual((found["SUMMARY"], found["DESCRIPTION"]), (title, title))
        self.assertGreater(text.count("\r\n "), 3)

    def test_the_same_input_always_gives_the_same_bytes(self):
        events = [event(), event(uid="b", summary="B")]
        self.assertEqual(ical.render_calendar("c", events), ical.render_calendar("c", events))

    def test_every_event_has_the_properties_clients_require(self):
        for found in icalcheck.events(ical.render_calendar("c", [event(), event(uid="b", first_day=date(2026, 12, 31), last_day=date(2026, 12, 31))])):
            for name in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
                self.assertIn(name, found)
            self.assertEqual(found["_params"]["DTSTART"], {"VALUE": "DATE"})
            self.assertEqual(found["TRANSP"], "TRANSPARENT")


if __name__ == "__main__":
    unittest.main()
