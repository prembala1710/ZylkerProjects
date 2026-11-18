"""Full-text search: the query language, ranking, filters, the index staying in step with the data, both engines."""

import contextlib
import io
import json
import os
import random
import sqlite3
import unittest
from unittest import mock

from helpers import ApiTestCase

from zylker_pm import cli, db
from zylker_pm.db import init_db
from zylker_pm.services import search
from zylker_pm.services.search import OFF, ON, parse


def plain(segs):
    return "".join(s["text"] for s in segs)


def matched(segs):
    return [s["text"] for s in segs if s["match"]]


class Corpus:
    """A small organisation with known words in known places."""

    def build(self):
        self.make_project("WEB", members=("bob", "carol"))
        self.make_project("OPS", members=("bob",))
        a = self.client("alice")
        self.ok(a.post("/api/projects/WEB/tasks", {
            "title": "Fix login crash on Safari", "description": "The login page crashes when the user clicks sign in",
            "labels": ["auth", "bug-fix"], "assignee": "bob", "type": "bug", "priority": "high",
        }), 201)  # fmt: skip
        self.ok(a.post("/api/projects/WEB/tasks", {"title": "Add logout button", "description": "Users want to log out everywhere", "assignee": "carol"}), 201)
        self.ok(a.post("/api/projects/WEB/tasks", {"title": "R\u00e9sum\u00e9 upload", "description": "Accept PDF r\u00e9sum\u00e9s"}), 201)
        self.ok(a.post("/api/projects/WEB/tasks", {"title": "Update docs", "description": "Document the AND OR NOT NEAR operators"}), 201)
        self.ok(a.post("/api/projects/OPS/tasks", {"title": "Rotate TLS certificates", "description": "Quarterly certificate rotation"}), 201)
        self.set_status("WEB-2", "in_progress")
        for status in ("in_progress", "in_review", "done"):
            self.set_status("OPS-1", status)
        self.comment = self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "Reproduced the crash on Safari 17"}), 201)

    def find(self, q, user="alice", **extra):
        return self.ok(self.client(user).get("/api/search", query={"q": q, **extra}))

    def hits(self, q, user="alice", **extra):
        return [(i["type"], i["key"]) for i in self.find(q, user, **extra)["items"]]

    def tasks(self, q, user="alice", **extra):
        return sorted(k for t, k in self.hits(q, user, **extra) if t == "task")

    def bad(self, q, **extra):
        r = self.client("alice").get("/api/search", query={"q": q, **extra})
        self.assertEqual(r.status, 422, (q, r.text))
        return r.json["error"]["details"]


class Behaviour(Corpus):
    """What every engine must do. Mixed into one TestCase per engine."""

    def setUp(self):
        super().setUp()
        self.build()

    # -- the query language
    def test_every_word_must_match(self):
        self.assertEqual(self.tasks("login safari"), ["WEB-1"])
        self.assertEqual(self.tasks("login logout"), [])

    def test_a_phrase_matches_in_order_only(self):
        self.assertEqual(self.tasks('"login page"'), ["WEB-1"])
        self.assertEqual(self.tasks('"page login"'), [])

    def test_prefix_search(self):
        self.assertEqual(self.tasks("log*"), ["WEB-1", "WEB-2"])

    def test_or_and_exclusion(self):
        self.assertEqual(self.tasks("logout OR certificate"), ["OPS-1", "WEB-2"])
        self.assertEqual(self.hits("safari -login"), [("comment", "WEB-1")])
        self.assertEqual(self.hits('safari -"login page"'), [("comment", "WEB-1")])

    def test_search_syntax_typed_by_a_user_is_only_text(self):
        self.assertIn("WEB-4", self.tasks("AND OR NOT NEAR operators"))
        for q in ["AND", "OR", "NOT", "NEAR(login safari)", "(", ")", "*", "a AND", '"', 'say "hi', "title:foo", "col:val", "'; DROP TABLE tasks; --",
                  "login\x02x", "\u2028 login", "login ^ safari", "{login}", "-", "status:", "a:b:c", "login*safari"]:  # fmt: skip
            r = self.client("alice").get("/api/search", query={"q": q})
            self.assertIn(r.status, (200, 422), (q, r.status, r.text))
        self.assertEqual(self.ok(self.client("alice").get("/api/tasks", query={"limit": 100}))["total"], 5)

    def test_hostile_random_queries_never_cause_a_server_error(self):
        rng = random.Random(5)
        atoms = ['"', "'", "(", ")", "*", "-", ":", "OR", "AND", "NOT", "NEAR", "^", "{", "}", "\\", "%", "_", "login", "WEB-1", "status:open", "status:", "in:task",
                 "project:WEB", "\u00e9", "\u65e5\u672c\u8a9e", "\x01", "\x02", "\x03", "\t", "&", "<b>", "--", ";", "a*b", '-"x y"', '"unterminated', "label:",
                 "assignee:me", "+", ".", ",", "[", "]", "|", "#", "@", "$", "=", "?"]  # fmt: skip
        for _ in range(300):
            q = " ".join("".join(rng.choice(atoms) for _ in range(rng.randint(1, 3))) for _ in range(rng.randint(1, 5)))[:200]
            r = self.client("alice").get("/api/search", query={"q": q})
            self.assertIn(r.status, (200, 422), (q, r.status, r.text))

    def test_input_that_has_nothing_to_search_for_is_refused(self):
        self.assertIn("q", self.bad(""))
        self.assertIn("q", self.bad("---"))
        self.assertIn("q", self.bad("-safari"))
        self.assertIn("q", self.bad("x" * 201))
        self.assertIn("q", self.bad(" ".join("w%d" % i for i in range(17))))

    # -- filters
    def test_filters_narrow_text_searches(self):
        self.assertEqual(self.tasks("crash project:WEB"), ["WEB-1"])
        self.assertEqual(self.tasks("crash project:OPS"), [])
        self.assertEqual(self.tasks("log* status:in_progress"), ["WEB-2"])
        self.assertEqual(self.tasks("log* assignee:bob"), ["WEB-1"])
        self.assertEqual(self.tasks("log* assignee:carol,bob"), ["WEB-1", "WEB-2"])
        self.assertEqual(self.tasks("log* priority:high"), ["WEB-1"])
        self.assertEqual(self.tasks("log* type:bug"), ["WEB-1"])
        self.assertEqual(self.tasks("log* label:auth"), ["WEB-1"])
        self.assertEqual(self.tasks("certificate is:closed"), ["OPS-1"])
        self.assertEqual(self.tasks("certificate is:open"), [])

    def test_a_comment_follows_the_filters_of_its_task(self):
        found = set(self.hits("safari status:todo"))
        self.assertEqual(found, {("task", "WEB-1"), ("comment", "WEB-1")})
        self.assertEqual(self.hits("safari assignee:carol"), [])
        self.assertEqual(self.hits("safari in:comment"), [("comment", "WEB-1")])
        self.assertEqual(self.hits("safari in:task"), [("task", "WEB-1")])
        self.assertEqual(self.hits("safari in:comments"), [("comment", "WEB-1")])

    def test_projects_are_searchable_and_filters_about_tasks_leave_them_out(self):
        self.assertEqual(self.hits("web", ), [("project", "WEB")])
        self.assertEqual(self.hits("web in:project"), [("project", "WEB")])
        self.assertEqual(self.hits("web in:task"), [])
        self.assertEqual(self.hits("web status:todo"), [])  # a status is about tasks, so the project drops out

    def test_filters_alone_list_tasks_most_recently_changed_first(self):
        self.assertEqual(self.tasks("status:done"), ["OPS-1"])
        self.assertEqual(self.tasks("is:open"), ["WEB-1", "WEB-2", "WEB-3", "WEB-4"])
        self.assertEqual(self.tasks("assignee:none"), ["OPS-1", "WEB-3", "WEB-4"])
        self.assertEqual(self.tasks("reporter:alice"), ["OPS-1", "WEB-1", "WEB-2", "WEB-3", "WEB-4"])
        self.assertEqual(self.tasks("project:ops"), ["OPS-1"])
        self.assertEqual(self.tasks("label:bug-fix"), ["WEB-1"])
        self.assertEqual(self.hits("assignee:me", user="bob"), [("task", "WEB-1")])
        self.assertEqual(self.hits("project:WEB in:project"), [("project", "WEB")])
        self.advance_days(1)
        self.client("alice").patch("/api/tasks/WEB-4", {"description": "touched"})
        self.assertEqual(self.hits("project:WEB is:open")[0], ("task", "WEB-4"))
        for item in self.find("status:done")["items"]:
            self.assertEqual(item["score"], None)

    def test_filters_alone_can_still_exclude_words(self):
        self.assertEqual(self.tasks("project:WEB -login"), ["WEB-2", "WEB-3", "WEB-4"])

    def test_bad_filters_are_reported(self):
        for q, word in (("status:bogus", "status:bogus"), ("in:nothing", "in:nothing"), ("priority:urgent", "priority:urgent"), ("type:epic2", "type:epic2"),
                        ("project:W", "project:W"), ("label:UP!PER", "label:"), ("assignee:bad$name", "assignee:bad"), ("is:maybe", "is:maybe"),
                        ("reporter:none", "reporter:none"), ("-status:open", "cannot be negated")):  # fmt: skip
            self.assertIn(word, self.bad("login " + q)["q"], q)
        self.assertEqual(self.bad("login status:bogus priority:urgent")["q"].count(";"), 1)  # every problem at once

    # -- task keys
    def test_a_task_key_goes_straight_to_the_task(self):
        for q in ("WEB-1", "web-1"):
            first = self.find(q)["items"][0]
            self.assertEqual((first["key"], first["exact_key"], first["matched_in"]), ("WEB-1", True, ["key"]))
        self.assertEqual(self.hits("WEB-99"), [])
        self.ok(self.client("alice").post("/api/projects/WEB/tasks", {"title": "See WEB-1 for details"}), 201)
        found = self.find("WEB-1")
        self.assertEqual([i["key"] for i in found["items"]], ["WEB-1", "WEB-5"])  # the pinned task once, then ordinary hits
        self.assertEqual(found["total"], 2)
        later = self.find("WEB-1", offset=1)
        self.assertEqual([i["key"] for i in later["items"]], ["WEB-5"])
        self.assertEqual([i["key"] for i in self.find("WEB-1", limit=1)["items"]], ["WEB-1"])  # the pin takes page one's only slot
        self.assertEqual([i["key"] for i in self.find("WEB-1", limit=1, offset=1)["items"]], ["WEB-5"])

    # -- ranking and presentation
    def test_a_title_match_outranks_a_text_match(self):
        a = self.client("alice")
        self.ok(a.post("/api/projects/WEB/tasks", {"title": "Other work", "description": "zebra zebra zebra zebra"}), 201)
        self.ok(a.post("/api/projects/WEB/tasks", {"title": "Zebra crossing"}), 201)
        self.assertEqual(self.tasks("zebra"), ["WEB-5", "WEB-6"])
        self.assertEqual([k for t, k in self.hits("zebra")], ["WEB-6", "WEB-5"])
        scores = [i["score"] for i in self.find("zebra")["items"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_results_carry_what_a_screen_needs(self):
        r = self.find("crash")
        self.assertEqual((r["total"], r["limit"], r["offset"], r["total_capped"]), (2, 20, 0, False))
        self.assertEqual(r["interpreted"], {"all_of": [["crash"]], "none_of": [], "filters": {}})
        task = next(i for i in r["items"] if i["type"] == "task")
        self.assertEqual((task["key"], task["title"], task["status"], task["priority"], task["assignee"], task["project"]), ("WEB-1", "Fix login crash on Safari", "todo", "high", "bob", "WEB"))
        self.assertEqual(plain(task["title_match"]), "Fix login crash on Safari")
        self.assertEqual([m.lower() for m in matched(task["title_match"])], ["crash"])
        self.assertIn("title", task["matched_in"])
        comment = next(i for i in r["items"] if i["type"] == "comment")
        self.assertEqual((comment["key"], comment["comment_id"], comment["author"], comment["title"]), ("WEB-1", self.comment["id"], "bob", "Fix login crash on Safari"))
        self.assertEqual([m.lower() for m in matched(comment["snippet"])], ["crash"])
        self.assertIn("Reproduced", plain(comment["snippet"]))

    def test_the_interpretation_echoes_the_query(self):
        r = self.find('login OR signin -safari "exact phrase" log* status:todo,blocked is:closed project:web')
        self.assertEqual(r["interpreted"], {
            "all_of": [["login", "signin"], ["exact phrase"], ["log*"]], "none_of": ["safari"],
            "filters": {"status": ["todo", "blocked", "done", "cancelled"], "project": ["WEB"]},
        })  # fmt: skip

    def test_highlights_are_data_never_markup(self):
        self.ok(self.client("alice").post("/api/projects/WEB/tasks", {"title": "<script>alert(1)</script> widget", "description": "<b>bold</b> widget & more"}), 201)
        item = self.find("widget")["items"][0]
        self.assertEqual(plain(item["title_match"]), "<script>alert(1)</script> widget")
        self.assertEqual(matched(item["title_match"]), ["widget"])
        self.assertIn("<b>bold</b>", plain(item["snippet"]))
        self.assertNotIn(ON, json.dumps(item))
        self.assertNotIn(OFF, json.dumps(item))

    def test_the_marker_characters_in_user_text_cannot_forge_a_highlight(self):
        r = self.client("alice").post("/api/projects/WEB/tasks", {"title": "plain title", "description": "before %sFORGED%s after widget" % (ON, OFF)})
        if r.status == 201:  # (the API may refuse control characters; if it does there is nothing to forge)
            item = self.find("widget")["items"][0]
            self.assertEqual(matched(item["snippet"]), ["widget"])

    def test_pagination_and_limits(self):
        for n in range(7):
            self.ok(self.client("alice").post("/api/projects/WEB/tasks", {"title": "pager item %d" % n}), 201)
        everything = [k for _, k in self.hits("pager", limit=50)]
        self.assertEqual(len(everything), 7)
        pages = [[k for _, k in self.hits("pager", limit=3, offset=o)] for o in (0, 3, 6)]
        self.assertEqual([len(p) for p in pages], [3, 3, 1])
        self.assertEqual(sorted(sum(pages, [])), sorted(everything))
        self.assertEqual(self.find("pager", limit=3)["total"], 7)
        for extra in ({"limit": "0"}, {"limit": "51"}, {"limit": "x"}, {"offset": "-1"}, {"offset": "1001"}, {"offset": "x"}):
            r = self.client("alice").get("/api/search", query={"q": "pager", **extra})
            self.assertEqual(r.status, 422, extra)

    def test_the_total_is_capped(self):
        conn = db.connect(self.config.db_path)
        conn.execute("BEGIN")
        pid = conn.execute("SELECT id FROM projects WHERE key = 'WEB'").fetchone()[0]
        rid = conn.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()[0]
        conn.executemany(
            "INSERT INTO tasks (project_id, number, title, reporter_id, created_at, updated_at) VALUES (?,?,?,?, '2026-10-16T09:00:00Z', '2026-10-16T09:00:00Z')",
            [(pid, 1000 + n, "bulkword %d" % n, rid) for n in range(1100)],
        )
        conn.execute("COMMIT")
        conn.close()
        r = self.find("bulkword", limit=1)
        self.assertEqual((r["total"], r["total_capped"]), (search.COUNT_CAP, True))

    # -- the index follows the data
    def test_edits_are_searchable_at_once(self):
        a = self.client("alice")
        self.ok(a.patch("/api/tasks/WEB-2", {"title": "Add sign-out button"}))
        self.assertEqual(self.tasks("logout"), [])
        self.assertEqual(self.tasks("sign-out"), ["WEB-2"])
        self.ok(a.patch("/api/tasks/WEB-2", {"description": "Needs a quokka"}))
        self.assertEqual(self.tasks("quokka"), ["WEB-2"])
        self.assertEqual(self.tasks("everywhere"), [])

    def test_labels_are_searchable_and_follow_changes(self):
        a = self.client("alice")
        self.ok(a.patch("/api/tasks/WEB-3", {"labels": ["urgent", "customer-facing"]}))
        self.assertEqual(self.tasks("urgent"), ["WEB-3"])
        self.assertEqual(self.find("urgent")["items"][0]["matched_in"], ["labels"])
        self.assertEqual(self.tasks("label:customer-facing"), ["WEB-3"])
        self.ok(a.patch("/api/tasks/WEB-3", {"labels": ["customer-facing"]}))
        self.assertEqual(self.tasks("urgent"), [])
        self.assertEqual(self.tasks("customer-facing"), ["WEB-3"])
        self.ok(a.patch("/api/tasks/WEB-3", {"labels": []}))
        self.assertEqual(self.tasks("customer"), [])

    def test_comments_follow_edits_and_deletes(self):
        cid = self.comment["id"]
        self.ok(self.client("bob").patch("/api/comments/%d" % cid, {"body": "Actually it was Firefox"}))
        self.assertEqual(self.hits("safari in:comment"), [])
        self.assertEqual(self.hits("firefox"), [("comment", "WEB-1")])
        self.assertEqual(self.client("bob").delete("/api/comments/%d" % cid).status, 204)
        self.assertEqual(self.hits("firefox"), [])

    def test_deleting_a_task_removes_it_and_its_comments(self):
        self.assertEqual(self.client("alice").delete("/api/tasks/WEB-1").status, 204)
        self.assertEqual(self.hits("safari"), [])
        self.assertEqual(self.hits("login"), [])
        self.assertEqual(self.hits("crash"), [])

    def test_deleting_a_project_removes_everything_in_it(self):
        self.assertEqual(self.client("admin").delete("/api/projects/WEB", query={"confirm": "WEB"}).status, 204)
        for q in ("login", "safari", "web", "logout", "docs"):
            self.assertEqual(self.hits(q), [], q)
        self.assertEqual(self.tasks("certificate"), ["OPS-1"])  # other projects untouched

    def test_project_edits_are_searchable(self):
        self.ok(self.client("alice").patch("/api/projects/OPS", {"name": "Platform", "description": "Keeps the lights on"}))
        self.assertEqual(self.hits("platform"), [("project", "OPS")])
        self.assertEqual(self.hits("lights"), [("project", "OPS")])
        self.assertEqual(self.hits("ops"), [("project", "OPS")])

    def test_csv_imports_are_searchable(self):
        raw = b"title,description\nImported zebra,from a csv file\n"
        self.ok(self.client("alice").request("POST", "/api/projects/WEB/import", raw=raw, content_type="text/csv"))
        self.assertEqual(self.tasks("zebra"), ["WEB-5"])

    def test_a_rolled_back_request_leaves_no_trace_in_search(self):
        with self.assertRaises(RuntimeError), self.app.session() as ctx:
            pid = ctx.conn.execute("SELECT id FROM projects WHERE key = 'WEB'").fetchone()[0]
            ctx.conn.execute(
                "INSERT INTO tasks (project_id, number, title, reporter_id, created_at, updated_at) VALUES (?, 99, 'phantom task', 1, 'x', 'x')", (pid,)
            )
            raise RuntimeError("boom")
        self.assertEqual(self.hits("phantom"), [])

    # -- administration
    def test_status_and_reindex_are_for_admins(self):
        for user in ("alice", "bob", "vera"):
            self.assertEqual(self.client(user).get("/api/search/status").status, 403, user)
            self.assertEqual(self.client(user).post("/api/search/reindex").status, 403, user)
        self.assertEqual(self.client(token="zpm_nope").get("/api/search").status, 401)

    def test_everyone_signed_in_can_search(self):
        for user in ("admin", "alice", "bob", "carol", "vera"):
            self.assertEqual(self.tasks("crash", user=user), ["WEB-1"], user)


class FtsEngineTests(Behaviour, ApiTestCase):
    def test_this_is_the_fts5_engine(self):
        self.assertEqual(self.find("crash")["engine"], "fts5")
        status = self.ok(self.client("admin").get("/api/search/status"))
        self.assertEqual((status["engine"], status["in_sync"], status["fts5_available"]), ("fts5", True, True))
        self.assertEqual(status["documents"], {"task": 5, "comment": 1, "project": 2})
        self.assertEqual(status["indexed"], {"task": 5, "comment": 1, "project": 2})

    def test_words_are_stemmed_and_accents_ignored(self):
        self.assertEqual(self.tasks("crashing"), ["WEB-1"])
        self.assertEqual(self.tasks("crashes"), ["WEB-1"])
        self.assertEqual(self.tasks("certificates"), ["OPS-1"])
        self.assertEqual(self.tasks("resume"), ["WEB-3"])
        self.assertEqual(self.tasks("R\u00c9SUM\u00c9"), ["WEB-3"])

    def test_the_index_is_kept_by_triggers_so_even_raw_sql_is_covered(self):
        conn = db.connect(self.config.db_path)
        pid = conn.execute("SELECT id FROM projects WHERE key = 'WEB'").fetchone()[0]
        conn.execute(
            "INSERT INTO tasks (project_id, number, title, description, reporter_id, created_at, updated_at) VALUES (?, 77, 'Raw SQL walrus', 'inserted behind the API', 1, 'x', 'x')",
            (pid,),
        )
        conn.close()
        self.assertEqual(self.tasks("walrus"), ["WEB-77"])
        conn = db.connect(self.config.db_path)
        conn.execute("UPDATE tasks SET title = 'Raw SQL narwhal' WHERE number = 77")
        conn.execute("INSERT INTO task_labels (task_id, label) SELECT id, 'marine' FROM tasks WHERE number = 77")
        conn.close()
        self.assertEqual((self.tasks("walrus"), self.tasks("narwhal"), self.tasks("marine")), ([], ["WEB-77"], ["WEB-77"]))

    def test_reindex_repairs_a_damaged_index(self):
        conn = db.connect(self.config.db_path)
        conn.execute("DELETE FROM search_index WHERE kind = 'task' AND ref IN (1, 2)")
        conn.execute("INSERT INTO search_index (rowid, kind, ref, project_id, task_id, title, labels, body) VALUES (4000, 'task', 1000, 1, 1000, 'ghost', '', '')")
        conn.close()
        self.assertEqual(self.tasks("login"), [])
        admin = self.client("admin")
        status = self.ok(admin.get("/api/search/status"))
        self.assertEqual((status["in_sync"], status["problems"]["task"]), (False, {"missing": 2, "orphaned": 1}))
        done = self.ok(admin.post("/api/search/reindex"))
        self.assertEqual(done, {"engine": "fts5", "indexed": {"task": 5, "comment": 1, "project": 2}})
        self.assertTrue(self.ok(admin.get("/api/search/status"))["in_sync"])
        self.assertEqual(self.tasks("login"), ["WEB-1"])
        self.assertEqual(self.tasks("ghost"), [])
        event = self.ok(admin.get("/api/audit", query={"action": "system.search_reindexed"}))["items"][0]
        self.assertEqual((event["actor"]["username"], event["details"]["rows"]), ("admin", 8))

    def test_the_index_matches_the_data_after_a_long_random_session(self):
        """Hundreds of mixed operations, then: the index is exactly in sync, and every probe word finds the same
        things through FTS5 as a plain scan of the tables does."""
        rng = random.Random(20261016)
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
        text = lambda n: " ".join(rng.choice(words) for _ in range(n))  # noqa: E731
        a, tasks, comments = self.client("alice"), [], []
        for _ in range(180):
            op = rng.choice(["create"] * 4 + ["title", "desc", "labels", "labels", "comment", "comment", "edit", "uncomment", "delete", "project"])
            if op == "create" or not tasks:
                key = rng.choice(["WEB", "OPS"])
                r = a.post("/api/projects/%s/tasks" % key, {"title": text(2), "description": text(4), "labels": rng.sample(words, rng.randint(0, 2))})
                tasks.append(self.ok(r, 201)["key"])
            elif op in ("title", "desc"):
                self.ok(a.patch("/api/tasks/" + rng.choice(tasks), {"title": text(2)} if op == "title" else {"description": text(5)}))
            elif op == "labels":
                self.ok(a.patch("/api/tasks/" + rng.choice(tasks), {"labels": rng.sample(words, rng.randint(0, 3))}))
            elif op == "comment":
                key = rng.choice(tasks)
                comments.append((key, self.ok(a.post("/api/tasks/%s/comments" % key, {"body": text(3)}), 201)["id"]))
            elif op == "edit" and comments:
                self.ok(a.patch("/api/comments/%d" % rng.choice(comments)[1], {"body": text(3)}))
            elif op == "uncomment" and comments:
                key, cid = comments.pop(rng.randrange(len(comments)))
                a.delete("/api/comments/%d" % cid)
            elif op == "delete" and len(tasks) > 3:
                key = tasks.pop(rng.randrange(len(tasks)))
                self.assertEqual(a.delete("/api/tasks/" + key).status, 204)
                comments = [c for c in comments if c[0] != key]
            elif op == "project":
                self.ok(a.patch("/api/projects/" + rng.choice(["WEB", "OPS"]), {"description": text(3)}))
        self.assertTrue(self.ok(self.client("admin").get("/api/search/status"))["in_sync"])
        def everything(word):
            found, offset = set(), 0
            while True:
                page = self.find(word, limit=50, offset=offset)["items"]
                found |= {(i["type"], i["key"], i.get("comment_id")) for i in page}
                if len(page) < 50:
                    return found
                offset += 50

        for word in words:
            via_fts = everything(word)
            with mock.patch.object(db, "search_engine", return_value="like"):
                via_scan = everything(word)
            self.assertTrue(via_fts, word)
            self.assertEqual(via_fts, via_scan, word)

    def test_parent_and_subtask_deletion_cleans_every_row(self):
        a = self.client("alice")
        parent = self.ok(a.post("/api/projects/WEB/tasks", {"title": "Parent platypus"}), 201)["key"]
        child = self.ok(a.post("/api/projects/WEB/tasks", {"title": "Child platypus", "parent": parent}), 201)["key"]
        a.post("/api/tasks/%s/comments" % child, {"body": "platypus comment"})
        self.assertEqual(len(self.hits("platypus")), 3)
        self.assertEqual(a.delete("/api/tasks/%s" % parent, query={"cascade": "true"}).status, 204)
        self.assertEqual(self.hits("platypus"), [])
        self.assertTrue(self.ok(self.client("admin").get("/api/search/status"))["in_sync"])


class LikeEngineTests(Behaviour, ApiTestCase):
    """The same behaviour on a SQLite without FTS5."""

    def setUp(self):
        patcher = mock.patch.object(db, "fts5_available", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_this_is_the_substring_engine(self):
        self.assertEqual(self.find("crash")["engine"], "like")
        status = self.ok(self.client("admin").get("/api/search/status"))
        self.assertEqual((status["engine"], status["in_sync"], status["fts5_available"]), ("like", True, False))
        self.assertNotIn("indexed", status)

    def test_matching_is_by_substring_and_ignores_case(self):
        self.assertEqual(self.tasks("OGIN"), ["WEB-1"])
        self.assertEqual(self.tasks("r\u00e9sum\u00e9"), ["WEB-3"])

    def test_percent_and_underscore_are_ordinary_characters(self):
        self.ok(self.client("alice").post("/api/projects/WEB/tasks", {"title": "100% done_ish"}), 201)
        self.assertEqual(self.tasks("100%"), ["WEB-5"])
        self.assertEqual(self.tasks("done_ish"), ["WEB-5"])
        self.assertEqual(self.tasks("one_ish"), ["WEB-5"])
        self.assertEqual(self.tasks("_"), ["WEB-5"])  # an underscore is a character, not a wildcard
        self.assertIn("q", self.bad("%"))  # a lone percent sign is not a word

    def test_there_is_no_index_to_rebuild(self):
        r = self.client("admin").post("/api/search/reindex")
        self.assertEqual((r.status, r.error_code), (409, "no_search_index"))

    def test_the_command_can_move_the_database_onto_fts5_when_it_becomes_possible(self):
        self.assertEqual(self.find("crash")["engine"], "like")
        with mock.patch.object(db, "fts5_available", return_value=True):
            result = search.rebuild_database(self.config.db_path)
            self.assertEqual(result, {"engine": "fts5", "switched": True, "indexed": {"task": 5, "comment": 1, "project": 2}})
            self.assertEqual(self.find("crash")["engine"], "fts5")
            self.assertEqual(self.tasks("crashing"), ["WEB-1"])
            self.assertEqual(search.rebuild_database(self.config.db_path)["switched"], False)  # second time: a plain rebuild
            self.assertTrue(self.ok(self.client("admin").get("/api/search/status"))["in_sync"])
            self.ok(self.client("alice").patch("/api/tasks/WEB-3", {"title": "Switched narwhal"}))
            self.assertEqual(self.tasks("narwhal"), ["WEB-3"])  # the triggers are live

    def test_the_command_says_so_when_there_is_nothing_to_do(self):
        result = search.rebuild_database(self.config.db_path)
        self.assertEqual((result["engine"], result["switched"], result["indexed"]), ("like", False, None))


class ParserTests(unittest.TestCase):
    def test_words_phrases_prefixes_or_and_exclusions(self):
        q = parse('login -safari "exact phrase" log* a OR b -"bad phrase"')
        self.assertEqual([[t.shown() for t in g] for g in q.groups], [["login"], ["exact phrase"], ["log*"], ["a", "b"]])
        self.assertEqual([t.shown() for t in q.excluded], ["safari", "bad phrase"])
        self.assertEqual(q.filters, {})

    def test_the_fts_expression_quotes_everything(self):
        q = parse('login -safari "exact phrase" log* a OR b')
        self.assertEqual(search.fts_expression(q), '("login" AND "exact phrase" AND "log"* AND ("a" OR "b")) NOT ("safari")')
        self.assertEqual(search.fts_expression(parse("AND NEAR(x y)")), '"AND" AND "NEAR(x" AND "y)"')
        self.assertEqual(search.fts_expression(parse('say "hi')), '"say" AND "hi"')

    def test_filters_are_recognised_by_name_only(self):
        q = parse("status:todo,done project:web x foo:bar Status:Blocked")
        self.assertEqual(q.filters, {"status": ["todo", "done", "Blocked"], "project": ["web"]})
        self.assertEqual([[t.shown() for t in g] for g in q.groups], [["x"], ["foo:bar"]])

    def test_a_dangling_or_is_ignored_and_or_binds_only_words(self):
        self.assertEqual([[t.shown() for t in g] for g in parse("OR login OR").groups], [["login"]])
        self.assertEqual([[t.shown() for t in g] for g in parse("a OR -b c").groups], [["a"], ["c"]])
        self.assertEqual([[t.shown() for t in g] for g in parse("a or b").groups], [["a"], ["or"], ["b"]])  # only upper-case OR is an operator

    def test_punctuation_alone_is_not_a_word(self):
        q = parse('--- "" *** ... ok')
        self.assertEqual([[t.shown() for t in g] for g in q.groups], [["ok"]])

    def test_segments_and_marking(self):
        self.assertEqual(search.segments("a %sb%s c" % (ON, OFF)), [{"text": "a ", "match": False}, {"text": "b", "match": True}, {"text": " c", "match": False}])
        self.assertEqual(search.segments(""), [{"text": "", "match": False}])
        self.assertEqual(search.segments("no marks"), [{"text": "no marks", "match": False}])
        self.assertEqual(search._mark("Fix LOGIN now", parse("login").terms), "Fix %sLOGIN%s now" % (ON, OFF))
        long = ("x" * 300) + " needle " + ("y" * 300)
        snippet = search._mark(long, parse("needle").terms, 100)
        self.assertTrue(snippet.startswith("\u2026") and snippet.endswith("\u2026") and "%sneedle%s" % (ON, OFF) in snippet and len(snippet) < 140)
        self.assertEqual(search._mark("a%sb" % ON, []), "ab")  # marker characters in the text itself are dropped


class UpgradeTests(ApiTestCase):
    def v5_database(self, path):
        conn = db.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        for version in (1, 2, 3, 4):
            db._migrate(conn, version)
        conn.close()

    def names(self, path):
        conn = sqlite3.connect(path)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def test_an_older_database_upgrades_and_its_existing_data_becomes_searchable(self):
        old = os.path.join(self.tmp, "v5.db")
        self.v5_database(old)
        conn = db.connect(old)
        conn.execute("INSERT INTO users (username, display_name, role, token_hash, created_at) VALUES ('u', 'U', 'admin', 'h', 'x')")
        conn.execute("INSERT INTO projects (key, name, description, status, created_at, updated_at) VALUES ('OLD', 'Legacy site', 'before search existed', 'active', 'x', 'x')")
        conn.execute("INSERT INTO tasks (project_id, number, title, description, reporter_id, created_at, updated_at) VALUES (1, 1, 'Ancient walrus', 'from v5', 1, 'x', 'x')")
        conn.execute("INSERT INTO task_labels VALUES (1, 'heritage')")
        conn.execute("INSERT INTO comments (task_id, author_id, body, created_at) VALUES (1, 1, 'an old comment: the walrus again', 'x')")
        conn.close()
        self.assertNotIn("search_meta", self.names(old)[0])
        init_db(old)
        fresh = os.path.join(self.tmp, "fresh6.db")
        init_db(fresh)
        self.assertEqual(self.names(old), self.names(fresh))
        self.assertEqual(self.names(old)[1], db.SCHEMA_VERSION)
        conn = db.connect(old)
        rows = {tuple(r) for r in conn.execute("SELECT kind, ref FROM search_index")}
        self.assertEqual(rows, {("task", 1), ("comment", 1), ("project", 1)})
        self.assertEqual([r[0] for r in conn.execute("SELECT kind FROM search_index WHERE search_index MATCH 'walrus' ORDER BY kind")], ["comment", "task"])
        self.assertEqual([r[0] for r in conn.execute("SELECT kind FROM search_index WHERE search_index MATCH 'heritage'")], ["task"])
        conn.close()

    def test_a_failing_upgrade_changes_nothing(self):
        path = os.path.join(self.tmp, "v5b.db")
        self.v5_database(path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE search_meta (surprise INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(sqlite3.OperationalError):
            init_db(path)
        names, version = self.names(path)
        self.assertEqual(version, 5)
        self.assertNotIn("search_index", names)
        self.assertFalse([n for n in names if n.startswith("search_") and n != "search_meta"])

    def test_a_database_created_without_fts5_uses_the_substring_engine(self):
        path = os.path.join(self.tmp, "nofts.db")
        with mock.patch.object(db, "fts5_available", return_value=False):
            init_db(path)
        names, _ = self.names(path)
        self.assertIn("search_meta", names)
        self.assertNotIn("search_index", names)
        conn = db.connect(path)
        self.assertEqual(db.search_engine(conn), "like")
        conn.close()

    def test_a_database_that_needs_fts5_is_refused_where_it_is_missing(self):
        with mock.patch.object(db, "fts5_available", return_value=False):
            with self.assertRaises(RuntimeError) as cm:
                init_db(self.config.db_path)  # the fixture database was created with FTS5
        self.assertIn("FTS5", str(cm.exception))

    def test_a_pre_v6_database_has_no_engine(self):
        path = os.path.join(self.tmp, "v5c.db")
        self.v5_database(path)
        conn = db.connect(path)
        self.assertIsNone(db.search_engine(conn))
        conn.close()


class CommandTests(ApiTestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("ZYLKER_")}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", self.config.db_path, *args])
        return code, out.getvalue(), err.getvalue()

    def test_search_rebuild_repairs_the_index(self):
        Corpus.build(self)
        conn = db.connect(self.config.db_path)
        conn.execute("DELETE FROM search_index")
        conn.close()
        self.assertEqual(self.client("alice").get("/api/search", query={"q": "login"}).json["total"], 0)
        code, out, _ = self.run_cli("search-rebuild")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"engine": "fts5", "switched": False, "indexed": {"task": 5, "comment": 1, "project": 2}})
        self.assertEqual(self.client("alice").get("/api/search", query={"q": "login"}).json["total"], 1)


if __name__ == "__main__":
    unittest.main()
