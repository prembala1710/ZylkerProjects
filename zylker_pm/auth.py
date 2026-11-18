"""Bearer-token authentication.

Two kinds of token exist. A user's *primary* token (``zpm_...``) is created with the account, never expires and has the
user's full powers. *API tokens* (``zpt_...``) are the narrower credentials for integrations: several per user, each
with an expiry, a cap on the role it may act with, an optional read-only flag and an optional allow-list of addresses.

Tokens are random 256-bit values, so a plain SHA-256 digest is sufficient for storage (there is nothing to
brute-force). The raw token is shown exactly once.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from datetime import datetime, timezone

from .domain import ROLE_RANK
from .errors import Unauthorized
from .util import iso

TOKEN_PREFIX = "zpm_"
API_TOKEN_PREFIX = "zpt_"

# Why an API token was refused, as recorded in the audit log (anything else is "invalid_token" / "missing_token").
REASONS = ("token_expired", "token_revoked", "token_ip_not_allowed")


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_api_token() -> str:
    return API_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(header):
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def token_of(user):
    """The API-token details when the request was authenticated with one, else None (a primary token or the CLI)."""
    return user.get("token") if isinstance(user, dict) else None


def rate_key(user):
    """Who a request is charged to: an API token has a budget of its own, so an integration cannot starve its owner."""
    tok = token_of(user)
    return ("token", tok["id"]) if tok else user["id"]


def effective_role(user_role, cap):
    return user_role if ROLE_RANK[user_role] <= ROLE_RANK[cap] else cap


def _forms(address):
    """An address as it may be written in an allow-list: IPv4 addresses are also tried in IPv6 notation and back."""
    if address.version == 6 and address.ipv4_mapped is not None:
        return [address, address.ipv4_mapped]
    if address.version == 4:
        return [address, ipaddress.IPv6Address("::ffff:" + str(address))]
    return [address]


def address_allowed(ip, networks) -> bool:
    """Whether ``ip`` is inside one of ``networks`` (no networks: anywhere). An address that cannot be read never is."""
    if not networks:
        return True
    try:
        forms = _forms(ipaddress.ip_address(ip or ""))
    except ValueError:
        return False
    for net in networks:
        try:
            network = ipaddress.ip_network(net, strict=False)
        except ValueError:
            continue
        if any(form in network for form in forms):
            return True
    return False


def authenticate(conn, header, now=None, ip=None):
    """Return the active user for an Authorization header or raise 401.

    A primary token gives the user's row. An API token gives a dict shaped like the row, with ``role`` already
    lowered to the token's cap and a ``token`` entry describing the credential.
    """
    token = bearer_token(header)
    challenge = {"WWW-Authenticate": "Bearer"}
    if not token:
        raise Unauthorized("Missing bearer token", headers=challenge)
    if token.startswith(API_TOKEN_PREFIX):
        return _api_token_user(conn, token, now or datetime.now(timezone.utc), ip, challenge)  # expiry is never skipped
    row = conn.execute("SELECT * FROM users WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if row is None or not row["active"]:
        raise Unauthorized("Invalid or revoked token", headers=challenge)
    return row


def _refused(message, code, row, challenge):
    """A 401 for a token that exists but may not be used. The audit log learns whose it is (``exc.audit``); the client
    is only told why."""
    exc = Unauthorized(message, code=code, headers=challenge)
    exc.audit = {"actor_id": row["id"], "details": {"token": row["tok_hint"], "user": row["username"]}}
    return exc


def _api_token_user(conn, token, now, ip, challenge):
    row = conn.execute(
        "SELECT u.*, t.id AS tok_id, t.name AS tok_name, t.hint AS tok_hint, t.role_cap AS tok_cap, t.read_only AS tok_read_only, "
        "t.allowed_ips AS tok_ips, t.expires_at AS tok_expires, t.revoked_at AS tok_revoked "
        "FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
        (hash_token(token),),
    ).fetchone()
    if row is None or not row["active"]:
        raise Unauthorized("Invalid or revoked token", headers=challenge)
    if row["tok_revoked"]:
        raise _refused("This API token has been revoked", "token_revoked", row, challenge)
    if iso(now) >= row["tok_expires"]:
        raise _refused("This API token has expired", "token_expired", row, challenge)
    networks = json.loads(row["tok_ips"] or "[]")
    if not address_allowed(ip, networks):
        raise _refused("This API token cannot be used from this address", "token_ip_not_allowed", row, challenge)
    user = {k: row[k] for k in row.keys() if not k.startswith("tok_") and k != "token_hash"}
    user["real_role"] = row["role"]
    user["role"] = effective_role(row["role"], row["tok_cap"])
    user["token"] = {
        "id": row["tok_id"], "name": row["tok_name"], "hint": row["tok_hint"], "role_cap": row["tok_cap"],
        "read_only": bool(row["tok_read_only"]), "expires_at": row["tok_expires"], "allowed_ips": networks,
    }  # fmt: skip
    return user
