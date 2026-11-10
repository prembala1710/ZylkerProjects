"""Importing a Microsoft Project (MSPDI) file: every field, every dependency type, milestones, people, repeat imports."""

import os

from msproject_helpers import MspCase, assignment, doc, resource, task

from zylker_pm.services.msproject import project_guid

HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 4800  # one working day of lag, in tenths of a minute


class MappingTests(MspCase):
    def test_a_plan_is_mapped_field_by_field(self):
        data = doc(
            task(1, "Website", level=1, Notes="The whole site"),
            task(2, "Design", level=2, Priority=750, PercentComplete=100, Start="2026-11-02T08:00:00", Finish="2026-11-03T17:00:00",
                 Duration="PT16H0M0S", Work="PT16H0M0S", Deadline="2026-11-04T17:00:00", Notes="Use\r\nthe new logo"),
            task(3, "Build", level=2, Priority=250, PercentComplete=40, Start="2026-11-04T08:00:00", Finish="2026-11-10T17:00:00", Work="PT30H30M0S"),
            task(4, "Launch", level=1, Duration="PT4H0M0S"),
            resources=[resource(1, "Bob"), resource(2, "Carol", EmailAddress="carol@example.com")],
            assignments=[assignment(2, 1, "PT16H0M0S"), assignment(3, 2, "PT30H30M0S")],
        )
        report = self.run_import(data)
        self.assertEqual((report["tasks_read"], report["created"], report["errors"], report["warnings"]), (4, 4, [], []))
        self.assertEqual(report["keys"], ["IMP-1", "IMP-2", "IMP-3", "IMP-4"])
        t = self.by_title()
        self.assertEqual((t["Website"]["type"], t["Website"]["parent"], t["Website"]["description"]), ("epic", None, "The whole site"))
        design = t["Design"]
        self.assertEqual((design["parent"], design["type"], design["status"], design["priority"]), ("IMP-1", "task", "done", "high"))
        self.assertEqual((design["start_date"], design["due_date"], design["estimate_hours"]), ("2026-11-02", "2026-11-04", 16.0))
        self.assertEqual((design["assignee"]["username"], design["description"]), ("bob", "Use\nthe new logo"))
        self.assertIsNotNone(design["completed_at"])
        build = t["Build"]
        self.assertEqual((build["status"], build["priority"], build["estimate_hours"], build["assignee"]["username"]), ("in_progress", "low", 30.5, "carol"))
        self.assertEqual((build["start_date"], build["due_date"]), ("2026-11-04", "2026-11-10"))  # no constraint or deadline: Project's own dates
        self.assertEqual((t["Launch"]["estimate_hours"], t["Launch"]["status"], t["Launch"]["parent"]), (4.0, "todo", None))
        self.assertEqual(t["Design"]["reporter"]["username"], "alice")  # the person who imported
        self.assertEqual(t["Design"]["external_ref"][:4], "msp:")

    def test_outline_levels_become_parents_and_the_type_follows_the_depth(self):
        self.run_import(doc(task(1, "A", 1), task(2, "A1", 2), task(3, "A1a", 3), task(4, "A1b", 3), task(5, "A2", 2), task(6, "B", 1), task(7, "B1", 2)))
        t = self.by_title()
        self.assertEqual({k: v["parent"] for k, v in t.items()}, {"A": None, "A1": "IMP-1", "A1a": "IMP-2", "A1b": "IMP-2", "A2": "IMP-1", "B": None, "B1": "IMP-6"})
        self.assertEqual({k: v["type"] for k, v in t.items()}, {"A": "epic", "A1": "story", "A1a": "task", "A1b": "task", "A2": "task", "B": "epic", "B1": "task"})

    def test_nesting_deeper_than_five_levels_is_moved_up_with_a_warning(self):
        report = self.run_import(doc(*[task(i, "L%d" % i, i) for i in range(1, 8)]))
        t = self.by_title()
        self.assertEqual([t["L%d" % i]["parent"] for i in range(1, 8)], [None, "IMP-1", "IMP-2", "IMP-3", "IMP-4", "IMP-4", "IMP-4"])
        self.assertEqual(len([w for w in report["warnings"] if "nested more than 5 levels" in w["message"]]), 2)
        self.assertEqual(report["errors"], [])

    def test_a_missing_or_odd_outline_level_means_top_level_or_one_below(self):
        self.run_import(doc(task(1, "Top", 1), task(2, "Skips a level", 3), task(3, "Level zero", 0)))
        t = self.by_title()
        self.assertEqual((t["Skips a level"]["parent"], t["Level zero"]["parent"]), ("IMP-1", None))

    def test_status_comes_from_completion_and_the_active_flag(self):
        cases = {"zero": (0, 1), "half": (50, 1), "almost": (99, 1), "full": (100, 1), "over": (120, 1), "inactive": (0, 0), "inactive-but-done": (100, 0)}
        self.run_import(doc(*[task(i, name, PercentComplete=pc, Active=active) for i, (name, (pc, active)) in enumerate(cases.items(), 1)]))
        t = self.by_title()
        self.assertEqual({k: v["status"] for k, v in t.items()}, {"zero": "todo", "half": "in_progress", "almost": "in_progress", "full": "done", "over": "done", "inactive": "cancelled", "inactive-but-done": "cancelled"})
        self.assertIsNotNone(t["inactive"]["completed_at"])

    def test_priority_bands_follow_projects_own_names(self):
        values = {"lowest": 100, "low": 400, "medium": 500, "still medium": 599, "high": 600, "higher": 700, "very high": 800, "highest": 900, "do not level": 1000}
        self.run_import(doc(*[task(i, name, Priority=value) for i, (name, value) in enumerate(values.items(), 1)], task(20, "absent"), task(21, "junk", Priority="x")))
        t = self.by_title()
        self.assertEqual([t[n]["priority"] for n in values], ["low", "low", "medium", "medium", "high", "high", "high", "critical", "critical"])
        self.assertEqual((t["absent"]["priority"], t["junk"]["priority"]), ("medium", "medium"))

    def test_the_estimate_is_the_work_else_the_duration_and_projects_placeholder_is_not_an_estimate(self):
        self.run_import(doc(
            task(1, "work wins", Work="PT10H0M0S", Duration="PT40H0M0S"),
            task(2, "duration only", Duration="PT24H0M0S"),
            task(3, "no work yet", Work="PT0H0M0S", Duration="PT16H0M0S"),
            task(4, "one day question mark", Work="PT0H0M0S", Duration="PT8H0M0S", Estimated=1),
            task(5, "two days question mark", Duration="PT16H0M0S", Estimated=1),
            task(6, "one day for sure", Duration="PT8H0M0S", Estimated=0),
            task(7, "milestone-like zero", Duration="PT0H0M0S"),
            task(8, "nothing"),
            task(9, "garbage", Work="soon", Duration="a while"),
            task(10, "huge", Work="PT20000H0M0S"),
            task(11, "seconds", Work="PT0H0M36S"),
            task(12, "days form", Duration="P1DT2H"),
        ))
        t = self.by_title()
        self.assertEqual({k: v["estimate_hours"] for k, v in t.items()}, {
            "work wins": 10.0, "duration only": 24.0, "no work yet": 16.0, "one day question mark": None, "two days question mark": 16.0,
            "one day for sure": 8.0, "milestone-like zero": None, "nothing": None, "garbage": None, "huge": 10000.0, "seconds": 0.01, "days form": 26.0})  # fmt: skip

    def test_dates_start_no_earlier_than_and_deadline_beat_the_computed_ones(self):
        report = self.run_import(doc(
            task(1, "constrained", Start="2026-11-05T08:00:00", Finish="2026-11-06T17:00:00", ConstraintType=4, ConstraintDate="2026-11-02T08:00:00", Deadline="2026-11-30T17:00:00"),
            task(2, "other constraint", Start="2026-11-05T08:00:00", Finish="2026-11-06T17:00:00", ConstraintType=2, ConstraintDate="2026-12-01T08:00:00"),
            task(3, "backwards", Start="2026-11-10T08:00:00", Finish="2026-11-06T17:00:00"),
            task(4, "garbage dates", Start="soon", Finish="2026-13-45T00:00:00", Deadline="NA"),
            task(5, "with zone", Start="2026-11-05T23:30:00+05:30", Finish="2026-11-06T01:00:00Z"),
        ))
        t = self.by_title()
        self.assertEqual((t["constrained"]["start_date"], t["constrained"]["due_date"]), ("2026-11-02", "2026-11-30"))
        self.assertEqual((t["other constraint"]["start_date"], t["other constraint"]["due_date"]), ("2026-11-05", "2026-11-06"))
        self.assertEqual((t["backwards"]["start_date"], t["backwards"]["due_date"]), ("2026-11-10", "2026-11-10"))
        self.assertEqual((t["garbage dates"]["start_date"], t["garbage dates"]["due_date"]), (None, None))
        self.assertEqual((t["with zone"]["start_date"], t["with zone"]["due_date"]), ("2026-11-05", "2026-11-06"))
        self.assertEqual([w["uid"] for w in report["warnings"] if "finish is before the start" in w["message"]], [3])

    def test_titles_notes_and_names_are_cleaned_and_limited(self):
        report = self.run_import(doc(
            task(1, "  Padded\ttitle\nwith  breaks  "), task(2, "x" * 250), task(3, "long notes", Notes="n" * 20500), task(4, "   "), task(5, "<b>&amp; \"quoted\" 😀 déjà</b>")))
        t = self.by_title()
        self.assertTrue("Padded title with breaks" in t)  # tabs, line breaks and runs of spaces become single spaces
        self.assertEqual(len(t["x" * 200]["title"]), 200)
        self.assertEqual(len(t["long notes"]["description"]), 20000)
        self.assertTrue('<b>&amp; "quoted" 😀 déjà</b>' in t)  # stored as text, never interpreted
        self.assertEqual([e["uid"] for e in report["errors"]], [4])
        self.assertEqual(sorted(w["message"] for w in report["warnings"] if "cut" in w["message"]), ["the name was cut to 200 characters", "the notes were cut to 20,000 characters"])

    def test_a_parent_follows_its_subtasks(self):
        self.run_import(doc(
            task(1, "all done", 1), task(2, "d1", 2, PercentComplete=100), task(3, "d2", 2, PercentComplete=100),
            task(4, "mixed", 1), task(5, "m1", 2, PercentComplete=100), task(6, "m2", 2),
            task(7, "untouched", 1), task(8, "u1", 2),
            task(9, "all cancelled", 1), task(10, "c1", 2, Active=0),
            task(11, "nested", 1), task(12, "n1", 2), task(13, "n1a", 3, PercentComplete=100), task(14, "n1b", 3, PercentComplete=100), task(15, "n2", 2, PercentComplete=100),
        ))
        t = self.by_title()
        self.assertEqual({k: t[k]["status"] for k in ("all done", "mixed", "untouched", "all cancelled", "nested", "n1")},
                         {"all done": "done", "mixed": "in_progress", "untouched": "todo", "all cancelled": "cancelled", "nested": "done", "n1": "done"})  # fmt: skip
        self.assertIsNotNone(t["all done"]["completed_at"])
        self.assertIsNone(t["mixed"]["completed_at"])


class PeopleTests(MspCase):
    def setUp(self):
        super().setUp()
        self.make_user("dana", "member", email="dana@example.com")
        self.make_user("erin", "member")

    def assigned(self):
        return {t["title"]: (t["assignee"] or {}).get("username") for t in self.tasks()}

    def test_people_are_found_by_e_mail_first_then_by_name_and_only_work_resources_count(self):
        report = self.run_import(doc(
            task(1, "by mail"), task(2, "by name"), task(3, "by username"), task(4, "material"), task(5, "stranger"), task(6, "blank row"),
            resources=[resource(1, "Somebody Else", EmailAddress="BOB@example.com"), resource(2, "Carol"), resource(3, "bob"), resource(4, "Carol", Type=0), resource(5, "Nobody At All"), resource(6, "Carol", IsNull=1)],
            assignments=[assignment(1, 1), assignment(2, 2), assignment(3, 3), assignment(4, 4), assignment(5, 5), assignment(6, 6), assignment(99, 2), assignment(1, 77)],
        ))
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.assigned(), {"by mail": None, "by name": "carol", "by username": "bob", "material": None, "stranger": None, "blank row": None})
        # Bob's address is not recorded on his user, so the e-mail does not match; the name 'Somebody Else' does not either
        self.assertEqual([w["message"] for w in report["warnings"]], ["Nobody At All: 1 task left unassigned because there is no active user with that name or e-mail address",
                                                                      "Somebody Else: 1 task left unassigned because there is no active user with that name or e-mail address"])  # fmt: skip

    def test_the_person_with_the_most_work_on_a_task_is_its_assignee(self):
        report = self.run_import(doc(task(1, "shared"), task(2, "tie"), resources=[resource(1, "Bob"), resource(2, "Carol")],
                                     assignments=[assignment(1, 1, "PT2H0M0S"), assignment(1, 2, "PT6H0M0S"), assignment(2, 2, "PT4H0M0S"), assignment(2, 1, "PT4H0M0S")]))
        self.assertEqual(self.assigned(), {"shared": "carol", "tie": "carol"})
        self.assertEqual(sorted(w["uid"] for w in report["warnings"]), [1, 2])
        self.assertIn("only Carol is kept", report["warnings"][0]["message"])

    def test_someone_who_is_not_a_member_is_reported_once_per_person_and_can_be_added(self):
        data = doc(task(1, "a"), task(2, "b"), task(3, "c"), resources=[resource(1, "Dana"), resource(2, "dana@example.com"), resource(3, "Erin")],
                   assignments=[assignment(1, 1), assignment(2, 1), assignment(3, 3)])
        report = self.run_import(data)
        self.assertEqual(self.assigned(), {"a": None, "b": None, "c": None})
        self.assertEqual(sorted(w["message"] for w in report["warnings"]), [
            "dana: 2 tasks left unassigned because they are not a member of the project (importing with the add-missing-members option adds them)",
            "erin: 1 task left unassigned because they are not a member of the project (importing with the add-missing-members option adds them)"])  # fmt: skip
        self.assertEqual(self.counts()[3], 2 + 1)  # members: bob, carol and the manager who created the project

    def test_add_missing_members_adds_them_and_a_dry_run_adds_nobody(self):
        data = doc(task(1, "a"), resources=[resource(1, "Dana", EmailAddress="dana@example.com")], assignments=[assignment(1, 1)])
        before = self.counts()
        report = self.run_import(data, dry_run="true", add_missing_members="true")
        self.assertEqual((report["dry_run"], report["created"], report["warnings"]), (True, 1, []))
        self.assertEqual(self.counts(), before)
        self.run_import(data, add_missing_members="true")
        self.assertEqual(self.assigned(), {"a": "dana"})
        self.assertEqual(self.counts()[3], before[3] + 1)

    def test_deactivated_users_are_not_assigned(self):
        erin = next(u for u in self.ok(self.admin.get("/api/users"))["items"] if u["username"] == "erin")
        self.ok(self.admin.patch("/api/users/%d" % erin["id"], {"active": False}))
        report = self.run_import(doc(task(1, "a"), resources=[resource(1, "Erin")], assignments=[assignment(1, 1)]), add_missing_members="true")
        self.assertEqual(self.assigned(), {"a": None})
        self.assertIn("no active user", report["warnings"][0]["message"])


class MilestoneTests(MspCase):
    def test_a_milestone_becomes_a_milestone_and_the_tasks_before_it_belong_to_it(self):
        report = self.run_import(doc(
            task(1, "Build", Finish="2026-11-10T17:00:00"), task(2, "Test", Finish="2026-11-12T17:00:00", links=[1]),
            task(3, "Beta", Milestone=1, Start="2026-11-13T17:00:00", Finish="2026-11-13T17:00:00", Notes="First usable version", links=[2, (1, 0)]),
            task(4, "Announce", links=[(3, 1, 2 * DAY)]),
        ))
        self.assertEqual((report["created"], report["milestones"], report["links"], report["errors"]), (3, 1, 3, []))
        (beta,) = self.milestones()
        self.assertEqual((beta["name"], beta["due_date"], beta["description"]), ("Beta", "2026-11-13", "First usable version"))
        t = self.by_title()
        self.assertEqual([t[n]["milestone"] and t[n]["milestone"]["name"] for n in ("Build", "Test", "Announce")], ["Beta", "Beta", None])
        self.assertNotIn("Beta", t)
        # the milestone is not a task: the dependency through it joins every task before it to the task after it, lags added
        self.assertEqual(self.blockers("IMP-3"), [("IMP-1", "finish_to_start", 2), ("IMP-2", "finish_to_start", 2)])

    def test_dependencies_run_through_milestones_and_chains_of_milestones(self):
        self.run_import(doc(
            task(1, "A", Duration="PT8H0M0S"), task(2, "M1", Milestone=1, Finish="2026-11-02T17:00:00", links=[(1, 1, DAY)]),
            task(3, "M2", Milestone=1, Finish="2026-11-03T17:00:00", links=[(2, 1, DAY)]), task(4, "B", links=[(3, 3, 3 * DAY)]), task(5, "C", links=[2]),
        ))
        self.assertEqual(self.blockers("IMP-2"), [("IMP-1", "start_to_start", 5)])  # A -> M1 (1d) -> M2 (1d) -> B (SS, 3d)
        self.assertEqual(self.blockers("IMP-3"), [("IMP-1", "finish_to_start", 1)])

    def test_the_milestone_name_and_date_decide_whether_it_exists_already(self):
        self.ok(self.alice.post("/api/projects/IMP/milestones", {"name": "Beta", "due_date": "2026-11-13"}), 201)
        data = doc(task(1, "Build"), task(2, "Beta", Milestone=1, Finish="2026-11-13T17:00:00", links=[1]), task(3, "Beta", Milestone=1, Finish="2026-11-20T17:00:00", links=[1]))
        report = self.run_import(data)
        self.assertEqual(report["milestones"], 1)  # the 13th existed; the 20th is new
        self.assertEqual([(m["name"], m["due_date"]) for m in self.milestones()], [("Beta", "2026-11-13"), ("Beta", "2026-11-20")])
        self.assertEqual(self.by_title()["Build"]["milestone"]["id"], self.milestones()[0]["id"])  # the earliest of the two
        self.assertTrue(any("several milestones" in w["message"] for w in report["warnings"]))
        self.assertEqual(self.run_import(data)["milestones"], 0)

    def test_a_milestone_needs_a_name_and_a_date_and_a_summary_is_never_one(self):
        report = self.run_import(doc(task(1, "No date", Milestone=1), task(2, "", Milestone=1, Finish="2026-11-02T00:00:00"), task(3, "Group", Milestone=1), task(4, "child", 2), task(5, "L" * 150, Milestone=1, Start="2026-11-05T08:00:00")))
        self.assertEqual(sorted(e["message"] for e in report["errors"]), ["the milestone has no date", "the milestone has no name"])
        self.assertEqual([m["name"] for m in self.milestones()], ["L" * 100])
        self.assertIn("Group", self.by_title())  # it has a subtask, so it is a task
        self.assertEqual((report["milestones"], report["created"]), (1, 2))

    def test_dependencies_that_start_at_a_milestone_with_nothing_before_it_are_reported(self):
        report = self.run_import(doc(task(1, "Kick-off", Milestone=1, Finish="2026-11-02T08:00:00"), task(2, "First", links=[1])))
        self.assertEqual(report["links"], 0)
        self.assertEqual([w["message"] for w in report["warnings"]], ["milestone Kick-off has tasks after it but none before it, so those dependencies were not imported"])


class DependencyTests(MspCase):
    def test_the_three_types_and_their_lags_are_kept(self):
        self.run_import(doc(task(1, "A"), task(2, "FS", links=[(1, 1, 2 * DAY)]), task(3, "SS", links=[(1, 3, -1 * DAY)]), task(4, "FF", links=[(1, 0, 0)]), task(5, "default type", links=[(1, "", 0)])))
        self.assertEqual(self.blockers("IMP-2"), [("IMP-1", "finish_to_start", 2)])
        self.assertEqual(self.blockers("IMP-3"), [("IMP-1", "start_to_start", -1)])
        self.assertEqual(self.blockers("IMP-4"), [("IMP-1", "finish_to_finish", 0)])
        self.assertEqual(self.blockers("IMP-5"), [("IMP-1", "finish_to_start", 0)])

    def test_lags_are_converted_to_whole_working_days(self):
        links = [(1, 1, 4 * 60 * 10, 5), (1, 1, 12 * 60 * 10, 5), (1, 1, 24 * 60 * 10, 8), (1, 1, 48 * 60 * 10, 8), (1, 1, 10 * 60 * 10, 7), (1, 1, 5 * DAY, 9), (1, 1, 100, 19), (1, 1, 4000 * DAY), (1, 1, -4000 * DAY)]
        report = self.run_import(doc(task(1, "A"), *[task(i + 2, "T%d" % i, links=[link]) for i, link in enumerate(links)]))
        lags = [self.blockers("IMP-%d" % (i + 2))[0][2] for i in range(len(links))]
        # 4 h and 12 h are half a working day and a day and a half, which round away from zero; an elapsed day is a calendar day
        self.assertEqual(lags, [1, 2, 1, 2, 1, 5, 0, 365, -365])
        self.assertEqual(sorted(w["message"] for w in report["warnings"]), ["a lag given as a percentage was ignored", "a lag of -4000 days was limited to 365", "a lag of 4000 days was limited to 365"])

    def test_what_the_app_cannot_hold_is_reported_and_the_rest_still_imports(self):
        data = doc(task(1, "A"), task(2, "B", links=[(1, 2)]), task(3, "C", links=[(77, 1)]), task(4, "D", links=[(4, 1)]), task(5, "E", links=[1]))
        report = self.run_import(data)
        self.assertEqual(report["links"], 1)
        self.assertEqual(sorted(w["message"] for w in report["warnings"]), [
            "a dependency on task UID 77 was not imported: there is no such task in the file",
            "a start-to-finish dependency was not imported (the app has no equivalent)",
            "a task cannot depend on itself; that dependency was not imported"])  # fmt: skip
        report = self.run_import(doc(task(1, "A"), task(2, "X", links=[(1, 1, 0, 7, 1)])), dry_run="true")
        self.assertEqual([w["message"] for w in report["warnings"]], ["a dependency on a task in another project file was not imported"])

    def test_a_loop_is_reported_and_only_the_link_that_closes_it_is_skipped(self):
        report = self.run_import(doc(task(1, "A", links=[3]), task(2, "B", links=[1]), task(3, "C", links=[2])))
        self.assertEqual(report["links"], 2)
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("circular wait", report["errors"][0]["message"])
        self.assertEqual(report["created"], 3)

    def test_a_dependency_between_a_parent_and_its_own_subtask_is_refused_like_in_the_app(self):
        report = self.run_import(doc(task(1, "Parent", 1), task(2, "Child", 2, links=[1]), task(3, "Other", 1, links=[1])))
        self.assertEqual(report["links"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(self.blockers("IMP-3"), [("IMP-1", "finish_to_start", 0)])

    def test_the_same_dependency_given_twice_is_made_once(self):
        report = self.run_import(doc(task(1, "A"), task(2, "B", links=[(1, 1), (1, 3), (1, 1)])))
        self.assertEqual((report["links"], report["errors"], report["warnings"]), (1, [], []))
        self.assertEqual(self.blockers("IMP-2"), [("IMP-1", "finish_to_start", 0)])

    def test_two_tasks_keep_one_dependency_and_the_other_way_round_is_reported_not_dropped_silently(self):
        report = self.run_import(doc(task(1, "A", links=[2]), task(2, "B", links=[(1, 3)])))
        self.assertEqual((report["links"], report["errors"]), (1, []))
        self.assertEqual([(w["uid"], w["message"]) for w in report["warnings"]],
                         [(2, "the dependency on UID 1 was not imported: that task already depends on this one, and two tasks keep a single dependency between them")])  # fmt: skip
        self.assertEqual(self.blockers("IMP-1"), [("IMP-2", "finish_to_start", 0)])
        self.assertEqual(self.blockers("IMP-2"), [])
        again = self.run_import(doc(task(1, "A", links=[2]), task(2, "B", links=[(1, 3)])))  # the file still has it, so it is still said
        self.assertEqual((again["created"], again["links"], again["errors"], len(again["warnings"])), (0, 0, [], 1))


class RepeatTests(MspCase):
    def plan(self):
        return doc(task(1, "Epic"), task(2, "Story", 2, links=[3]), task(3, "Other"), task(4, "Gate", Milestone=1, Finish="2026-11-09T17:00:00", links=[2]), UID="plan-1", Name="Plan")

    def test_importing_the_same_file_again_creates_nothing(self):
        first = self.run_import(self.plan())
        again = self.run_import(self.plan())
        self.assertEqual((first["created"], first["milestones"], first["links"]), (3, 1, 1))
        self.assertEqual((again["created"], again["skipped_duplicates"], again["milestones"], again["links"], again["errors"]), (0, 3, 0, 0, []))
        self.assertEqual(len(self.tasks()), 3)
        self.assertEqual(len(self.milestones()), 1)

    def test_a_task_added_to_the_file_later_is_the_only_one_imported_and_joins_the_existing_ones(self):
        self.run_import(self.plan())
        bigger = doc(task(1, "Epic"), task(2, "Story", 2, links=[3]), task(5, "Added later", 2, links=[3]), task(3, "Other"), task(4, "Gate", Milestone=1, Finish="2026-11-09T17:00:00", links=[2]), UID="plan-1", Name="Plan")
        report = self.run_import(bigger)
        self.assertEqual((report["created"], report["skipped_duplicates"], report["links"]), (1, 3, 1))
        self.assertEqual(self.by_title()["Added later"]["parent"], "IMP-1")
        self.assertEqual(self.blockers("IMP-4"), [("IMP-3", "finish_to_start", 0)])

    def test_two_files_with_different_ids_do_not_recognise_each_others_tasks(self):
        self.run_import(self.plan())
        report = self.run_import(self.plan().replace(b"plan-1", b"plan-2"))
        self.assertEqual((report["created"], report["skipped_duplicates"]), (3, 0))

    def test_without_a_project_id_the_name_is_what_identifies_the_file(self):
        data = doc(task(1, "A"), Name="Anonymous")
        self.run_import(data)
        self.assertEqual(self.run_import(data)["skipped_duplicates"], 1)
        self.assertEqual(self.run_import(doc(task(1, "A"), Name="Another"))["created"], 1)

    def test_a_dry_run_changes_nothing_and_matches_what_the_real_import_does(self):
        before = self.counts()
        dry = self.run_import(self.plan(), dry_run="true")
        self.assertEqual(self.counts(), before)
        real = self.run_import(self.plan())
        for field in ("tasks_read", "created", "skipped_duplicates", "milestones", "links", "errors", "warnings", "keys"):
            self.assertEqual(dry[field], real[field], field)
        self.assertEqual((dry["dry_run"], real["dry_run"]), (True, False))

    def test_only_a_real_import_is_audited_and_once(self):
        def imports():
            return [a for a in self.ok(self.alice.get("/api/projects/IMP/activity"))["items"] if a["action"] == "import.completed"]

        self.run_import(self.plan(), dry_run="true")
        self.assertEqual(imports(), [])
        self.run_import(self.plan())
        (entry,) = imports()
        self.assertEqual((entry["action"], entry["details"]), ("import.completed", {"source": "msproject", "created": 3, "skipped_duplicates": 0, "errors": 0}))
        self.assertEqual(entry["actor"]["username"], "alice")


class ProblemTests(MspCase):
    def test_a_bad_task_is_reported_and_the_others_are_imported(self):
        report = self.run_import(doc(task(1, "Fine"), task(2, None), task(3, "Also fine"), task("x", "No number"), task(1, "Same uid again"), task(0, "The project row"), task(5, "Blank", IsNull=1)))
        self.assertEqual(report["created"], 2)
        self.assertEqual(sorted(self.by_title()), ["Also fine", "Fine"])
        self.assertEqual(sorted(e["message"] for e in report["errors"]), [
            "a task without a usable UID was skipped (a UID is a whole number from 1 to 2147483647)",
            "task: title: the task has no name",
            "the UID is used more than once; the later task was skipped"])  # fmt: skip

    def test_the_subtasks_of_a_task_that_could_not_be_imported_are_skipped_with_a_reason(self):
        report = self.run_import(doc(task(1, None, 1), task(2, "Child", 2), task(3, "Grandchild", 3), task(4, "Sibling", 1, links=[2])))
        self.assertEqual(sorted(self.by_title()), ["Sibling"])
        self.assertEqual(len(report["errors"]), 3)
        self.assertEqual(report["links"], 0)  # the dependency on a task that was not imported is not attempted

    def test_a_closed_parent_that_already_exists_refuses_open_children_but_not_the_rest(self):
        parent = self.make_task("IMP", "Closed epic", as_user="alice", status="done")
        data = doc(task(1, "Closed epic", 1), task(2, "New child", 2), task(3, "Free", 1), UID=project_guid("IMP"))
        report = self.run_import(data)
        self.assertEqual(parent["key"], "IMP-1")
        self.assertEqual((report["created"], report["skipped_duplicates"]), (1, 1))
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("is closed", report["errors"][0]["message"])

    def test_a_file_without_tasks_says_so(self):
        report = self.run_import(doc())
        self.assertEqual((report["tasks_read"], report["created"], report["warnings"]), (0, 0, [{"uid": None, "message": "the file contains no tasks"}]))

    def test_more_than_five_thousand_tasks_are_refused_before_anything_is_written(self):
        before = self.counts()
        response = self.post_xml(doc(*[task(i, "T%d" % i) for i in range(1, 5002)]))
        self.assertEqual(response.status, 422)
        self.assertIn("5001 tasks", response.json["error"]["details"]["xml"])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.run_import(doc(*[task(i, "T%d" % i) for i in range(1, 5001)]), dry_run="true")["created"], 5000)

    def test_huge_uids_and_odd_numbers_cannot_break_the_database_layer(self):
        report = self.run_import(doc(task(2**40, "Big"), task(-5, "Negative"), task("99999999999999999999999", "Huge"), task(7, "Normal", links=[(2**40, 1)])))
        self.assertEqual(sorted(self.by_title()), ["Normal"])
        self.assertEqual(len(report["errors"]), 3)


class ForeignFileTests(MspCase):
    """tests/fixtures/mpxj_sample.xml was written by MPXJ, not by this application (see MakeMpxjSample.java next to it)."""

    def setUp(self):
        super().setUp()
        self.make_user("anna", "member", email="anna@example.com")
        with open(os.path.join(HERE, "fixtures", "mpxj_sample.xml"), "rb") as f:
            self.sample = f.read()

    def test_a_plan_saved_by_another_tool_comes_in_whole(self):
        report = self.run_import(self.sample, add_missing_members="true")
        self.assertEqual((report["tasks_read"], report["created"], report["milestones"], report["links"], report["errors"]), (9, 8, 1, 5, []))
        t = self.by_title()
        self.assertEqual({k: (v["type"], v["parent"], v["status"]) for k, v in t.items()}, {
            "Planning": ("epic", None, "in_progress"), "Survey the new floor": ("task", "IMP-1", "done"), "Approve the budget": ("task", "IMP-1", "in_progress"),
            "Move day": ("epic", None, "todo"), "Pack the offices": ("task", "IMP-4", "todo"), "Haul the furniture": ("task", "IMP-4", "todo"),
            "Unpack & settle in": ("task", "IMP-4", "todo"), "Housewarming": ("task", None, "todo")})  # fmt: skip
        survey, budget = t["Survey the new floor"], t["Approve the budget"]
        self.assertEqual((survey["priority"], budget["priority"]), ("high", "low"))
        self.assertEqual((survey["assignee"]["username"], survey["estimate_hours"], survey["start_date"], survey["due_date"]), ("anna", 16.0, "2026-11-02", "2026-11-03"))
        self.assertEqual(t["Pack the offices"]["due_date"], "2026-11-13")  # its deadline, not its finish on the 12th
        self.assertEqual(t["Haul the furniture"]["start_date"], "2026-11-13")
        self.assertEqual(t["Planning"]["description"], "Everything before the boxes come out.")
        self.assertEqual(self.blockers("IMP-3"), [("IMP-2", "finish_to_start", 1)])
        self.assertEqual(self.blockers("IMP-6"), [("IMP-5", "start_to_start", 2)])
        self.assertEqual(self.blockers("IMP-7"), [("IMP-6", "finish_to_finish", 0)])
        self.assertEqual(self.blockers("IMP-8"), [("IMP-7", "finish_to_start", 2)])  # through the milestone, plus its 2 days
        self.assertEqual([(m["name"], m["due_date"]) for m in self.milestones()], [("Moved in", "2026-11-17")])
        self.assertEqual(t["Unpack & settle in"]["milestone"]["name"], "Moved in")
        self.assertEqual([w["message"] for w in report["warnings"]], [
            "assigned to several people (Anna Keller, Ben Ortiz); only Ben Ortiz is kept",
            "Ben Ortiz: 1 task left unassigned because there is no active user with that name or e-mail address"])  # fmt: skip

    def test_the_same_foreign_file_twice_is_harmless(self):
        self.run_import(self.sample)
        again = self.run_import(self.sample)
        self.assertEqual((again["created"], again["skipped_duplicates"], again["milestones"], again["links"], again["errors"]), (0, 8, 0, 0, []))
