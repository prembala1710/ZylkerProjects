"""API-token authentication.

Tokens are random 256-bit values, so a plain SHA-256 digest is sufficient for
storage (there is nothing to brute-force). The raw token is shown exactly once.
"""

from __future__ import annotations

import hashlib
import secrets

from .errors import Unauthorized

TOKEN_PREFIX = "zpm_"


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(header):
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def authenticate(conn, header):
    """Return the active user row for an Authorization header or raise 401."""
    token = bearer_token(header)
    challenge = {"WWW-Authenticate": "Bearer"}
    if not token:
        raise Unauthorized("Missing bearer token", headers=challenge)
    row = conn.execute("SELECT * FROM users WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if row is None or not row["active"]:
        raise Unauthorized("Invalid or revoked token", headers=challenge)
    return row
