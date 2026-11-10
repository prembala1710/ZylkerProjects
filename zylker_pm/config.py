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
    webhook_allow_private: bool = False  # let outbound webhooks target loopback / private-network addresses
    webhook_timeout: float = 10.0  # seconds, per network operation of one webhook delivery attempt

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
            webhook_timeout = float(get("WEBHOOK_TIMEOUT", "10"))
        except ValueError:
            raise ValueError("ZYLKER_WEBHOOK_TIMEOUT must be a number of seconds") from None
        cfg = cls(
            db_path=get("DB", "zylker.db"),
            host=get("HOST", "127.0.0.1"),
            port=port,
            timezone=get("TIMEZONE", "UTC"),
            public_url=get("PUBLIC_URL").rstrip("/"),
            cliq_endpoint=get("CLIQ_ENDPOINT"),
            cliq_bot_unique_name=get("CLIQ_BOT_UNIQUE_NAME"),
            webhook_allow_private=get("WEBHOOK_ALLOW_PRIVATE").strip().lower() in ("1", "true", "yes", "on"),
            webhook_timeout=webhook_timeout,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        get_tz(self.timezone)
        if self.cliq_endpoint:
            parsed = urlparse(self.cliq_endpoint)
            local = parsed.hostname in LOOPBACK_HOSTS
            if not parsed.hostname or parsed.scheme not in ("https", "http"):
                raise ValueError("ZYLKER_CLIQ_ENDPOINT must be an http(s) URL")
            if parsed.scheme != "https" and not local:
                raise ValueError("ZYLKER_CLIQ_ENDPOINT must use https")
        if self.public_url and not self.public_url.startswith(("http://", "https://")):
            raise ValueError("ZYLKER_PUBLIC_URL must start with http:// or https://")
        if not 1 <= self.webhook_timeout <= 60:
            raise ValueError("ZYLKER_WEBHOOK_TIMEOUT must be between 1 and 60 seconds")
