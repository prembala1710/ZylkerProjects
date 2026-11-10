"""Outbound HTTP for webhooks: URL validation, the SSRF guard and the POST itself (standard library only).

A webhook URL is chosen by an administrator but still points the *server* at an arbitrary host, so
two guards apply unless ``ZYLKER_WEBHOOK_ALLOW_PRIVATE`` is set:

* the URL is checked when it is saved (scheme, no credentials, no private literal address), and
* at delivery time the host name is resolved here, **every** answer must be a public unicast
  address, and the connection is then made to the vetted IP itself - so a name that resolves to a
  public address when checked and a private one when connected (DNS rebinding) cannot slip through.

Redirects are never followed, the response body is read in small bounded pieces, and nothing from the
exchange other than a short sanitised excerpt is kept. URLs are never logged (they may carry a key).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2000
MAX_READ = 2048  # bytes of the response body that are ever read
EXCERPT_CHARS = 300

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class Blocked(Exception):
    """The destination is not a public address."""


def is_blocked(address) -> bool:
    """True for everything that is not an ordinary public unicast address.

    Covers loopback, private, link-local (including the 169.254.169.254 metadata address), carrier-grade
    NAT, multicast, reserved and unspecified ranges, and IPv6 forms that merely wrap an IPv4 address
    (IPv4-mapped and NAT64), which are judged by the IPv4 address inside. The deprecated 6to4 and Teredo tunnelling
    ranges are refused outright.
    """
    if isinstance(address, str):
        address = ipaddress.ip_address(address.split("%", 1)[0])
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return is_blocked(address.ipv4_mapped)
        if address.sixtofour is not None or address.teredo is not None:
            return True
        if address in _NAT64:
            return is_blocked(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return address.is_multicast or not address.is_global


def ip_literal(host: str):
    """The address a host string denotes when it is an IP literal (including odd IPv4 spellings such as
    ``2130706433`` or ``127.1``, which resolvers accept too); None for ordinary host names."""
    host = host.strip("[]")
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def validate_url(url, allow_private=False) -> str:
    """Return the URL, normalised, or raise ValueError with a message fit for an API error."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("must be a URL")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("must be at most %d characters" % MAX_URL_LENGTH)
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
        raise ValueError("must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("is not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("must start with http:// or https://")
    host = parts.hostname
    if not host:
        raise ValueError("must include a host name")
    if parts.username is not None or parts.password is not None:
        raise ValueError("must not contain a user name or password (deliveries are authenticated by the signature)")
    if port == 0:
        raise ValueError("has an invalid port")
    if not allow_private:
        literal = ip_literal(host)
        if host == "localhost" or host.endswith(".localhost") or (literal is not None and is_blocked(literal)):
            raise ValueError("points at a private, loopback or reserved address, which webhooks may not use")
    return url


def default_resolver(host, port):
    """Every distinct address the name resolves to, as strings."""
    found = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = info[4][0]
        if ip not in found:
            found.append(ip)
    return found


def vet_addresses(addresses, allow_private):
    if not addresses:
        raise socket.gaierror("the host name did not resolve")
    if not allow_private and any(is_blocked(a) for a in addresses):
        raise Blocked("resolves to a non-public address")
    return list(addresses)


def _connect_pinned(addresses, port, timeout):
    last = None
    for ip in addresses:
        try:
            return socket.create_connection((ip, port), timeout)
        except OSError as exc:
            last = exc
    raise last or OSError("no usable address")


class _PinnedHTTP(http.client.HTTPConnection):
    """Connects to an address that was already vetted instead of resolving the host name again."""

    def __init__(self, host, port, addresses, timeout):
        super().__init__(host, port, timeout=timeout)
        self._addresses = addresses

    def connect(self):
        self.sock = _connect_pinned(self._addresses, self.port, self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """As above; the certificate is still checked against the host name in the URL (also used for SNI)."""

    def __init__(self, host, port, addresses, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._addresses = addresses

    def connect(self):
        raw = _connect_pinned(self._addresses, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def ssl_context() -> ssl.SSLContext:
    """Certificate and host name verification stay on (a module-level hook so tests can add a test CA)."""
    return ssl.create_default_context()


@dataclass
class SendResult:
    ok: bool
    status: int | None = None
    error: str | None = None  # short machine-readable code, never an exception message
    excerpt: str = ""
    duration_ms: int = 0


def clean_excerpt(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    text = "".join(ch if ch.isprintable() or ch in "\n\t " else " " for ch in text)
    return " ".join(text.split())[:EXCERPT_CHARS]


def _read_limited(response, deadline) -> bytes:
    chunks, total = [], 0
    try:
        while total < MAX_READ and time.monotonic() < deadline:
            chunk = response.read1(MAX_READ - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except (http.client.HTTPException, OSError):
        pass  # the status line already told us the outcome; a broken body does not change it
    return b"".join(chunks)


def post(url, body, headers, *, timeout=10.0, allow_private=False, resolver=None) -> SendResult:
    """POST ``body`` to ``url``. Never raises: every failure becomes a SendResult with an error code."""
    started = time.monotonic()

    def result(ok, status=None, error=None, excerpt=""):
        return SendResult(ok, status, error, excerpt, int((time.monotonic() - started) * 1000))

    conn = None
    try:
        parts = urlsplit(url)
        if not parts.hostname:
            return result(False, error="invalid_request")  # never let "no host" fall through to the resolver's loopback default
        https = parts.scheme == "https"
        port = parts.port or (443 if https else 80)
        addresses = vet_addresses((resolver or default_resolver)(parts.hostname, port), allow_private)
        if https:
            conn = _PinnedHTTPS(parts.hostname, port, addresses, timeout, ssl_context())
        else:
            conn = _PinnedHTTP(parts.hostname, port, addresses, timeout)
        target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        conn.request("POST", target, body=body, headers=headers)
        response = conn.getresponse()
        excerpt = clean_excerpt(_read_limited(response, started + timeout))
        if 200 <= response.status < 300:
            return result(True, response.status, None, excerpt)
        return result(False, response.status, "http_%d" % response.status, excerpt)
    except Blocked:
        return result(False, error="blocked_address")
    except socket.gaierror:
        return result(False, error="dns_error")
    except TimeoutError:
        return result(False, error="timeout")
    except ssl.SSLError:
        return result(False, error="tls_error")
    except http.client.HTTPException:
        return result(False, error="protocol_error")
    except OSError:
        return result(False, error="connection_failed")
    except ValueError:  # e.g. a header or host name the HTTP layer refuses
        return result(False, error="invalid_request")
    finally:
        if conn is not None:
            conn.close()
