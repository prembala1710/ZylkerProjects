"""The Microsoft Project import takes a file from anybody who can reach the endpoint: hostile XML, wrong types, sizes, roles."""

import os
import time
from dataclasses import replace

from helpers import Client
from msproject_helpers import NS, MspCase, doc, task

from zylker_pm.app import App
from zylker_pm.errors import Forbidden, PayloadTooLarge
from zylker_pm.services import msproject, users

TASKS = "<Tasks><Task><UID>1</UID><Name>Planted</Name><OutlineLevel>1</OutlineLevel></Task></Tasks>"


def project(body, head=""):
    return ('<?xml version="1.0"?>%s<Project xmlns="%s">%s</Project>' % (head, NS, body)).encode("utf-8")


class HostileXmlTests(MspCase):
    def user_row(self, username):
        with self.app.session() as ctx:
            return users.find_by_username(ctx.conn, username)

    def refused(self, data, expect, status=422):
        before = self.counts()
        started = time.monotonic()
        r = self.post_xml(data)
        self.assertEqual(r.status, status, r.text[:300])
        self.assertIn(expect, r.text)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.counts(), before)  # nothing was written
        return r

    def test_a_doctype_is_refused_whatever_it_declares(self):
        for head in (
            '<!DOCTYPE Project [<!ENTITY a "b">]>',
            '<!DOCTYPE Project>',
            '<!DOCTYPE Project SYSTEM "http://127.0.0.1:9/never.dtd">',
            '<!DOCTYPE Project PUBLIC "-//X//Y" "http://127.0.0.1:9/never.dtd">',
            '<!-- a comment first --><!DOCTYPE Project [<!ELEMENT Project ANY>]>',
            '<!DOCTYPE Project [<!ENTITY % p SYSTEM "file:///etc/passwd"> %p;]>',
        ):
            self.refused(project(TASKS, head), "DOCTYPE or entity declarations are not accepted")

    def test_entity_expansion_bombs_are_stopped_before_anything_expands(self):
        laughs = ['<!ENTITY l0 "lol">'] + ['<!ENTITY l%d "%s">' % (i, "&l%d;" % (i - 1) * 10) for i in range(1, 10)]
        self.refused(project("<Tasks><Task><UID>1</UID><Name>&l9;</Name></Task></Tasks>", "<!DOCTYPE Project [%s]>" % "".join(laughs)), "DOCTYPE")

    def test_an_external_entity_never_reads_a_file_or_calls_out(self):
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w") as f:
            f.write("TOP-SECRET-CONTENT")
        r = self.refused(project("<Tasks><Task><UID>1</UID><Name>&xxe;</Name></Task></Tasks>", '<!DOCTYPE Project [<!ENTITY xxe SYSTEM "file://%s">]>' % secret), "DOCTYPE")
        self.assertNotIn("TOP-SECRET", r.text)
        self.assertEqual(self.tasks(), [])

    def test_the_encoding_cannot_be_used_to_hide_a_doctype(self):
        for bom, encoding in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):  # UTF-16 announced by its byte order mark
            text = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE Project [<!ENTITY a "b">]><Project xmlns="%s">%s</Project>' % (NS, TASKS)
            self.refused(bom + text.encode(encoding), "DOCTYPE")
        text = '<?xml version="1.0" encoding="ISO-8859-1"?><!DOCTYPE Project [<!ENTITY a "b">]><Project>' + TASKS + "</Project>"
        self.refused(text.encode("latin-1"), "DOCTYPE")

    def test_encodings_the_parser_cannot_read_are_a_clean_refusal_not_a_crash(self):
        for name in ("utf-16-le", "utf-32", "shift_jis", "no-such-encoding"):
            data = ('<?xml version="1.0" encoding="%s"?><Project><Tasks/></Project>' % name).encode("ascii")
            self.refused(data, "text encoding that is not supported")

    def test_files_that_are_not_well_formed_are_refused_with_a_line_number(self):
        broken = {
            "truncated": project(TASKS)[:-30],
            "mismatched": project("<Tasks><Task></Tasks></Task>"),
            "two roots": project(TASKS) + project(TASKS),
            "junk before": b"junk" + project(TASKS),
            "nothing but text": b"just some words, not xml",
            "control character": project("<Tasks><Task><UID>1</UID><Name>bell\x07</Name></Task></Tasks>"),
            "null reference": project("<Tasks><Task><UID>1</UID><Name>a&#0;b</Name></Task></Tasks>"),
            "undefined entity": project("<Tasks><Task><UID>1</UID><Name>&nbsp;</Name></Task></Tasks>"),
            "bad utf-8": project("<Tasks><Task><UID>1</UID><Name>").replace(b"</Project>", b"") + b"\xff\xfe\xfa</Name></Task></Tasks></Project>",
            "unbound prefix": project("<x:Tasks/>"),
        }
        for name, data in broken.items():
            r = self.refused(data, "not well-formed XML (line")
            self.assertEqual(r.error_code, "validation_error", name)

    def test_other_kinds_of_file_are_recognised_as_such(self):
        self.refused(b"", "the file is empty")
        self.refused(b"  \n ", "the file is empty")
        self.refused(b'<html xmlns="http://www.w3.org/1999/xhtml"><body>hi</body></html>', "the top element is <html>, not <Project>")
        self.refused(b'<project><tasks><task name="x"/></tasks></project>', "not <Project>")  # GanttProject's lower-case root
        self.refused(b'{"tasks": []}', "not well-formed XML")
        self.refused(b"PK\x03\x04binary", "not well-formed XML")
        self.refused(project("<Resources/>"), "has no <Tasks> section")
        self.refused(project("<Other><Tasks/></Other>"), "has no <Tasks> section")

    def test_only_tasks_in_the_right_place_count(self):
        data = project("<Decoys><Tasks><Task><UID>9</UID><Name>nested decoy</Name></Task></Decoys>".replace("</Decoys>", "</Tasks></Decoys>")
                       + "<Tasks><Group><Task><UID>8</UID><Name>wrapped decoy</Name></Task></Group><Task><UID>1</UID><Name>Real</Name><Baseline><Task><UID>7</UID><Name>inner decoy</Name></Task></Baseline></Task></Tasks>")  # fmt: skip
        self.assertEqual(self.run_import(data)["created"], 1)
        self.assertEqual(list(self.by_title()), ["Real"])

    def test_nesting_and_element_counts_are_bounded(self):
        self.refused(project("<Tasks>" + "<a>" * 300 + "</a>" * 300 + "</Tasks>"), "nested too deeply")
        self.refused(project("<Tasks>" + "<Task><UID>1</UID></Task>" * 10_001 + "</Tasks>"), "too many tasks")
        self.refused(project("<Resources>" + "<Resource><UID>1</UID></Resource>" * 50_001 + "</Resources><Tasks/>"), "too many resources")

    def test_huge_text_is_cut_and_never_stored_whole(self):
        r = self.run_import(project("<Tasks><Task><UID>1</UID><Name>%s</Name><Notes>%s</Notes></Task></Tasks>" % ("t" * 1_500_000, "n" * 2_500_000)))
        (stored,) = self.tasks()
        self.assertEqual((len(stored["title"]), len(stored["description"])), (200, 20000))
        self.assertEqual(len(r["warnings"]), 2)

    def test_the_ordinary_xml_features_of_a_real_file_are_fine(self):
        data = project('<Tasks><Task><UID>1</UID><Name><![CDATA[<b>Fish & chips</b>]]></Name><Notes>caf&#233; &#x1F600; &lt;ok&gt; &amp; &quot;&apos;</Notes><!-- a comment --><?pi data?></Task></Tasks>',
                       "<!-- exported by something -->")  # fmt: skip
        self.run_import(data)
        (t,) = self.tasks()
        self.assertEqual((t["title"], t["description"]), ("<b>Fish & chips</b>", "café 😀 <ok> & \"'"))

    def test_the_namespace_does_not_matter_but_the_element_names_do(self):
        for data in (
            b'<Project><Tasks><Task><UID>1</UID><Name>No namespace</Name></Task></Tasks></Project>',
            b'<p:Project xmlns:p="http://schemas.microsoft.com/project"><p:Tasks><p:Task><p:UID>1</p:UID><p:Name>Prefixed</p:Name></p:Task></p:Tasks></p:Project>',
            b'<Project xmlns="http://example.com/some/other/version"><Tasks><Task><UID>1</UID><Name>Other namespace</Name></Task></Tasks></Project>',
            b'\xef\xbb\xbf<Project><Tasks><Task><UID>1</UID><Name>With a byte order mark</Name></Task></Tasks></Project>',
            '<?xml version="1.0" encoding="ISO-8859-1"?><Project><Tasks><Task><UID>1</UID><Name>Caf\xe9 Latin-1</Name></Task></Tasks></Project>'.encode("latin-1"),
        ):
            self.run_import(data, dry_run="true")
            self.assertEqual(self.run_import(data, dry_run="true")["created"], 1, data[:60])

    def test_the_content_type_has_to_say_xml(self):
        data = doc(task(1, "A"))
        for bad in ("application/json", "text/csv", "text/plain", "application/octet-stream", "multipart/form-data", ""):
            r = self.post_xml(data, content_type=bad)
            self.assertEqual((r.status, r.error_code), (415, "unsupported_media_type"), bad)
        for good in ("application/xml", "text/xml", "application/xml; charset=utf-8", "TEXT/XML;charset=UTF-8"):
            self.assertEqual(self.post_xml(data, content_type=good, dry_run="true").status, 200, good)
        self.assertEqual(self.tasks(), [])

    def test_a_file_over_the_import_limit_is_refused_before_it_is_read(self):
        small = App(replace(self.config, max_import_bytes=2000), clock=lambda: self.now, notifier=self.notifier)
        client = Client(small, self.tokens["alice"])
        ok = client.request("POST", "/api/projects/IMP/import/msproject", raw=doc(task(1, "A")), content_type="application/xml")
        self.assertEqual(ok.status, 200)
        r = client.request("POST", "/api/projects/IMP/import/msproject", raw=doc(*[task(i, "T%d" % i) for i in range(1, 100)]), content_type="application/xml")
        self.assertEqual((r.status, r.error_code), (413, "payload_too_large"))
        with small.session(user=self.user_row("alice")) as ctx:
            with self.assertRaises(PayloadTooLarge):  # the service enforces the limit as well (the command line does not go through the route)
                msproject.import_xml(ctx, "IMP", b"<Project>" + b" " * 3000 + b"</Project>")


class AccessTests(MspCase):
    DATA = doc(task(1, "A"))

    def user_row(self, username):
        with self.app.session() as ctx:
            return users.find_by_username(ctx.conn, username)

    def test_only_managers_and_admins_may_import(self):
        before = self.counts()
        for user, status in (("vera", 403), ("bob", 403), ("carol", 403)):
            r = self.post_xml(self.DATA, user=user)
            self.assertEqual((r.status, r.error_code), (status, "insufficient_role"), user)
        self.assertEqual(self.client(token="wrong").request("POST", "/api/projects/IMP/import/msproject", raw=self.DATA, content_type="application/xml").status, 401)
        self.assertEqual(self.client().request("POST", "/api/projects/IMP/import/msproject", raw=self.DATA, content_type="application/xml", token=None).status, 401)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.post_xml(self.DATA, user="alice").status, 200)
        self.assertEqual(self.post_xml(self.DATA, user="admin", dry_run="true").status, 200)

    def test_a_dry_run_is_no_way_to_probe_a_project_you_may_not_write_to(self):
        self.assertEqual(self.post_xml(self.DATA, user="vera", dry_run="true").status, 403)

    def test_an_archived_project_is_read_only(self):
        self.ok(self.alice.patch("/api/projects/IMP", {"status": "archived"}))
        before = self.counts()
        r = self.post_xml(self.DATA)
        self.assertEqual((r.status, r.error_code), (409, "project_archived"))
        self.assertEqual(self.post_xml(self.DATA, dry_run="true").status, 409)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.export().status, 200)  # but it can still be exported

    def test_an_unknown_project_is_a_404_and_the_key_is_not_case_sensitive(self):
        self.assertEqual(self.post_xml(self.DATA, key="NOPE").status, 404)
        self.assertEqual(self.post_xml(self.DATA, key="imp", dry_run="true").status, 200)

    def test_the_service_checks_project_membership_itself(self):
        """The command line calls the service directly, with the user it was told to act as."""
        self.make_project("PRIV", members=("bob",))
        with self.app.session(user=self.user_row("carol")) as ctx:
            with self.assertRaises(Forbidden) as caught:
                msproject.import_xml(ctx, "PRIV", self.DATA)
        self.assertEqual(caught.exception.code, "not_project_member")
        with self.app.session(user=self.user_row("bob")) as ctx:
            self.assertEqual(msproject.import_xml(ctx, "PRIV", self.DATA)["created"], 1)

    def test_the_audit_trail_names_the_importer_and_carries_no_file_content(self):
        self.post_xml(doc(task(1, "Confidential title", Notes="Confidential notes")), user="admin")
        entries = [a for a in self.ok(self.admin.get("/api/projects/IMP/activity"))["items"] if a["action"] == "import.completed"]
        self.assertEqual([e["actor"]["username"] for e in entries], ["admin"])
        self.assertNotIn("Confidential", str(entries))
