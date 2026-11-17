"""Runtime configuration, read from ZYLKER_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timezone, tzinfo
from urllib.parse import urlparse

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def get_tz(name: str) -> tzinfo:
    if name.upper() == "UTC":
        return timezone.utc
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            "Unknown timezone %r. Use an IANA name such as 'Asia/Kolkata' "
            "(on Windows, `pip install tzdata`)." % name
        ) from None


@dataclass(frozen=True)
class Config:
    db_path: str = "zylker.db"
    host: str = "127.0.0.1"
    port: int = 8080
    timezone: str = "UTC"  # decides what "today" means for overdue checks
    public_url: str = ""  # used for links in Cliq messages
    cliq_endpoint: str = ""  # Cliq incoming-webhook URL; empty disables notifications
    cliq_bot_unique_name: str = ""  # optional: post as this bot
    max_body_bytes: int = 1_000_000
    max_import_bytes: int = 5_000_000
    default_task_hours: float = 8.0  # duration assumed for unestimated tasks in critical path
    recurrence_check_minutes: int = 15  # how often `serve` looks for recurring tasks that are due (0 = never)
    auth_failure_limit: int = 0  # failed sign-ins per address (in 5 minutes) before it is blocked; 0 = off. from_env turns on 10
    rate_limit_per_minute: int = 0  # sustained requests per user per minute; 0 = off. from_env turns on 600
    webhook_allow_private: bool = False  # let webhooks reach private/loopback addresses and use plain http (internal receivers)
    webhook_interval_seconds: int = 0  # how often `serve` delivers queued webhooks (0 = never; from_env turns on 2)
    proxy_hops: int = 0  # trusted reverse proxies in front of the app; decides which client address the audit log records

    @classmethod
    def from_env(cls, env=None) -> Config:
        env = os.environ if env is None else env

        def get(name, default=""):
            return env.get("ZYLKER_" + name, default)

        try:
            port = int(get("PORT", "8080"))
        except ValueError:
            raise ValueError("ZYLKER_PORT must be an integer") from None
        try:
            proxy_hops = int(get("PROXY_HOPS", "0"))
        except ValueError:
            raise ValueError("ZYLKER_PROXY_HOPS must be an integer") from None
        try:
            recurrence_check_minutes = int(get("RECURRENCE_CHECK_MINUTES", "15"))
        except ValueError:
            raise ValueError("ZYLKER_RECURRENCE_CHECK_MINUTES must be an integer") from None
        numbers = {}
        for attr, name, default in (("auth_failure_limit", "AUTH_FAILURE_LIMIT", "10"), ("rate_limit_per_minute", "RATE_LIMIT_PER_MINUTE", "600")):
            try:
                numbers[attr] = int(get(name, default))
            except ValueError:
                raise ValueError("ZYLKER_%s must be an integer" % name) from None
        try:
            numbers["webhook_interval_seconds"] = int(get("WEBHOOK_INTERVAL_SECONDS", "2"))
        except ValueError:
            raise ValueError("ZYLKER_WEBHOOK_INTERVAL_SECONDS must be an integer") from None
        allow_private = get("WEBHOOK_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on")
        cfg = cls(
            db_path=get("DB", "zylker.db"),
            host=get("HOST", "127.0.0.1"),
            port=port,
            timezone=get("TIMEZONE", "UTC"),
            public_url=get("PUBLIC_URL").rstrip("/"),
            cliq_endpoint=get("CLIQ_ENDPOINT"),
            cliq_bot_unique_name=get("CLIQ_BOT_UNIQUE_NAME"),
            proxy_hops=proxy_hops,
            recurrence_check_minutes=recurrence_check_minutes,
            webhook_allow_private=allow_private,
            **numbers,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        get_tz(self.timezone)
        if not 0 <= self.proxy_hops <= 5:
            raise ValueError("ZYLKER_PROXY_HOPS must be between 0 and 5")
        if not 0 <= self.recurrence_check_minutes <= 1440:
            raise ValueError("ZYLKER_RECURRENCE_CHECK_MINUTES must be between 0 and 1440")
        if not 0 <= self.auth_failure_limit <= 1000:
            raise ValueError("ZYLKER_AUTH_FAILURE_LIMIT must be between 0 and 1000")
        if not 0 <= self.rate_limit_per_minute <= 100_000:
            raise ValueError("ZYLKER_RATE_LIMIT_PER_MINUTE must be between 0 and 100000")
        if not 0 <= self.webhook_interval_seconds <= 3600:
            raise ValueError("ZYLKER_WEBHOOK_INTERVAL_SECONDS must be between 0 and 3600")
        if self.cliq_endpoint:
            parsed = urlparse(self.cliq_endpoint)
            local = parsed.hostname in LOOPBACK_HOSTS
            if not parsed.hostname or parsed.scheme not in ("https", "http"):
                raise ValueError("ZYLKER_CLIQ_ENDPOINT must be an http(s) URL")
            if parsed.scheme != "https" and not local:
                raise ValueError("ZYLKER_CLIQ_ENDPOINT must use https")
        if self.public_url and not self.public_url.startswith(("http://", "https://")):
            raise ValueError("ZYLKER_PUBLIC_URL must start with http:// or https://")
