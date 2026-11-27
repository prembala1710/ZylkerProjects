"""A strict reader for the iCalendar text this project writes, used by the tests as an independent check of the writer.

It is deliberately stricter than most calendar apps: it refuses bare line feeds, over-long lines, unescaped commas or
semicolons in text, unknown escapes and unbalanced components, so a writer bug shows up here instead of in somebody's calendar.
"""

from __future__ import annotations

TEXT_PROPERTIES = {"SUMMARY", "DESCRIPTION", "NAME", "X-WR-CALNAME", "X-WR-CALDESC", "PRODID"}
ESCAPES = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}


def unfold(text: str) -> list[str]:
    assert text.endswith("\r\n"), "the text must end with CRLF"
    body = text[:-2]
    assert "\n" not in body.replace("\r\n", "") and "\r" not in body.replace("\r\n", ""), "bare CR or LF in the text"
    physical = body.split("\r\n")
    for line in physical:
        assert len(line.encode("utf-8")) <= 75, "line longer than 75 octets: %r" % line
        assert line != "", "empty line"
    logical = []
    for line in physical:
        if line.startswith(" "):
            assert logical, "continuation line without a line to continue"
            logical[-1] += line[1:]
        else:
            logical.append(line)
    return logical


def unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        char = value[i]
        if char == "\\":
            assert i + 1 < len(value), "dangling backslash"
            assert value[i + 1] in ESCAPES, "illegal escape \\%s" % value[i + 1]
            out.append(ESCAPES[value[i + 1]])
            i += 2
            continue
        assert char not in ",;", "unescaped %r in a text value: %r" % (char, value)
        out.append(char)
        i += 1
    return "".join(out)


def parse(text: str) -> dict:
    """Return the VCALENDAR as {"type", "props": [(name, params, value)], "children": [...]}."""
    root, stack = None, []
    for line in unfold(text):
        head, sep, value = line.partition(":")
        assert sep, "no colon in %r" % line
        name, *raw_params = head.split(";")
        params = dict(p.split("=", 1) for p in raw_params)
        if name == "BEGIN":
            component = {"type": value, "props": [], "children": []}
            if stack:
                stack[-1]["children"].append(component)
            else:
                assert root is None, "more than one top-level component"
                root = component
            stack.append(component)
        elif name == "END":
            assert stack and stack[-1]["type"] == value, "END:%s does not match" % value
            stack.pop()
        else:
            assert stack, "property outside a component"
            stack[-1]["props"].append((name, params, unescape(value) if name in TEXT_PROPERTIES else value))
    assert not stack, "unclosed component"
    assert root is not None and root["type"] == "VCALENDAR"
    return root


def props(component: dict) -> dict:
    """Properties by name; a name that occurs twice is a bug in a writer that promises single occurrences."""
    out = {}
    for name, params, value in component["props"]:
        assert name not in out, "%s occurs twice" % name
        out[name] = {"value": value, "params": params}
    return out


def events(text: str) -> list[dict]:
    """Every VEVENT as a flat dict: UID, SUMMARY, ... plus 'start' and 'end' (the exclusive DTEND) as given."""
    calendar = parse(text)
    result = []
    for child in calendar["children"]:
        assert child["type"] == "VEVENT"
        found = props(child)
        event = {name: item["value"] for name, item in found.items()}
        event["_params"] = {name: item["params"] for name, item in found.items()}
        result.append(event)
    return result
