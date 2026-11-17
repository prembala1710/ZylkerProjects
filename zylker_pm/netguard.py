"""Safe outbound HTTP for webhooks: URL validation, address screening and a one-shot POST.

A webhook URL is typed in by an administrator but the *server* makes the request, so it must not become a way to
reach things only the server can reach (cloud metadata at 169.254.169.254, an internal admin page, localhost).
Three defences work together:

* ``validate_url`` rejects anything that is not a plain ``https`` URL with a host name (credentials in the URL,
  backslashes, odd ports and literal private addresses included).
* ``send`` resolves the name itself, refuses the delivery if *any* resolved address is not a public one, and
  then connects to that very address - so the name cannot be re-pointed at a private address between the check and
  the connection (DNS rebinding).
* Redirects are never followed and the response body is never kept.

``allow_private`` (``ZYLKER_WEBHOOK_ALLOW_PRIVATE``) is the deliberate opt-out for installations whose receivers
live on the internal network; it also permits plain ``http``.

Errors are short codes (``timeout``, ``blocked_address`` ...) rather than exception text, which can contain
addresses and URLs.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from typing import NamedTuple
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2000
TIMEOUT_SECONDS = 10.0
READ_LIMIT = 4096  # the response is read (so the connection ends cleanly) but only this much, and never stored

_FORBIDDEN = re.compile(r"[\s\\<>\"`{}|^\x00-\x1f\x7f]")
_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")
_LOCAL_NAMES = ("localhost", "localhost.localdomain")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class SendResult(NamedTuple):
    status: int | None  # HTTP status, or None when no response arrived
    error: str | None  # short code; None when a response arrived
    ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class Target(NamedTuple):
    scheme: str
    host: str
    port: int
    path: str  # path and query, ready for the request line


def is_public(address: str) -> bool:
    """True only for globally routable unicast addresses. IPv4 hidden inside IPv6 (mapped, 6to4, Teredo, NAT64)
    is judged by the IPv4 address it carries."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            return is_public(str(ip.sixtofour))
        if ip.teredo is not None:
            return is_public(str(ip.teredo[1])) and is_public(str(ip.teredo[0]))
        if ip in _NAT64:
            return is_public(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    return ip.is_global and not ip.is_multicast


def validate_url(url, allow_private=False) -> Target:
    """Check a webhook URL without touching the network. Raises ValueError with a message fit for the user."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("must be a URL")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("must be at most %d characters" % MAX_URL_LENGTH)
    if _FORBIDDEN.search(url):
        raise ValueError("must not contain spaces, backslashes, quotes or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ValueError("is not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("must start with https://")
    if parts.scheme == "http" and not allow_private:
        raise ValueError("must use https:// (plain http is only allowed when ZYLKER_WEBHOOK_ALLOW_PRIVATE is set)")
    if not host:
        raise ValueError("needs a host name")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("must not contain a user name or password")
    if parts.fragment:
        raise ValueError("must not contain a #fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("has an invalid port")
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if not host.isascii() or not _HOST.match(host):
            raise ValueError("has an invalid host name (use the punycode form for international names)") from None
    if not allow_private:
        lowered = host.lower().rstrip(".")
        if literal is not None and not is_public(host):
            raise ValueError("points at a private or reserved address")
        if literal is None and (lowered in _LOCAL_NAMES or lowered.endswith(_LOCAL_SUFFIXES)):
            raise ValueError("points at a local host name")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(parts.scheme, host, port or (443 if parts.scheme == "https" else 80), path)


def _fail(started, code):
    return SendResult(None, code, int((time.monotonic() - started) * 1000))


def send(url, body: bytes, headers: dict, timeout=TIMEOUT_SECONDS, *, allow_private=False, resolver=None) -> SendResult:
    """POST ``body`` to ``url``. Never raises; the outcome is a ``SendResult``."""
    started = time.monotonic()
    sock = None
    try:
        try:
            target = validate_url(url, allow_private)
        except ValueError:
            return _fail(started, "invalid_url")
        try:
            infos = (resolver or socket.getaddrinfo)(target.host, target.port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return _fail(started, "dns_failure")
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        if not addresses:
            return _fail(started, "dns_failure")
        if not allow_private and not all(is_public(a) for a in addresses):
            return _fail(started, "blocked_address")  # one private answer is enough to refuse: no picking and choosing
        failure = "connection_failed"
        for address in addresses:
            try:
                sock = socket.create_connection((address, target.port), timeout=timeout)
                break
            except TimeoutError:
                failure = "timeout"
            except ConnectionRefusedError:
                failure = "connection_refused"
            except OSError:
                failure = "connection_failed"
        if sock is None:
            return _fail(started, failure)
        if target.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.host)
        conn = http.client.HTTPConnection(target.host, target.port, timeout=timeout)
        conn.sock = sock  # connect to the address we vetted, whatever the name resolves to a moment later
        conn.request("POST", target.path, body=body, headers=headers)
        response = conn.getresponse()
        response.read(READ_LIMIT)
        return SendResult(response.status, None, int((time.monotonic() - started) * 1000))
    except ssl.SSLCertVerificationError:
        return _fail(started, "tls_certificate")
    except ssl.SSLError:
        return _fail(started, "tls_error")
    except TimeoutError:
        return _fail(started, "timeout")
    except (http.client.HTTPException, ConnectionError):
        return _fail(started, "protocol_error")
    except OSError:
        return _fail(started, "network_error")
    except Exception:  # noqa: BLE001 - a delivery attempt must never raise into the worker
        return _fail(started, "internal_error")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def make_sender(allow_private=False, resolver=None):
    """The callable the delivery worker uses: ``sender(url, body, headers, timeout) -> SendResult``."""

    def sender(url, body, headers, timeout=TIMEOUT_SECONDS):
        return send(url, body, headers, timeout, allow_private=allow_private, resolver=resolver)

    return sender
