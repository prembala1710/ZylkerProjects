"""Small helpers: time formatting and request-body validation."""

from __future__ import annotations

import re
from datetime import date, datetime

from .errors import BadRequest, ValidationError

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
TASK_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,9})$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-/]{0,29}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value) -> date | None:
    """Strict YYYY-MM-DD. Returns None when the value is not a valid date."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def truncate(text, limit=200):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_task_key(key: str) -> tuple[str, int]:
    match = TASK_KEY_RE.match(key or "")
    if not match:
        raise BadRequest("Invalid task key %r (expected e.g. WEB-12)" % key, code="invalid_key")
    return match.group(1).upper(), int(match.group(2))


def normalize_labels(values, field="labels"):
    """Lower-case, de-duplicate and validate a list of labels."""
    if not isinstance(values, list):
        raise ValidationError({field: "must be a list of strings"})
    out = []
    for raw in values:
        if not isinstance(raw, str):
            raise ValidationError({field: "must be a list of strings"})
        label = raw.strip().lower()
        if not LABEL_RE.match(label):
            raise ValidationError(
                {field: "invalid label %r (1-30 chars: letters, digits, _ . - /)" % raw}
            )
        if label not in out:
            out.append(label)
    if len(out) > 20:
        raise ValidationError({field: "at most 20 labels per task"})
    return out


class Checker:
    """Validates a JSON object against a whitelist of fields.

    Collects every problem and raises a single 422 from ``result()``. With
    ``partial=True`` (PATCH) absent fields are simply skipped.
    """

    def __init__(self, data, allowed, partial=False):
        if not isinstance(data, dict):
            raise BadRequest("A JSON object is required")
        self.data = data
        self.partial = partial
        self.errors: dict[str, str] = {}
        self.out: dict = {}
        for key in data:
            if key not in allowed:
                self.errors[key] = "unknown field"

    def _present(self, name, required):
        if name in self.data:
            return True
        if required and not self.partial:
            self.errors[name] = "required"
        return False

    def _null(self, name, nullable):
        if self.data[name] is None:
            if nullable:
                self.out[name] = None
            else:
                self.errors[name] = "must not be null"
            return True
        return False

    def text(self, name, *, required=False, max_len=200, min_len=0, nullable=False):
        if not self._present(name, required) or self._null(name, nullable):
            return
        value = self.data[name]
        if not isinstance(value, str):
            self.errors[name] = "must be a string"
            return
        value = value.strip()
        if len(value) < min_len:
            self.errors[name] = "must be at least %d character(s)" % min_len if min_len > 1 else "must not be empty"
        elif len(value) > max_len:
            self.errors[name] = "must be at most %d characters" % max_len
        else:
            self.out[name] = value

    def choice(self, name, choices, *, required=False, nullable=False):
        if not self._present(name, required) or self._null(name, nullable):
            return
        value = self.data[name]
        if value not in choices:
            self.errors[name] = "must be one of: " + ", ".join(choices)
        else:
            self.out[name] = value

    def date(self, name, *, required=False, nullable=True):
        if not self._present(name, required) or self._null(name, nullable):
            return
        if parse_date(self.data[name]) is None:
            self.errors[name] = "must be a valid date in YYYY-MM-DD format"
        else:
            self.out[name] = self.data[name]

    def number(self, name, *, minimum=None, maximum=None, nullable=False, required=False, digits=2):
        if not self._present(name, required) or self._null(name, nullable):
            return
        value = self.data[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.errors[name] = "must be a number"
        elif value != value or value in (float("inf"), float("-inf")):
            self.errors[name] = "must be a finite number"
        elif minimum is not None and value < minimum:
            self.errors[name] = "must be >= %s" % minimum
        elif maximum is not None and value > maximum:
            self.errors[name] = "must be <= %s" % maximum
        else:
            self.out[name] = round(float(value), digits)

    def integer(self, name, *, minimum=None, maximum=None, nullable=False, required=False):
        if not self._present(name, required) or self._null(name, nullable):
            return
        value = self.data[name]
        if isinstance(value, bool) or not isinstance(value, int):
            self.errors[name] = "must be an integer"
        elif minimum is not None and value < minimum:
            self.errors[name] = "must be >= %s" % minimum
        elif maximum is not None and value > maximum:
            self.errors[name] = "must be <= %s" % maximum
        else:
            self.out[name] = value

    def boolean(self, name, *, required=False):
        if not self._present(name, required):
            return
        if not isinstance(self.data[name], bool):
            self.errors[name] = "must be true or false"
        else:
            self.out[name] = self.data[name]

    def labels(self, name="labels"):
        if not self._present(name, False):
            return
        try:
            self.out[name] = normalize_labels(self.data[name], name)
        except ValidationError as exc:
            self.errors.update(exc.details)

    def raw(self, name, *, required=False):
        """Pass a value through untouched (the service validates it)."""
        if self._present(name, required):
            self.out[name] = self.data[name]

    def result(self):
        if self.errors:
            raise ValidationError(self.errors)
        return self.out
