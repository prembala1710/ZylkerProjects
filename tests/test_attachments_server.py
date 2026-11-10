"""Uploads through the real threaded server (the stack ``zylker-pm serve`` runs), over real sockets."""

import dataclasses
import hashlib
import io
import json
import logging
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helpers import ApiTestCase

from zylker_pm.cli import QuietHandler, make_threaded_server


class LiveUploadCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        logging.getLogger("zylker.access").setLevel(logging.CRITICAL)
        self.make_project("WEB", members=("bob", "carol"))
        self.make_task("WEB", "Fix the login page")
        self.start()

    def start(self):
        self.server = make_threaded_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def call(self, method, path, data=None, token=None, content_type="application/json", timeout=10):
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", content_type)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def open_upload(self, length, sent=b"", token=None, path="/api/tasks/WEB-1/attachments?filename=slow.bin"):
        """Start an upload that promises ``length`` bytes and has delivered only ``sent`` of them."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        head = (
            "POST %s HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer %s\r\nContent-Type: application/octet-stream\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n" % (path, token or self.tokens["bob"], length)
        )
        sock.sendall(head.encode() + sent)
        return sock

    @staticmethod
    def read_response(sock):
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except (ConnectionResetError, TimeoutError):
                break
            if not data:
                break
            chunks.append(data)
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head else None
        return status, body

    def stored(self):
        import sqlite3

        conn = sqlite3.connect(self.config.db_path)
        try:
            return conn.execute("SELECT filename, size FROM attachments ORDER BY id").fetchall()
        finally:
            conn.close()


class RoundTripTests(LiveUploadCase):
    def test_a_multi_megabyte_file_goes_up_and_comes_back_identical(self):
        data = os.urandom(5 * 1024 * 1024 + 123)
        status, _, body = self.call("POST", "/api/tasks/WEB-1/attachments?filename=big.bin", data, self.tokens["bob"], "application/octet-stream")
        made = json.loads(body)
        self.assertEqual((status, made["size"], made["sha256"]), (201, len(data), hashlib.sha256(data).hexdigest()))
        status, headers, got = self.call("GET", "/api/attachments/%d/content" % made["id"], token=self.tokens["vera"])
        self.assertEqual((status, len(got), hashlib.sha256(got).hexdigest()), (200, len(data), made["sha256"]))
        self.assertEqual(int(headers["Content-Length"]), len(data))
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))

    def test_parallel_uploads_all_arrive_whole(self):
        results, errors = {}, []

        def worker(i):
            data = bytes([i]) * (300_000 + i)
            try:
                status, _, body = self.call("POST", "/api/tasks/WEB-1/attachments?filename=part%d.bin" % i, data, self.tokens["bob" if i % 2 else "carol"], "application/octet-stream")
                results[i] = (status, json.loads(body)["sha256"], hashlib.sha256(data).hexdigest())
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual({i: r[0] for i, r in results.items()}, {i: 201 for i in range(1, 9)})
        self.assertTrue(all(sent == stored for _, sent, stored in results.values()))
        self.assertEqual(len(self.stored()), 8)


class SlowClientTests(LiveUploadCase):
    def test_a_stalled_upload_does_not_hold_the_write_lock(self):
        sock = self.open_upload(5000, b"x" * 100)
        time.sleep(0.4)  # the server is now blocked reading the rest of the body
        started = time.monotonic()
        status, _, body = self.call("POST", "/api/tasks/WEB-1/comments", {"body": "written while an upload is in flight"}, self.tokens["carol"], timeout=4)
        self.assertEqual(status, 201, body)
        self.assertLess(time.monotonic() - started, 2.0)  # inside the transaction this would wait for the 10 s busy timeout
        sock.sendall(b"y" * 4900)
        status, body = self.read_response(sock)
        self.assertEqual((status, json.loads(body)["size"]), (201, 5000))
        self.assertEqual(self.stored(), [("slow.bin", 5000)])

    def test_an_upload_that_is_cut_off_stores_nothing(self):
        sock = self.open_upload(5000, b"z" * 100)
        time.sleep(0.2)
        sock.shutdown(socket.SHUT_WR)  # the client is gone after 100 of 5000 bytes
        status, body = self.read_response(sock)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "incomplete_body")
        self.assertEqual(self.stored(), [])
        status, _, _ = self.call("POST", "/api/tasks/WEB-1/comments", {"body": "the server carries on"}, self.tokens["bob"])
        self.assertEqual(status, 201)

    def test_a_client_that_stops_sending_is_dropped_after_the_timeout(self):
        with mock.patch.object(QuietHandler, "timeout", 0.5):
            self.server.shutdown()
            self.server.server_close()
            self.start()
            started = time.monotonic()
            stalled_body = self.open_upload(5000, b"q" * 50)
            status, body = self.read_response(stalled_body)
            self.assertEqual(status, 400)  # it is told why, then disconnected
            self.assertEqual(json.loads(body)["error"]["code"], "incomplete_body")
            silent = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            self.addCleanup(silent.close)
            silent.sendall(b"POST /api/tasks/WEB-1/attachments?filename=x HTTP/1.1\r\nHost: test\r\n")  # never finishes its headers
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(self.read_response(silent), (None, b""))
                time.sleep(0.2)
            self.assertEqual(stderr.getvalue(), "")  # a stalled client is not worth a stack trace
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.stored(), [])

    def test_a_client_that_sends_everything_slowly_is_not_dropped(self):
        with mock.patch.object(QuietHandler, "timeout", 1.0):
            self.server.shutdown()
            self.server.server_close()
            self.start()
            sock = self.open_upload(3000, b"a" * 1000)
            for _ in range(2):  # 0.6 s between pieces: slow, but never silent for the whole timeout
                time.sleep(0.6)
                sock.sendall(b"b" * 1000)
            status, body = self.read_response(sock)
        self.assertEqual((status, json.loads(body)["size"]), (201, 3000))


class OversizedTests(LiveUploadCase):
    def test_a_file_over_the_limit_is_refused_and_the_server_stays_healthy(self):
        self.app.config = dataclasses.replace(self.config, attachment_max_bytes=1000)
        sock = self.open_upload(1001, b"w" * 1001)
        status, body = self.read_response(sock)
        if status is not None:  # on some platforms the server's early close resets the connection before the reply is read
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(body)["error"]["code"], "payload_too_large")
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.call("GET", "/api/health")[0], 200)


class DefaultsTests(unittest.TestCase):
    def test_a_client_that_goes_silent_is_dropped_by_default(self):
        self.assertTrue(0 < QuietHandler.timeout <= 300)  # the tests above shorten it; this is what ships
