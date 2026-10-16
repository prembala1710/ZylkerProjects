"""Comments, @mentions, time tracking and milestones."""

from helpers import ApiTestCase


class CollabBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.make_project("WEB", members=("bob", "carol"))
        self.make_task("WEB", "Task one", assignee="bob", estimate_hours=10)
        self.notifier.messages.clear()


class CommentTests(CollabBase):
    def test_add_list_and_author_details(self):
        c = self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "  Looks good  "}), 201)
        self.assertEqual((c["body"], c["author"]["username"], c["task"]), ("Looks good", "bob", "WEB-1"))
        items = self.ok(self.client("vera").get("/api/tasks/WEB-1/comments"))["items"]  # viewers may read
        self.assertEqual([i["id"] for i in items], [c["id"]])

    def test_validation_and_permissions(self):
        self.assertEqual(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": ""}).status, 422)
        self.assertEqual(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "x" * 10001}).status, 422)
        self.assertEqual(self.client("vera").post("/api/tasks/WEB-1/comments", {"body": "hi"}).status, 403)
        self.assertEqual(self.client("alice").post("/api/tasks/WEB-9/comments", {"body": "hi"}).status, 404)

    def test_mentions_notify_each_valid_user_once_and_never_the_author(self):
        self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "cc @carol @carol @bob and @ghost, mail a@b.com"}), 201)
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("mentioned Carol", self.notifier.messages[0])
        acts = self.ok(self.client("bob").get("/api/tasks/WEB-1/activity"))["items"]
        self.assertEqual(acts[0]["details"]["mentions"], ["carol"])

    def test_only_the_author_edits_but_a_manager_can_delete(self):
        c = self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": "v1"}), 201)
        url = "/api/comments/%d" % c["id"]
        self.assertEqual(self.client("carol").patch(url, {"body": "hijack"}).status, 403)
        edited = self.ok(self.client("bob").patch(url, {"body": "v2"}))
        self.assertEqual(edited["body"], "v2")
        self.assertIsNotNone(edited["edited_at"])
        self.assertEqual(self.client("carol").delete(url).status, 403)
        self.assertEqual(self.client("alice").delete(url).status, 204)
        self.assertEqual(self.ok(self.client("bob").get("/api/tasks/WEB-1/comments"))["items"], [])
        self.assertEqual(self.client("bob").patch(url, {"body": "x"}).status, 404)

    def test_script_in_a_comment_is_stored_verbatim_for_the_ui_to_escape(self):
        body = "<img src=x onerror=alert(1)>"
        c = self.ok(self.client("bob").post("/api/tasks/WEB-1/comments", {"body": body}), 201)
        self.assertEqual(c["body"], body)


class TimeTests(CollabBase):
    def log(self, hours, user="bob", **extra):
        return self.client(user).post("/api/tasks/WEB-1/time", {"hours": hours, **extra})

    def test_logging_accumulates_on_the_task(self):
        e = self.ok(self.log(2.5, note="spike"), 201)
        self.assertEqual((e["hours"], e["work_date"], e["note"], e["user"]["username"]), (2.5, "2026-10-16", "spike", "bob"))
        self.ok(self.log(1.25, user="carol", work_date="2026-10-15"), 201)
        t = self.ok(self.client("bob").get("/api/tasks/WEB-1"))
        self.assertEqual((t["logged_hours"], t["estimate_hours"]), (3.75, 10))
        entries = self.ok(self.client("bob").get("/api/tasks/WEB-1/time"))["items"]
        self.assertEqual([x["work_date"] for x in entries], ["2026-10-16", "2026-10-15"])

    def test_validation(self):
        for bad in (0, -1, 24.5, "2", None, True):
            self.assertEqual(self.log(bad).status, 422, bad)
        self.assertEqual(self.log(1, work_date="2026-10-18").status, 422)  # future
        self.assertEqual(self.log(1, work_date="2026-10-17").status, 201)  # tomorrow tolerated for timezones
        self.assertEqual(self.log(1, work_date="yesterday").status, 422)
        self.assertEqual(self.log(1, user="vera").status, 403)

    def test_only_the_owner_or_a_manager_can_delete_an_entry(self):
        e = self.ok(self.log(2), 201)
        url = "/api/time/%d" % e["id"]
        self.assertEqual(self.client("carol").delete(url).status, 403)
        self.assertEqual(self.client("bob").delete(url).status, 204)
        e = self.ok(self.log(2), 201)
        self.assertEqual(self.client("alice").delete("/api/time/%d" % e["id"]).status, 204)
        self.assertEqual(self.ok(self.client("bob").get("/api/tasks/WEB-1"))["logged_hours"], 0)

    def test_timesheet_visibility_and_totals(self):
        self.ok(self.log(2, work_date="2026-10-14"), 201)
        self.ok(self.log(3, user="carol", work_date="2026-10-15"), 201)
        self.ok(self.log(1, user="carol", work_date="2026-10-15"), 201)
        ts = self.ok(self.client("alice").get("/api/reports/timesheet", query={"from": "2026-10-01", "to": "2026-10-16"}))
        self.assertEqual(ts["total_hours"], 6)
        self.assertEqual({u["username"]: u["hours"] for u in ts["by_user"]}, {"bob": 2, "carol": 4})
        self.assertEqual({d["date"]: d["hours"] for d in ts["by_day"]}, {"2026-10-14": 2, "2026-10-15": 4})
        self.assertEqual(ts["by_project"], [{"project": "WEB", "hours": 6}])
        mine = self.ok(self.client("bob").get("/api/reports/timesheet", query={"from": "2026-10-01", "to": "2026-10-16"}))
        self.assertEqual(mine["total_hours"], 2)  # members only ever see themselves
        r = self.client("bob").get("/api/reports/timesheet", query={"user": "carol"})
        self.assertEqual(r.status, 403)
        only = self.ok(self.client("alice").get("/api/reports/timesheet", query={"user": "carol", "from": "2026-10-01"}))
        self.assertEqual(only["total_hours"], 4)
        self.assertEqual(self.client("alice").get("/api/reports/timesheet", query={"from": "2026-10-20", "to": "2026-10-01"}).status, 422)


class MilestoneTests(CollabBase):
    def test_lifecycle_progress_and_status(self):
        alice = self.client("alice")
        ms = self.ok(alice.post("/api/projects/WEB/milestones", {"name": "Beta", "due_date": "2026-10-30"}), 201)
        self.assertEqual((ms["status"], ms["progress"], ms["tasks_total"]), ("open", 0.0, 0))
        self.ok(alice.patch("/api/tasks/WEB-1", {"milestone": ms["id"]}))
        self.make_task("WEB", "Two", milestone=ms["id"])
        self.make_task("WEB", "Cancelled one", milestone=ms["id"])
        self.set_status("WEB-3", "cancelled")  # cancelled work does not count against the milestone
        self.set_status("WEB-1", "done")
        [m] = self.ok(alice.get("/api/projects/WEB/milestones"))["items"]
        self.assertEqual((m["tasks_total"], m["tasks_done"], m["progress"], m["status"]), (2, 1, 50.0, "open"))
        self.set_status("WEB-2", "done")
        self.assertEqual(self.ok(alice.get("/api/projects/WEB/milestones"))["items"][0]["status"], "achieved")
        self.assertEqual(self.ok(alice.get("/api/tasks/WEB-1"))["milestone"]["name"], "Beta")

    def test_overdue_and_validation_and_permissions(self):
        alice = self.client("alice")
        ms = self.ok(alice.post("/api/projects/WEB/milestones", {"name": "Late", "due_date": "2026-10-01"}), 201)
        self.assertEqual(ms["status"], "overdue")
        self.assertEqual(alice.post("/api/projects/WEB/milestones", {"name": "x"}).status, 422)  # due_date required
        self.assertEqual(self.client("bob").post("/api/projects/WEB/milestones", {"name": "x", "due_date": "2026-12-01"}).status, 403)
        self.assertEqual(self.client("bob").get("/api/projects/WEB/milestones").status, 200)

    def test_milestone_must_belong_to_the_same_project_and_delete_unlinks(self):
        alice = self.client("alice")
        self.make_project("API")
        other = self.ok(alice.post("/api/projects/API/milestones", {"name": "Other", "due_date": "2026-12-01"}), 201)
        r = alice.patch("/api/tasks/WEB-1", {"milestone": other["id"]})
        self.assertEqual(r.status, 422)
        mine = self.ok(alice.post("/api/projects/WEB/milestones", {"name": "Mine", "due_date": "2026-12-01"}), 201)
        self.ok(alice.patch("/api/tasks/WEB-1", {"milestone": mine["id"]}))
        self.assertEqual(self.client("bob").get("/api/tasks", query={"milestone": str(mine["id"])}).json["total"], 1)
        self.ok(alice.patch("/api/milestones/%d" % mine["id"], {"name": "Renamed", "due_date": "2026-12-05"}))
        self.assertEqual(alice.delete("/api/milestones/%d" % mine["id"]).status, 204)
        self.assertIsNone(self.ok(alice.get("/api/tasks/WEB-1"))["milestone"])
        self.assertEqual(alice.delete("/api/milestones/%d" % mine["id"]).status, 404)
