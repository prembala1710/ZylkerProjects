"""Task attachments: upload and download, permissions, limits, name and type cleaning, audit trail, cascades, backup."""

import contextlib
import dataclasses
import hashlib
import io
import os
import sqlite3
import unittest
from unittest import mock

from attachment_helpers import ALL_BYTES, GIF, HTML, JPEG, SVG, WEBP, make_png
from helpers import ApiTestCase

from zylker_pm.app import App
from zylker_pm.cli import main as cli_main
from zylker_pm.config import Config
from zylker_pm.db import SCHEMA_VERSION
from zylker_pm.services import attachments
from zylker_pm.services.attachments import clean_content_type, clean_filename, content_disposition, sniff_image


class AttachmentCase(ApiTestCase):
    """Project WEB (members bob and carol, managed by alice) with one task, WEB-1."""

    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.task = self.make_task("WEB", "Fix the login page")["key"]

    def upload(self, user, data=b"hello", filename="note.txt", content_type="text/plain", key=None, **extra):
        query = {"filename": filename} if filename is not None else {}
        return self.client(user).request("POST", "/api/tasks/%s/attachments" % (key or self.task), raw=data, content_type=content_type, query=query, **extra)

    def attach(self, user="bob", data=b"hello", filename="note.txt", **kw):
        return self.ok(self.upload(user, data, filename, **kw), 201)

    def download(self, user, attachment_id):
        return self.client(user).get("/api/attachments/%d/content" % attachment_id)

    def sql(self, query, args=()):
        conn = sqlite3.connect(self.config.db_path)
        try:
            return conn.execute(query, args).fetchall()
        finally:
            conn.close()

    def reconfigure(self, **changes):
        self.config = dataclasses.replace(self.config, **changes)
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.admin = self.client("admin")

    def counts(self):
        return self.sql("SELECT (SELECT COUNT(*) FROM attachments), (SELECT COUNT(*) FROM attachment_blobs)")[0]


# ---------------------------------------------------------------------------------- schema


class SchemaTests(AttachmentCase):
    def test_the_tables_exist_and_the_schema_version_is_untouched(self):
        names = {r[0] for r in self.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"attachments", "attachment_blobs"} <= names)
        self.assertEqual(self.sql("PRAGMA user_version")[0][0], SCHEMA_VERSION)  # additive: no migration, no version bump

    def test_a_database_without_the_tables_gains_them_when_the_app_starts(self):
        self.attach()
        self.sql("SELECT 1")
        conn = sqlite3.connect(self.config.db_path)
        conn.executescript("DROP TABLE attachment_blobs; DROP TABLE attachments;")
        conn.close()
        App(self.config)  # what every start of the server does
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(self.ok(self.client("bob").get("/api/tasks/WEB-1/attachments"))["items"], [])
        self.assertEqual(self.ok(self.admin.get("/api/tasks/WEB-1"))["title"], "Fix the login page")  # nothing else was touched

    def test_creating_the_tables_is_idempotent_and_ignores_missing_and_memory_databases(self):
        for _ in range(3):
            attachments.ensure_schema(self.config.db_path)
        attachments.ensure_schema(":memory:")
        missing = os.path.join(self.tmp, "never-created.db")
        attachments.ensure_schema(missing)
        self.assertFalse(os.path.exists(missing))  # a path that is not a database is not turned into one
        self.assertEqual(self.counts(), (0, 0))

    def test_missing_tables_are_reported_as_a_conflict_not_a_crash(self):
        conn = sqlite3.connect(self.config.db_path)
        conn.executescript("DROP TABLE attachment_blobs; DROP TABLE attachments;")
        conn.close()
        for response in (self.upload("bob"), self.client("bob").get("/api/tasks/WEB-1/attachments"), self.client("bob").get("/api/projects/WEB/attachments")):
            self.assertEqual((response.status, response.error_code), (409, "attachments_not_initialised"))
        self.assertEqual(self.client("bob").delete("/api/attachments/1").error_code, "attachments_not_initialised")
        self.assertEqual(self.client("bob").get("/api/attachments/1/content").error_code, "attachments_not_initialised")


# ---------------------------------------------------------------------------------- names, types, previews


class CleaningTests(unittest.TestCase):
    def test_file_names_lose_paths_controls_and_direction_tricks(self):
        cases = {
            "report.pdf": "report.pdf",
            "../../etc/passwd": "passwd",
            "..\\..\\windows\\system32\\evil.dll": "evil.dll",
            "C:\\Users\\bob\\Desktop\\plan.xlsx": "plan.xlsx",
            "/absolute/path/spec.md": "spec.md",
            "a\r\nb.txt": "a b.txt",
            "tab\there.txt": "tab here.txt",
            "nul\x00byte.txt": "nulbyte.txt",
            "invoice\u202egnp.exe": "invoicegnp.exe",
            "\u200bhidden\u200e.txt": "hidden.txt",
            "  spaced    out  .txt  ": "spaced out .txt",
            "trailing dots...": "trailing dots",
            ".env": ".env",
            "": "file",
            "   ": "file",
            "...": "file",
            "folder/": "file",
            "..": "file",
            None: "file",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_filename(raw), expected)

    def test_unicode_is_kept_and_normalised(self):
        self.assertEqual(clean_filename("re\u0301sume\u0301.pdf"), "r\u00e9sum\u00e9.pdf")  # NFC: one form for search and display
        self.assertEqual(clean_filename("\u62a5\u544a \u2013 \u0642\u0627\u0626\u0645\u0629.docx"), "\u62a5\u544a \u2013 \u0642\u0627\u0626\u0645\u0629.docx")
        self.assertEqual(clean_filename("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.txt"), "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.txt")  # joiners some scripts need

    def test_long_names_are_shortened_but_keep_their_extension(self):
        name = clean_filename("x" * 400 + ".pdf")
        self.assertEqual((len(name), name[-4:]), (180, ".pdf"))
        self.assertEqual(len(clean_filename("y" * 400)), 180)
        self.assertEqual(clean_filename("z" * 300 + "." + "e" * 40)[:3], "zzz")  # an absurd "extension" is not preserved
        self.assertEqual(len(clean_filename("z" * 300 + "." + "e" * 40)), 180)
        self.assertEqual(clean_filename("." * 300 + ".txt"), "file")  # nothing usable is left after trimming

    def test_declared_types_are_labels_that_must_be_well_formed(self):
        cases = [
            ("image/png", "x", "image/png"),
            ("IMAGE/PNG; charset=binary", "x", "image/png"),
            ("application/pdf", "a.bin", "application/pdf"),
            ("application/octet-stream", "a.pdf", "application/pdf"),  # "don't know" falls back to the extension
            ("", "a.csv", "text/csv"),
            (None, "photo.jpeg", "image/jpeg"),
            ("text/html\r\nSet-Cookie: x=1", "a.txt", "text/plain"),  # header injection through the type is impossible
            ("not a type", "a.zip", "application/zip"),
            ("a/b/c", "a.png", "image/png"),
            ("text/", "a.txt", "text/plain"),
            ("x" * 200 + "/y", "noext", "application/octet-stream"),
            (None, "no-extension", "application/octet-stream"),
            (None, "weird.zzzunknown", "application/octet-stream"),
        ]
        for declared, filename, expected in cases:
            with self.subTest(declared=declared, filename=filename):
                self.assertEqual(clean_content_type(declared, filename), expected)

    def test_only_real_raster_images_can_be_previewed(self):
        self.assertEqual(sniff_image(make_png()), "image/png")
        self.assertEqual(sniff_image(JPEG), "image/jpeg")
        self.assertEqual(sniff_image(GIF), "image/gif")
        self.assertEqual(sniff_image(WEBP), "image/webp")
        for data in (b"", b"\x89PNG", SVG, HTML, b"RIFF\x00\x00\x00\x00WAVEfmt ", b"GIF90a", b"%PDF-1.7", b"PK\x03\x04", ALL_BYTES):
            self.assertIsNone(sniff_image(data), data[:12])

    def test_the_download_header_always_says_attachment_and_cannot_be_injected(self):
        self.assertEqual(content_disposition("plan.pdf"), "attachment; filename=\"plan.pdf\"; filename*=UTF-8''plan.pdf")
        self.assertEqual(
            content_disposition('r\u00e9sum\u00e9 "final".pdf'),
            "attachment; filename=\"r_sum_ _final_.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9%20%22final%22.pdf",
        )
        for nasty in ('a";\r\nSet-Cookie: x=1', "back\\slash%00.txt", "\u202e.txt", "semi;colon,comma.txt"):
            header = content_disposition(clean_filename(nasty))
            self.assertTrue(header.startswith("attachment; filename=\""), header)
            self.assertTrue(header.isascii() and "\r" not in header and "\n" not in header, header)


# ---------------------------------------------------------------------------------- upload and download


class UploadTests(AttachmentCase):
    def test_a_file_survives_the_round_trip_byte_for_byte(self):
        made = self.attach("bob", ALL_BYTES, "all-bytes.bin", content_type="application/octet-stream")
        self.assertEqual(
            made,
            {
                "id": 1, "task": "WEB-1", "filename": "all-bytes.bin", "content_type": "application/octet-stream", "size": 2048,
                "sha256": hashlib.sha256(ALL_BYTES).hexdigest(), "preview": None,
                "uploaded_by": {"id": made["uploaded_by"]["id"], "username": "bob", "display_name": "Bob"},
                "created_at": "2026-10-16T09:00:00Z",
            },
        )  # fmt: skip
        got = self.download("carol", made["id"])
        self.assertEqual((got.status, got.body, got.headers["Content-Type"]), (200, ALL_BYTES, "application/octet-stream"))
        listing = self.ok(self.client("vera").get("/api/tasks/WEB-1/attachments"))
        self.assertEqual(listing["items"], [made])
        self.assertEqual(listing["total_bytes"], 2048)

    def test_the_listing_reports_the_limits_so_a_client_can_check_before_sending(self):
        listing = self.ok(self.client("bob").get("/api/tasks/WEB-1/attachments"))
        self.assertEqual(listing, {"items": [], "total_bytes": 0, "limits": {"max_file_bytes": 10 * 1024 * 1024, "max_per_task": 25}})

    def test_files_are_listed_oldest_first_and_belong_to_their_task(self):
        other = self.make_task("WEB", "Another")["key"]
        first = self.attach("bob", b"1", "first.txt")
        second = self.attach("carol", b"22", "second.txt")
        elsewhere = self.attach("bob", b"333", "elsewhere.txt", key=other)
        mine = self.ok(self.client("bob").get("/api/tasks/WEB-1/attachments"))
        self.assertEqual([a["filename"] for a in mine["items"]], ["first.txt", "second.txt"])
        self.assertEqual(mine["total_bytes"], 3)
        self.assertEqual([a["id"] for a in self.ok(self.client("bob").get("/api/tasks/%s/attachments" % other))["items"]], [elsewhere["id"]])
        self.assertEqual((first["id"], second["id"]), (1, 2))

    def test_the_declared_type_is_kept_as_a_label_and_the_extension_fills_a_gap(self):
        self.assertEqual(self.attach(filename="a.bin", content_type="application/pdf")["content_type"], "application/pdf")
        self.assertEqual(self.attach(filename="b.pdf", content_type="application/octet-stream")["content_type"], "application/pdf")
        self.assertEqual(self.attach(filename="c.csv", content_type=None)["content_type"], "text/csv")
        self.assertEqual(self.attach(filename="d", content_type="text/html; charset=utf-8")["content_type"], "text/html")

    def test_a_preview_is_offered_for_real_images_only_whatever_they_claim_to_be(self):
        png = self.attach(data=make_png(), filename="shot.png", content_type="image/png")
        self.assertEqual(png["preview"], "image/png")
        self.assertEqual(self.attach(data=make_png(), filename="no-extension", content_type="application/octet-stream")["preview"], "image/png")
        self.assertEqual(self.attach(data=JPEG, filename="p.jpg", content_type="image/jpeg")["preview"], "image/jpeg")
        self.assertEqual(self.attach(data=GIF, filename="a.gif", content_type="image/gif")["preview"], "image/gif")
        self.assertEqual(self.attach(data=WEBP, filename="a.webp", content_type="image/webp")["preview"], "image/webp")
        for data, name, claimed in ((HTML, "fake.png", "image/png"), (SVG, "logo.svg", "image/svg+xml"), (b"just text", "x.jpg", "image/jpeg")):
            self.assertIsNone(self.attach(data=data, filename=name, content_type=claimed)["preview"], name)

    def test_the_file_name_in_the_request_is_cleaned_before_it_is_stored(self):
        self.assertEqual(self.attach(filename="../../etc/passwd")["filename"], "passwd")
        self.assertEqual(self.attach(filename="C:\\temp\\plan.pdf")["filename"], "plan.pdf")
        self.assertEqual(self.attach(filename="a\r\nX-Evil: 1.txt")["filename"], "a X-Evil: 1.txt")
        self.assertEqual(self.attach(filename="Z\u00fcrich \u2013 Plan.pdf")["filename"], "Z\u00fcrich \u2013 Plan.pdf")
        self.assertEqual(self.attach(filename="..")["filename"], "file")

    def test_the_file_name_is_required_and_the_file_must_not_be_empty(self):
        for name in (None, "", "   "):
            r = self.upload("bob", b"data", name)
            self.assertEqual((r.status, list(r.json["error"]["details"])), (422, ["filename"]), repr(name))
        r = self.upload("bob", b"", "empty.txt")
        self.assertEqual((r.status, list(r.json["error"]["details"])), (422, ["file"]))
        self.assertEqual(self.counts(), (0, 0))

    def test_a_missing_content_type_is_fine(self):
        self.assertEqual(self.attach(filename="notes.txt", content_type=None)["content_type"], "text/plain")

    def test_identical_uploads_are_kept_separately(self):
        a, b = self.attach(data=b"same", filename="same.txt"), self.attach(data=b"same", filename="same.txt")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["sha256"], b["sha256"])


class DownloadTests(AttachmentCase):
    def test_a_download_is_always_an_attachment_with_hardening_headers(self):
        for data, name, claimed in ((HTML, "page.html", "text/html"), (SVG, "logo.svg", "image/svg+xml"), (make_png(), "shot.png", "image/png"), (b"x", "a.js", "text/javascript")):
            made = self.attach(data=data, filename=name, content_type=claimed)
            r = self.download("vera", made["id"])
            self.assertEqual(r.status, 200, name)
            self.assertTrue(r.headers["Content-Disposition"].startswith("attachment;"), r.headers["Content-Disposition"])
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
            self.assertEqual(r.body, data)

    def test_non_ascii_names_are_sent_in_both_header_forms(self):
        made = self.attach(filename='r\u00e9sum\u00e9 "final".pdf', content_type="application/pdf")
        r = self.download("bob", made["id"])
        self.assertEqual(
            r.headers["Content-Disposition"],
            "attachment; filename=\"r_sum_ _final_.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9%20%22final%22.pdf",
        )
        self.assertEqual(r.headers["Content-Type"], "application/pdf")

    def test_head_returns_the_headers_without_the_file(self):
        made = self.attach(data=b"x" * 500, filename="big.txt")
        r = self.client("bob").request("HEAD", "/api/attachments/%d/content" % made["id"])
        self.assertEqual((r.status, r.body), (200, b""))
        self.assertEqual(r.headers["Content-Type"], "text/plain")

    def test_unknown_and_malformed_ids_are_404s(self):
        for path in ("/api/attachments/999/content", "/api/attachments/abc/content", "/api/attachments/-1/content", "/api/attachments/0/content"):
            r = self.client("bob").get(path)
            self.assertEqual((r.status, r.error_code), (404, "attachment_not_found"), path)
        self.assertEqual(self.client("bob").delete("/api/attachments/abc").error_code, "attachment_not_found")

    def test_a_file_whose_content_row_is_gone_is_a_clean_404(self):
        made = self.attach()
        conn = sqlite3.connect(self.config.db_path)
        conn.execute("DELETE FROM attachment_blobs")
        conn.commit()
        conn.close()
        r = self.download("bob", made["id"])
        self.assertEqual((r.status, r.error_code), (404, "attachment_content_missing"))

    def test_every_endpoint_needs_a_token(self):
        made = self.attach()
        anonymous = self.client()
        for method, path in (
            ("GET", "/api/tasks/WEB-1/attachments"), ("POST", "/api/tasks/WEB-1/attachments"), ("GET", "/api/attachments/%d/content" % made["id"]),
            ("DELETE", "/api/attachments/%d" % made["id"]), ("GET", "/api/projects/WEB/attachments"),
        ):  # fmt: skip
            self.assertEqual(anonymous.request(method, path, raw=b"x", content_type="text/plain", query={"filename": "a.txt"}).status, 401, path)


# ---------------------------------------------------------------------------------- who may do what


class PermissionTests(AttachmentCase):
    def test_members_of_the_project_can_attach_and_everyone_signed_in_can_read(self):
        self.assertEqual(self.upload("bob").status, 201)
        self.assertEqual(self.upload("carol").status, 201)
        for user in ("alice", "admin", "vera", "bob"):
            self.assertEqual(len(self.ok(self.client(user).get("/api/tasks/WEB-1/attachments"))["items"]), 2, user)
            self.assertEqual(self.download(user, 1).status, 200, user)
            self.assertEqual(self.ok(self.client(user).get("/api/projects/WEB/attachments"))["total"], 2, user)

    def test_viewers_cannot_attach_or_delete(self):
        made = self.attach()
        r = self.upload("vera")
        self.assertEqual((r.status, r.error_code), (403, "insufficient_role"))
        r = self.client("vera").delete("/api/attachments/%d" % made["id"])
        self.assertEqual((r.status, r.error_code), (403, "insufficient_role"))
        self.assertEqual(self.counts(), (1, 1))

    def test_a_member_outside_the_project_cannot_write_but_managers_and_admins_can(self):
        self.make_project("OPS", members=("carol",))
        self.make_task("OPS", "Outage")
        r = self.upload("bob", key="OPS-1")
        self.assertEqual((r.status, r.error_code), (403, "not_project_member"))
        self.make_user("mona", "manager")
        for user in ("mona", "admin", "alice"):
            self.assertEqual(self.upload(user, key="OPS-1").status, 201, user)
        self.assertEqual(self.upload("carol", key="OPS-1").status, 201)

    def test_only_the_uploader_or_a_manager_can_delete(self):
        mine = self.attach("bob", filename="bobs.txt")
        theirs = self.attach("carol", filename="carols.txt")
        r = self.client("carol").delete("/api/attachments/%d" % mine["id"])
        self.assertEqual((r.status, r.error_code), (403, "not_author"))
        self.assertEqual(self.client("bob").delete("/api/attachments/%d" % mine["id"]).status, 204)
        self.assertEqual(self.client("alice").delete("/api/attachments/%d" % theirs["id"]).status, 204)  # a manager can
        self.assertEqual(self.counts(), (0, 0))
        again = self.client("bob").delete("/api/attachments/%d" % mine["id"])
        self.assertEqual((again.status, again.error_code), (404, "attachment_not_found"))

    def test_an_archived_project_is_read_only_but_its_files_stay_available(self):
        made = self.attach()
        self.ok(self.client("alice").patch("/api/projects/WEB", {"status": "archived"}))
        for r in (self.upload("bob"), self.client("alice").delete("/api/attachments/%d" % made["id"])):
            self.assertEqual((r.status, r.error_code), (409, "project_archived"))
        self.assertEqual(self.download("vera", made["id"]).body, b"hello")
        self.assertEqual(len(self.ok(self.client("vera").get("/api/tasks/WEB-1/attachments"))["items"]), 1)

    def test_unknown_tasks_and_projects_are_404s_and_bad_keys_400s(self):
        r = self.upload("bob", key="WEB-99")
        self.assertEqual((r.status, r.error_code), (404, "task_not_found"))
        self.assertEqual(self.upload("bob", key="nonsense").error_code, "invalid_key")
        self.assertEqual(self.client("bob").get("/api/tasks/WEB-99/attachments").error_code, "task_not_found")
        self.assertEqual(self.client("bob").get("/api/projects/NOPE/attachments").error_code, "project_not_found")

    def test_checks_happen_before_anything_is_stored_or_logged(self):
        before = self.sql("SELECT COUNT(*) FROM activity")[0][0]
        self.upload("vera")
        self.upload("bob", key="WEB-99")
        self.upload("bob", filename="")
        self.assertEqual((self.counts(), self.sql("SELECT COUNT(*) FROM activity")[0][0]), ((0, 0), before))


# ---------------------------------------------------------------------------------- limits


class LimitTests(AttachmentCase):
    def test_the_per_file_limit_is_enforced_before_the_body_is_read(self):
        self.reconfigure(attachment_max_bytes=1000)
        self.assertEqual(self.upload("bob", b"x" * 1000).status, 201)
        r = self.upload("bob", b"x" * 1001)
        self.assertEqual((r.status, r.error_code), (413, "payload_too_large"))
        unread = mock.MagicMock()
        unread.read.side_effect = AssertionError("an oversized body must not be read")
        captured = {}
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/tasks/WEB-1/attachments", "QUERY_STRING": "filename=big.bin", "CONTENT_LENGTH": "5000000",
            "CONTENT_TYPE": "application/octet-stream", "wsgi.input": unread, "HTTP_AUTHORIZATION": "Bearer " + self.tokens["bob"],
        }  # fmt: skip
        b"".join(self.app(environ, lambda status, headers: captured.update(status=status)))
        self.assertTrue(captured["status"].startswith("413"))
        unread.read.assert_not_called()
        self.assertEqual(self.counts(), (1, 1))

    def test_a_request_without_credentials_or_the_right_role_is_not_read_at_all(self):
        for token, expected in ((None, "401"), (self.tokens["vera"], "403")):
            unread = mock.MagicMock()
            unread.read.side_effect = AssertionError("the body of an unauthorised upload must not be read")
            environ = {
                "REQUEST_METHOD": "POST", "PATH_INFO": "/api/tasks/WEB-1/attachments", "QUERY_STRING": "filename=a.bin", "CONTENT_LENGTH": "1000",
                "CONTENT_TYPE": "application/octet-stream", "wsgi.input": unread,
            }  # fmt: skip
            if token:
                environ["HTTP_AUTHORIZATION"] = "Bearer " + token
            captured = {}
            b"".join(self.app(environ, lambda status, headers, sink=captured: sink.update(status=status)))
            self.assertTrue(captured["status"].startswith(expected), captured["status"])
            unread.read.assert_not_called()

    def test_a_task_holds_a_limited_number_of_files(self):
        other = self.make_task("WEB", "Elsewhere")["key"]
        with mock.patch.object(attachments, "MAX_PER_TASK", 3):
            for i in range(3):
                self.attach(filename="f%d.txt" % i)
            r = self.upload("bob", filename="one-too-many.txt")
            self.assertEqual((r.status, r.error_code), (409, "too_many_attachments"))
            self.assertEqual(self.upload("bob", key=other).status, 201)  # the allowance is per task
            self.assertEqual(self.client("bob").delete("/api/attachments/1").status, 204)
            self.assertEqual(self.upload("bob", filename="fits-now.txt").status, 201)
        self.assertEqual(self.counts()[0], 4)

    def test_total_storage_is_capped_and_deleting_frees_room(self):
        self.reconfigure(attachment_quota_bytes=100)
        first = self.attach(data=b"a" * 60)
        r = self.upload("bob", b"b" * 60, "second.txt")
        self.assertEqual((r.status, r.error_code), (409, "storage_quota_exceeded"))
        self.assertEqual(self.upload("bob", b"c" * 40, "fits.txt").status, 201)  # exactly 100 bytes in total
        self.assertEqual(self.upload("bob", b"d", "no-room.txt").error_code, "storage_quota_exceeded")
        self.assertEqual(self.client("bob").delete("/api/attachments/%d" % first["id"]).status, 204)
        self.assertEqual(self.upload("bob", b"e" * 60, "now-fits.txt").status, 201)

    def test_a_quota_of_zero_means_unlimited(self):
        self.reconfigure(attachment_quota_bytes=0)
        for i in range(3):
            self.attach(data=b"x" * 5000, filename="f%d.bin" % i)
        self.assertEqual(self.counts(), (3, 3))

    def test_half_a_file_is_never_stored(self):
        environ_body = io.BytesIO(b"only ten b")
        captured = {}
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/tasks/WEB-1/attachments", "QUERY_STRING": "filename=cut.bin", "CONTENT_LENGTH": "100",
            "CONTENT_TYPE": "application/octet-stream", "wsgi.input": environ_body, "HTTP_AUTHORIZATION": "Bearer " + self.tokens["bob"],
        }  # fmt: skip
        out = b"".join(self.app(environ, lambda status, headers: captured.update(status=status)))
        self.assertTrue(captured["status"].startswith("400"), captured)
        self.assertIn(b"incomplete_body", out)
        self.assertEqual(self.counts(), (0, 0))

    def test_a_cut_short_json_request_is_reported_as_such_rather_than_as_malformed_json(self):
        captured = {}
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/projects/WEB/tasks", "QUERY_STRING": "", "CONTENT_LENGTH": "60",
            "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(b'{"title": "cut'), "HTTP_AUTHORIZATION": "Bearer " + self.tokens["bob"],
        }  # fmt: skip
        out = b"".join(self.app(environ, lambda status, headers: captured.update(status=status)))
        self.assertTrue(captured["status"].startswith("400") and b"incomplete_body" in out, out)

    def test_a_chunked_upload_is_refused_with_an_explanation(self):
        captured = {}
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/tasks/WEB-1/attachments", "QUERY_STRING": "filename=a.bin", "CONTENT_TYPE": "application/octet-stream",
            "HTTP_TRANSFER_ENCODING": "chunked", "wsgi.input": io.BytesIO(b"5\r\nhello\r\n0\r\n\r\n"), "HTTP_AUTHORIZATION": "Bearer " + self.tokens["bob"],
        }  # fmt: skip
        out = b"".join(self.app(environ, lambda status, headers: captured.update(status=status)))
        self.assertTrue(captured["status"].startswith("400") and b"length_required" in out and b"Content-Length" in out, out)
        self.assertEqual(self.counts(), (0, 0))

    def test_a_body_that_cannot_be_read_is_an_incomplete_body(self):
        broken = mock.MagicMock()
        broken.read.side_effect = TimeoutError("timed out")
        captured = {}
        environ = {
            "REQUEST_METHOD": "POST", "PATH_INFO": "/api/tasks/WEB-1/attachments", "QUERY_STRING": "filename=a.bin", "CONTENT_LENGTH": "100",
            "CONTENT_TYPE": "application/octet-stream", "wsgi.input": broken, "HTTP_AUTHORIZATION": "Bearer " + self.tokens["bob"],
        }  # fmt: skip
        out = b"".join(self.app(environ, lambda status, headers: captured.update(status=status)))
        self.assertTrue(captured["status"].startswith("400") and b"incomplete_body" in out, out)


# ---------------------------------------------------------------------------------- audit trail, cascades, listings


class AuditTests(AttachmentCase):
    def test_uploads_and_deletions_are_in_the_audit_trail_without_the_content(self):
        made = self.attach("bob", b"secret contents", "plan.txt")
        self.client("alice").delete("/api/attachments/%d" % made["id"])
        feed = self.ok(self.client("vera").get("/api/tasks/WEB-1/activity"))["items"]
        added, deleted = (next(a for a in feed if a["action"] == name) for name in ("attachment.added", "attachment.deleted"))
        self.assertEqual((added["actor"]["username"], added["task"], added["project"]), ("bob", "WEB-1", "WEB"))
        self.assertEqual(added["details"], {"attachment_id": made["id"], "filename": "plan.txt", "size": 15})
        self.assertEqual((deleted["actor"]["username"], deleted["details"]["filename"]), ("alice", "plan.txt"))
        self.assertNotIn("secret contents", str(feed))
        project_feed = self.ok(self.client("vera").get("/api/projects/WEB/activity"))["items"]
        self.assertEqual([a["action"] for a in project_feed if a["action"].startswith("attachment.")], ["attachment.deleted", "attachment.added"])


class CascadeTests(AttachmentCase):
    def test_deleting_a_task_deletes_its_files_and_nothing_else(self):
        other = self.make_task("WEB", "Keep me")["key"]
        self.attach(key=self.task)
        self.attach(key=other, filename="stays.txt")
        self.assertEqual(self.client("alice").delete("/api/tasks/WEB-1").status, 204)
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.sql("SELECT filename FROM attachments")[0][0], "stays.txt")

    def test_deleting_a_parent_with_cascade_deletes_the_files_of_its_subtasks(self):
        child = self.ok(self.client("alice").post("/api/projects/WEB/tasks", {"title": "Child", "parent": self.task}), 201)["key"]
        self.attach(key=child)
        self.assertEqual(self.client("alice").delete("/api/tasks/WEB-1", query={"cascade": "true"}).status, 204)
        self.assertEqual(self.counts(), (0, 0))

    def test_deleting_a_project_deletes_its_files(self):
        self.make_project("OPS", members=("bob",))
        self.make_task("OPS", "Outage")
        self.attach(key="OPS-1")
        self.attach(key="WEB-1", filename="stays.txt")
        self.assertEqual(self.admin.delete("/api/projects/OPS", query={"confirm": "OPS"}).status, 204)
        self.assertEqual(self.counts(), (1, 1))


class ProjectListingTests(AttachmentCase):
    def setUp(self):
        super().setUp()
        self.second = self.make_task("WEB", "Second task")["key"]
        self.attach("bob", b"1", "design-notes.txt")
        self.attach("carol", b"22", "100%_real.csv", key=self.second)
        self.attach("bob", b"333", "Design System.pdf", key=self.second)

    def listing(self, **query):
        return self.ok(self.client("vera").get("/api/projects/WEB/attachments", query=query))

    def test_the_newest_files_come_first_with_their_task_and_the_totals(self):
        data = self.listing()
        self.assertEqual([a["filename"] for a in data["items"]], ["Design System.pdf", "100%_real.csv", "design-notes.txt"])
        self.assertEqual((data["total"], data["total_bytes"], data["limit"], data["offset"]), (3, 6, 25, 0))
        self.assertEqual((data["items"][0]["task"], data["items"][0]["task_title"]), ("WEB-2", "Second task"))
        self.assertNotIn("data", data["items"][0])

    def test_files_can_be_searched_by_name_and_wildcards_are_literal(self):
        self.assertEqual([a["filename"] for a in self.listing(q="design")["items"]], ["Design System.pdf", "design-notes.txt"])
        self.assertEqual(self.listing(q="DESIGN")["total"], 2)
        self.assertEqual([a["filename"] for a in self.listing(q="100%")["items"]], ["100%_real.csv"])
        self.assertEqual(self.listing(q="100%")["total_bytes"], 2)  # the totals follow the search
        self.assertEqual(self.listing(q="_")["total"], 1)  # "_" is not "any single character"
        self.assertEqual(self.listing(q="%")["total"], 1)
        self.assertEqual(self.listing(q="nothing like this")["items"], [])

    def test_paging(self):
        first = self.listing(limit=2)
        self.assertEqual(([a["filename"] for a in first["items"]], first["total"]), (["Design System.pdf", "100%_real.csv"], 3))
        self.assertEqual([a["filename"] for a in self.listing(limit=2, offset=2)["items"]], ["design-notes.txt"])
        self.assertEqual(self.listing(limit=500)["limit"], 100)  # capped

    def test_bad_paging_is_a_validation_error(self):
        for query in ({"limit": "x"}, {"limit": "0"}, {"offset": "-1"}, {"offset": "1.5"}):
            r = self.client("vera").get("/api/projects/WEB/attachments", query=query)
            self.assertEqual(r.status, 422, query)

    def test_other_projects_files_are_not_listed(self):
        self.make_project("OPS", members=("bob",))
        self.make_task("OPS", "Outage")
        self.attach(key="OPS-1", filename="ops-only.txt")
        self.assertEqual(self.listing()["total"], 3)
        self.assertEqual(self.ok(self.client("vera").get("/api/projects/OPS/attachments"))["total"], 1)


# ---------------------------------------------------------------------------------- operations


class BackupTests(AttachmentCase):
    def test_a_backup_contains_the_files(self):
        made = self.attach("bob", ALL_BYTES, "everything.bin")
        target = os.path.join(self.tmp, "backup.db")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["--db", self.config.db_path, "backup", target]), 0)
        conn = sqlite3.connect(target)
        try:
            name, data = conn.execute("SELECT a.filename, b.data FROM attachments a JOIN attachment_blobs b ON b.attachment_id = a.id WHERE a.id = ?", (made["id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual((name, data), ("everything.bin", ALL_BYTES))


class ConfigTests(unittest.TestCase):
    def test_the_limits_are_read_from_the_environment_in_megabytes(self):
        cfg = Config.from_env({"ZYLKER_ATTACHMENT_MAX_MB": "25", "ZYLKER_ATTACHMENT_QUOTA_MB": "500"})
        self.assertEqual((cfg.attachment_max_bytes, cfg.attachment_quota_bytes), (25 * 1024 * 1024, 500 * 1024 * 1024))
        defaults = Config.from_env({})
        self.assertEqual((defaults.attachment_max_bytes, defaults.attachment_quota_bytes), (10 * 1024 * 1024, 2048 * 1024 * 1024))
        self.assertEqual(Config.from_env({"ZYLKER_ATTACHMENT_MAX_MB": "0.5"}).attachment_max_bytes, 512 * 1024)
        self.assertEqual(Config.from_env({"ZYLKER_ATTACHMENT_QUOTA_MB": "0"}).attachment_quota_bytes, 0)

    def test_nonsense_is_refused_at_start_up(self):
        for env in (
            {"ZYLKER_ATTACHMENT_MAX_MB": "lots"}, {"ZYLKER_ATTACHMENT_MAX_MB": "nan"}, {"ZYLKER_ATTACHMENT_MAX_MB": "inf"},
            {"ZYLKER_ATTACHMENT_MAX_MB": "0"}, {"ZYLKER_ATTACHMENT_MAX_MB": "-5"}, {"ZYLKER_ATTACHMENT_MAX_MB": "101"},
            {"ZYLKER_ATTACHMENT_QUOTA_MB": "-1"}, {"ZYLKER_ATTACHMENT_QUOTA_MB": "plenty"},
        ):  # fmt: skip
            with self.assertRaises(ValueError, msg=str(env)):
                Config.from_env(env)

    def test_the_ceiling_exists_because_an_upload_is_held_in_memory(self):
        Config(attachment_max_bytes=100 * 1024 * 1024).validate()
        with self.assertRaises(ValueError):
            Config(attachment_max_bytes=100 * 1024 * 1024 + 1).validate()


if __name__ == "__main__":
    unittest.main()
