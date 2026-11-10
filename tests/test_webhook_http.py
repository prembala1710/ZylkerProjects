"""The webhook transport: real sockets, the SSRF guard, DNS pinning and TLS verification."""

import http.client
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from webhook_helpers import Receiver

from zylker_pm import webhook_http
from zylker_pm.webhook_http import clean_excerpt, ip_literal, is_blocked, post, validate_url

HEADERS = {"Content-Type": "application/json", "X-Zylker-Event": "task.created", "Connection": "close"}
BODY = b'{"event":"task.created"}'

NON_PUBLIC = [
    "127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.5.4", "172.31.255.255", "192.168.1.1", "169.254.169.254", "169.254.0.1", "100.64.0.1",
    "100.127.255.255", "0.0.0.0", "255.255.255.255", "224.0.0.1", "239.255.255.250", "240.0.0.1", "192.0.2.1", "198.51.100.7", "203.0.113.9",
    "198.18.0.1", "192.0.0.1", "::1", "::", "fe80::1", "fe80::1%eth0", "fc00::1", "fd12:3456::1", "ff02::1", "2001:db8::1",
    "::ffff:127.0.0.1", "::ffff:10.1.2.3", "::ffff:169.254.169.254", "2002:7f00:1::1", "2002:a9fe:a9fe::1", "2002:808:808::1",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2", "64:ff9b::7f00:1", "64:ff9b::a00:1",
]  # fmt: skip
PUBLIC = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "172.32.0.1", "172.15.255.255", "100.63.255.255", "100.128.0.1", "2606:4700:4700::1111",
          "2001:4860:4860::8888", "::ffff:8.8.8.8", "64:ff9b::808:808"]  # fmt: skip


class AddressTests(unittest.TestCase):
    def test_everything_that_is_not_a_public_unicast_address_is_blocked(self):
        for address in NON_PUBLIC:
            with self.subTest(address=address):
                self.assertTrue(is_blocked(address))

    def test_public_addresses_are_allowed(self):
        for address in PUBLIC:
            with self.subTest(address=address):
                self.assertFalse(is_blocked(address))

    def test_odd_spellings_of_an_ip_literal_are_recognised(self):
        for host, expected in {"2130706433": "127.0.0.1", "127.1": "127.0.0.1", "0x7f.1": "127.0.0.1", "1.2.3": "1.2.0.3", "[::1]": "::1", "::1": "::1"}.items():
            self.assertEqual(str(ip_literal(host)), expected, host)
        for host in ("example.com", "localhost", "hooks.example.org", "1.2.3.4.example.com"):
            self.assertIsNone(ip_literal(host), host)

    def test_url_validation(self):
        for url in ("https://example.com/hook", "http://8.8.8.8:8080/x?a=b", "https://hooks.example.org:8443/a/b?token=1"):
            self.assertEqual(validate_url(url), url)
        self.assertEqual(validate_url("  https://example.com/x  "), "https://example.com/x")
        for url in ("ftp://x.example/", "https://", "https://user@example.com/", "https://:pw@example.com/", "http://example.com:0/",
                    "http://example.com:70000/", "", "   ", None, 5, "https://example.com/a\nb", "http://[::1", "https://example.com/" + "a" * 2001):  # fmt: skip
            with self.assertRaises(ValueError, msg=repr(url)):
                validate_url(url)
        self.assertEqual(validate_url("http://localhost:9/x", allow_private=True), "http://localhost:9/x")

    def test_excerpts_are_short_and_printable(self):
        self.assertEqual(clean_excerpt(b"upstream  exploded\n\x07bell\t!"), "upstream exploded bell !")
        self.assertEqual(len(clean_excerpt(b"x" * 5000)), webhook_http.EXCERPT_CHARS)
        self.assertEqual(clean_excerpt(b"\xff\xfe ok"), "\ufffd\ufffd ok")


class SendTests(unittest.TestCase):
    def receiver(self, **kwargs):
        r = Receiver(**kwargs)
        self.addCleanup(r.close)
        return r

    def send(self, r, **kwargs):
        kwargs.setdefault("allow_private", True)
        kwargs.setdefault("timeout", 5)
        return post(r.url, kwargs.pop("body", BODY), kwargs.pop("headers", HEADERS), **kwargs)

    def test_posts_the_body_and_headers_to_the_url(self):
        r = self.receiver()
        result = self.send(r)
        self.assertEqual((result.ok, result.status, result.error, result.excerpt), (True, 200, None, "ok"))
        self.assertGreaterEqual(result.duration_ms, 0)
        (seen,) = r.requests
        self.assertEqual(seen["path"], "/hooks/zylker?key=abc")  # path and query survive
        self.assertEqual(seen["raw"], BODY)
        self.assertEqual((seen["headers"]["Content-Type"], seen["headers"]["X-Zylker-Event"]), ("application/json", "task.created"))
        self.assertEqual(seen["headers"]["Host"], "127.0.0.1:%d" % r.port)

    def test_any_non_2xx_answer_is_a_failure_with_status_and_excerpt(self):
        for status in (301, 400, 404, 410, 429, 500, 503):
            r = self.receiver(status=status, body=b"upstream  exploded\n\x07bell")
            result = self.send(r)
            self.assertEqual((result.ok, result.status, result.error, result.excerpt), (False, status, "http_%d" % status, "upstream exploded bell"), status)
        for status in (200, 201, 202, 204, 299):
            self.assertTrue(self.send(self.receiver(status=status, body=b"" if status == 204 else b"fine")).ok, status)

    def test_redirects_are_never_followed(self):
        target = self.receiver()
        r = self.receiver(status=302, headers=[("Location", target.url)])
        result = self.send(r)
        self.assertEqual((result.ok, result.error), (False, "http_302"))
        self.assertEqual((len(r.requests), len(target.requests)), (1, 0))

    def test_a_slow_receiver_times_out(self):
        r = self.receiver(delay=1.5)
        started = time.monotonic()
        result = self.send(r, timeout=0.3)
        self.assertEqual((result.ok, result.status, result.error), (False, None, "timeout"))
        self.assertLess(time.monotonic() - started, 1.2)

    def test_connection_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        result = post("http://127.0.0.1:%d/x" % port, BODY, HEADERS, allow_private=True, timeout=2)
        self.assertEqual((result.ok, result.error), (False, "connection_failed"))

    def test_a_huge_response_is_not_read_to_the_end(self):
        r = self.receiver(body=b"x" * 5_000_000)
        started = time.monotonic()
        result = self.send(r)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.excerpt), webhook_http.EXCERPT_CHARS)
        self.assertLess(time.monotonic() - started, 3)

    def test_dns_failures(self):
        def broken(host, port):
            raise socket.gaierror(-2, "Name or service not known")

        for resolver in (broken, lambda host, port: []):
            result = post("http://hooks.example.com/x", BODY, HEADERS, resolver=resolver)
            self.assertEqual((result.ok, result.status, result.error), (False, None, "dns_error"))

    def test_problems_with_the_request_itself_are_reported_not_raised(self):
        r = self.receiver()
        result = self.send(r, headers={**HEADERS, "X-Bad": "line\nbreak"})
        self.assertEqual((result.ok, result.error), (False, "invalid_request"))
        self.assertEqual(r.requests, [])
        self.assertEqual(post("http://[::1", BODY, HEADERS).error, "invalid_request")
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("a URL without a host must never reach the resolver")):
            for url in ("http:///path", "http://:8080/x", "not a url"):
                self.assertEqual(post(url, BODY, HEADERS, allow_private=True).error, "invalid_request", url)

    def test_a_server_that_hangs_up_is_a_protocol_error_not_a_crash(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def hang_up():
            conn, _ = server.accept()
            conn.recv(65536)
            conn.close()

        threading.Thread(target=hang_up, daemon=True).start()
        result = post("http://127.0.0.1:%d/x" % server.getsockname()[1], BODY, HEADERS, allow_private=True, timeout=2)
        self.assertFalse(result.ok)
        self.assertIn(result.error, ("protocol_error", "connection_failed"))


class BoundedReadTests(unittest.TestCase):
    """Only the start of an answer is ever read, however much the receiver sends or however slowly."""

    class Endless:
        def __init__(self, pause=0.0, drip=None):
            self.requested, self.pause, self.drip = [], pause, drip

        def read1(self, n):
            self.requested.append(n)
            time.sleep(self.pause)
            return b"x" * (self.drip or n)

    def test_reading_stops_at_the_limit(self):
        response = self.Endless()
        data = webhook_http._read_limited(response, time.monotonic() + 30)
        self.assertEqual(len(data), webhook_http.MAX_READ)
        self.assertEqual(response.requested, [webhook_http.MAX_READ])  # one request for exactly the allowance, never for more

    def test_reading_stops_at_the_deadline(self):
        response = self.Endless(pause=0.05, drip=1)  # a receiver that dribbles one byte at a time
        started = time.monotonic()
        data = webhook_http._read_limited(response, started + 0.2)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertLess(len(data), webhook_http.MAX_READ)

    def test_a_broken_body_does_not_change_the_outcome(self):
        class Broken:
            def read1(self, n):
                raise http.client.IncompleteRead(b"")

        self.assertEqual(webhook_http._read_limited(Broken(), time.monotonic() + 5), b"")


class SsrfTests(unittest.TestCase):
    """The delivery-time guard: what a host name *resolves to* decides, and the vetted address is the one used."""

    def refuse_to_connect(self):
        patcher = mock.patch("socket.create_connection", side_effect=AssertionError("must not connect"))
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)

    def test_names_that_resolve_to_private_addresses_are_never_contacted(self):
        self.refuse_to_connect()
        for answer in (["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["::1"], ["::ffff:127.0.0.1"], ["fe80::1"], ["100.64.1.1"], ["0.0.0.0"],
                       ["93.184.216.34", "10.0.0.5"], ["10.0.0.5", "93.184.216.34"]):  # one bad answer poisons the lot
            result = post("http://hooks.example.com/x", BODY, HEADERS, resolver=lambda host, port, a=answer: a)
            self.assertEqual((result.ok, result.status, result.error), (False, None, "blocked_address"), answer)
        self.connect.assert_not_called()

    def test_the_connection_goes_to_the_vetted_address_not_to_the_name(self):
        with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError) as connect:
            result = post("http://hooks.example.com/x", BODY, HEADERS, resolver=lambda host, port: ["93.184.216.34"], timeout=7)
            self.assertEqual(result.error, "connection_failed")
            connect.assert_called_once_with(("93.184.216.34", 80), 7)
        with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError) as connect:
            post("https://hooks.example.com/x", BODY, HEADERS, resolver=lambda host, port: ["93.184.216.34"], timeout=7)
            connect.assert_called_once_with(("93.184.216.34", 443), 7)
        with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError) as connect:
            post("http://hooks.example.com:8080/x", BODY, HEADERS, resolver=lambda host, port: ["2606:4700:4700::1111"])
            self.assertEqual(connect.call_args.args[0], ("2606:4700:4700::1111", 8080))

    def test_a_name_is_resolved_once_so_it_cannot_change_its_answer_after_the_check(self):
        answers = [["93.184.216.34"], ["127.0.0.1"], ["127.0.0.1"]]  # a rebinding DNS server: public first, loopback afterwards
        resolver = mock.Mock(side_effect=lambda host, port: answers.pop(0))
        with mock.patch("socket.create_connection", side_effect=ConnectionRefusedError) as connect:
            post("http://rebind.example.com/x", BODY, HEADERS, resolver=resolver)
        self.assertEqual(resolver.call_count, 1)
        connect.assert_called_once()
        self.assertEqual(connect.call_args.args[0][0], "93.184.216.34")

    def test_the_next_address_is_tried_when_the_first_does_not_answer(self):
        r = Receiver()
        self.addCleanup(r.close)
        real = socket.create_connection
        seen = []

        def connect(address, timeout=None, *args, **kwargs):
            seen.append(address[0])
            if address[0] == "93.184.216.34":
                raise ConnectionRefusedError
            return real(("127.0.0.1", r.port), timeout)  # stand-in for the second public address

        with mock.patch("socket.create_connection", side_effect=connect):
            result = post("http://hooks.example.com/x", BODY, HEADERS, resolver=lambda host, port: ["93.184.216.34", "8.8.8.8"])
        self.assertEqual((result.ok, seen), (True, ["93.184.216.34", "8.8.8.8"]))

    def test_the_operator_can_allow_private_destinations(self):
        r = Receiver()
        self.addCleanup(r.close)
        result = post(r.url, BODY, HEADERS, allow_private=True, resolver=lambda host, port: ["127.0.0.1"])
        self.assertTrue(result.ok)
        self.refuse_to_connect()
        blocked = post(r.url, BODY, HEADERS, allow_private=False)
        self.assertEqual(blocked.error, "blocked_address")  # same URL, default settings: loopback is refused before any connection

    def test_localhost_is_refused_by_default_even_though_it_is_a_name(self):
        self.refuse_to_connect()
        result = post("http://localhost:9/x", BODY, HEADERS)
        self.assertEqual(result.error, "blocked_address")


@unittest.skipUnless(shutil.which("openssl"), "the openssl command line tool is needed to make a test certificate")
class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.cert, key = os.path.join(cls.tmp, "cert.pem"), os.path.join(cls.tmp, "key.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cls.cert, "-days", "2", "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost"],
            check=True, capture_output=True,
        )  # fmt: skip

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"secure")

            def log_message(self, *args):
                pass

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.socket = context.wrap_socket(cls.httpd.socket, server_side=True)
        cls.port = cls.httpd.server_port
        threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, True)

    def test_certificates_are_verified_by_default(self):
        result = post("https://localhost:%d/x" % self.port, BODY, HEADERS, allow_private=True, timeout=5, resolver=lambda h, p: ["127.0.0.1"])
        self.assertEqual((result.ok, result.error), (False, "tls_error"))  # a self-signed certificate is not trusted

    def test_a_trusted_certificate_works_and_the_name_in_the_url_is_what_gets_checked(self):
        trust = ssl.create_default_context(cafile=self.cert)
        with mock.patch.object(webhook_http, "ssl_context", return_value=trust):
            ok = post("https://localhost:%d/x" % self.port, BODY, HEADERS, allow_private=True, timeout=5, resolver=lambda h, p: ["127.0.0.1"])
            self.assertEqual((ok.ok, ok.status, ok.excerpt), (True, 200, "secure"))  # connected to the pinned IP, verified as "localhost"
            wrong_name = post("https://127.0.0.1:%d/x" % self.port, BODY, HEADERS, allow_private=True, timeout=5)
            self.assertEqual((wrong_name.ok, wrong_name.error), (False, "tls_error"))  # the certificate is for localhost, not for the IP


class PinnedConnectionTests(unittest.TestCase):
    def test_the_pinned_classes_are_real_http_connections(self):
        self.assertTrue(issubclass(webhook_http._PinnedHTTP, http.client.HTTPConnection))
        self.assertTrue(issubclass(webhook_http._PinnedHTTPS, http.client.HTTPSConnection))


if __name__ == "__main__":
    unittest.main()
