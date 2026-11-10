"""Delivery projection, part 1: the scheduling engine and the working calendar (no database involved).

Hand-computed schedules pin down the rules (priority, dependencies, idle people, start days, part-time work, exactness).
The randomised cases compare the event-driven engine with an *independent* oracle: a naive simulation that steps through
time one eighth of a day at a time and re-derives everything at every tick. The two share the written rules but not a line
of code, so a mistake in the engine would have to be repeated in a different form to go unnoticed.
"""

import random
import unittest
from datetime import date, timedelta
from fractions import Fraction as F

from helpers import ApiTestCase  # noqa: F401  (puts the repo root on sys.path)

from zylker_pm.services.scheduler import (
    Job,
    date_of_time,
    first_workday,
    schedule,
    start_date_of_time,
    workday_date,
    workday_index,
)

EIGHT = F(8)


def job(jid, effort, person=None, blockers=(), earliest=0, order=None):
    return Job(jid, F(effort), person, frozenset(blockers), earliest, order if order is not None else (0, jid))


def run(jobs, rates=None, horizon=1000, unassigned=EIGHT):
    return schedule({j.id: j for j in jobs}, {k: F(v) for k, v in (rates or {}).items()}, unassigned, horizon)


def span(result, jid):
    p = result[jid]
    return (p.start, p.finish)


class HandComputedTests(unittest.TestCase):
    def test_one_person_works_through_their_jobs_in_order(self):
        r = run([job(1, 16, 1, order=(0,)), job(2, 8, 1, order=(1,)), job(3, 4, 1, order=(2,))], {1: 8})
        self.assertEqual([span(r, i) for i in (1, 2, 3)], [(0, 2), (2, 3), (3, F(7, 2))])
        self.assertEqual(r[1].why["kind"], "immediately")
        self.assertEqual((r[2].why["kind"], r[2].why["job"]), ("queue", 1))
        self.assertEqual((r[3].why["kind"], r[3].why["job"]), ("queue", 2))

    def test_the_order_decides_who_goes_first_and_ties_fall_back_to_the_id(self):
        r = run([job(1, 8, 1, order=(5,)), job(2, 8, 1, order=(1,)), job(3, 8, 1, order=(1,))], {1: 8})
        self.assertEqual([span(r, i) for i in (2, 3, 1)], [(0, 1), (1, 2), (2, 3)])

    def test_a_job_waits_for_its_blocker_and_another_person_may_take_it(self):
        r = run([job(1, 8, 1), job(2, 8, 2, blockers=[1]), job(3, 8, 2, order=(1, 3))], {1: 8, 2: 8})
        self.assertEqual(span(r, 1), (0, 1))
        self.assertEqual(span(r, 3), (0, 1))  # carol's other job fills the time while job 2 waits
        self.assertEqual(span(r, 2), (1, 2))
        self.assertEqual((r[2].why["kind"], r[2].why["job"]), ("blocker", 1))  # blocker and queue tie at 1: the dependency is named

    def test_nobody_sits_idle_while_there_is_something_to_do(self):
        # job 2 (important) waits for carol's 16 hours; bob does the unimportant job 3 meanwhile instead of idling
        r = run([job(1, 16, 2), job(2, 8, 1, blockers=[1], order=(0,)), job(3, 8, 1, order=(9,))], {1: 8, 2: 8})
        self.assertEqual(span(r, 3), (0, 1))
        self.assertEqual(span(r, 2), (2, 3))
        self.assertEqual((r[2].why["kind"], r[2].why["job"], r[2].why["at"]), ("blocker", 1, 2))

    def test_a_person_does_not_drop_a_job_for_a_more_important_one_that_turns_up(self):
        r = run([job(1, 24, 1, order=(9,)), job(2, 8, 2), job(3, 8, 1, blockers=[2], order=(0,))], {1: 8, 2: 8})
        self.assertEqual(span(r, 1), (0, 3))  # no pre-emption: bob finishes what he started at day 0
        self.assertEqual(span(r, 3), (3, 4))
        self.assertEqual(r[3].why["kind"], "queue")

    def test_a_start_day_holds_a_job_back(self):
        r = run([job(1, 8, 1, earliest=3)], {1: 8})
        self.assertEqual(span(r, 1), (3, 4))
        self.assertEqual(r[1].why["kind"], "start_date")

    def test_a_start_day_that_comes_after_the_blockers_finish(self):
        r = run([job(1, 8, 1), job(2, 8, 2, blockers=[1], earliest=3)], {1: 8, 2: 8})
        self.assertEqual(span(r, 2), (3, 4))
        self.assertEqual(r[2].why["kind"], "start_date")

    def test_a_start_day_that_has_already_passed_changes_nothing(self):
        r = run([job(1, 8, 1, earliest=0)], {1: 8})
        self.assertEqual((span(r, 1), r[1].why["kind"]), ((0, 1), "immediately"))

    def test_part_time_work_takes_longer(self):
        r = run([job(1, 10, 1, order=(0,)), job(2, 6, 1, order=(1,))], {1: 4})
        self.assertEqual([span(r, 1), span(r, 2)], [(0, F(5, 2)), (F(5, 2), 4)])

    def test_exact_arithmetic_keeps_a_job_that_needs_three_days_on_the_third_day(self):
        pace = F(8, 3)  # 8 hours at 8/3 hours a day: floats would give 3.0000000000000004 and a wrong date
        r = run([job(1, 8, 1)], {1: pace})
        self.assertEqual(r[1].finish, 3)
        self.assertEqual(date_of_time(date(2026, 10, 19), r[1].finish), date(2026, 10, 21))  # Mon, Tue, Wed

    def test_people_who_cannot_work_never_finish_and_neither_does_what_waits_for_them(self):
        r = run([job(1, 8, 1), job(2, 8, 2, blockers=[1]), job(3, 8, 2, order=(1,)), job(4, 8, 2, blockers=[2], order=(2,))], {1: 0, 2: 8})
        self.assertEqual(r[1].status, "no_capacity")
        self.assertEqual((r[2].status, r[2].waits_for), ("waiting_for_unschedulable", 1))
        self.assertEqual((r[4].status, r[4].waits_for), ("waiting_for_unschedulable", 2))
        self.assertEqual((r[3].status, span(r, 3)), ("scheduled", (0, 1)))  # unrelated work carries on

    def test_a_job_stuck_behind_several_unschedulable_jobs_names_the_most_important(self):
        r = run([job(1, 8, 1, order=(5,)), job(2, 8, 1, order=(2,)), job(3, 8, 2, blockers=[1, 2])], {1: 0, 2: 8})
        self.assertEqual((r[3].status, r[3].waits_for), ("waiting_for_unschedulable", 2))  # order (2,) beats (5,), whatever the ids say

    def test_unassigned_jobs_run_side_by_side_at_the_fixed_pace(self):
        r = run([job(1, 16), job(2, 16), job(3, 4, blockers=[1])])
        self.assertEqual([span(r, 1), span(r, 2)], [(0, 2), (0, 2)])
        self.assertEqual(span(r, 3), (2, F(5, 2)))  # 4 hours at 8 a day
        r = run([job(1, 16)], unassigned=F(4))
        self.assertEqual(r[1].finish, 4)

    def test_a_person_the_engine_does_not_know_is_treated_as_unassigned(self):
        r = run([job(1, 8, person=99)], {1: 8})
        self.assertEqual(span(r, 1), (0, 1))

    def test_work_with_nothing_left_to_do_finishes_the_moment_it_can(self):
        r = run([job(1, 0), job(2, 0, blockers=[1]), job(3, 8, 1, blockers=[2])], {1: 8})
        self.assertEqual([span(r, 1), span(r, 2), span(r, 3)], [(0, 0), (0, 0), (0, 1)])

    def test_work_with_nothing_left_waits_for_its_blockers_but_not_for_a_person(self):
        r = run([job(1, 16, 1), job(2, 0, 1, blockers=[1]), job(3, 8, 1, blockers=[2], order=(5,))], {1: 8})
        self.assertEqual(span(r, 2), (2, 2))
        self.assertEqual(span(r, 3), (2, 3))

    def test_a_blocker_that_is_not_a_job_is_ignored(self):
        r = run([job(1, 8, 1, blockers=[77, 1])], {1: 8})  # 77 does not exist; a job cannot wait for itself
        self.assertEqual(span(r, 1), (0, 1))

    def test_a_loop_of_dependencies_neither_crashes_nor_finishes(self):
        r = run([job(1, 8, 1, blockers=[2]), job(2, 8, 1, blockers=[1]), job(3, 8, 1, order=(3,))], {1: 8})
        self.assertEqual((r[1].status, r[2].status), ("waiting_for_unschedulable", "waiting_for_unschedulable"))
        self.assertEqual(r[3].status, "scheduled")

    def test_work_that_would_finish_after_the_horizon_is_reported_as_such(self):
        r = run([job(1, 400, 1), job(2, 8, 2, blockers=[1]), job(3, 8, 1, order=(5,))], {1: 8, 2: 8}, horizon=30)
        self.assertEqual(r[1].status, "beyond_horizon")
        self.assertEqual(r[1].start, 0)
        self.assertEqual(r[2].status, "waiting_for_unschedulable")
        self.assertEqual(r[3].status, "beyond_horizon")  # queued behind job 1
        self.assertEqual(run([job(1, 240, 1)], {1: 8}, horizon=30)[1].finish, 30)  # exactly on the horizon still counts

    def test_a_diamond_waits_for_the_slower_side(self):
        r = run([job(1, 8, 1), job(2, 8, 1, blockers=[1], order=(1,)), job(3, 24, 2, blockers=[1]), job(4, 8, 1, blockers=[2, 3], order=(2,))], {1: 8, 2: 8})
        self.assertEqual(span(r, 4), (4, 5))
        self.assertEqual((r[4].why["kind"], r[4].why["job"]), ("blocker", 3))  # job 3 (finishing at 4) is the last blocker

    def test_two_blockers_finishing_together_name_the_more_important_one(self):
        r = run([job(1, 8, 1, order=(2,)), job(2, 8, 2, order=(1,)), job(3, 8, 3, blockers=[1, 2])], {1: 8, 2: 8, 3: 8})
        self.assertEqual(r[3].why["job"], 2)

    def test_the_result_is_the_same_whatever_order_the_jobs_are_given_in(self):
        jobs = [job(i, e, p, b, s, o) for i, e, p, b, s, o in (
            (1, 12, 1, (), 0, (1,)), (2, 8, 2, (1,), 0, (0,)), (3, 20, 1, (), 1, (2,)), (4, 6, 2, (3,), 0, (1,)), (5, 9, None, (2,), 0, (0,)),
        )]  # fmt: skip
        reference = {k: (v.status, v.start, v.finish) for k, v in run(jobs, {1: 8, 2: 4}).items()}
        for seed in range(10):
            shuffled = jobs[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual({k: (v.status, v.start, v.finish) for k, v in run(shuffled, {1: 8, 2: 4}).items()}, reference)


# --------------------------------------------------------------------------------- an independent oracle

TICKS = 8  # one tick is an eighth of a day; with whole-hour efforts and 8, 4 or 2 hours a day every finish lands on a tick


def oracle(jobs, rates, unassigned_rate, horizon):
    """Step through time tick by tick, re-deriving who is ready and who is free every time. Returns {id: (start, finish)}."""
    start, finish, running, remaining = {}, {}, {}, {}
    waiting = set(jobs)
    t = 0

    def ready_jobs():
        return [
            j for j in sorted(waiting)
            if all(b not in jobs or b == j or (b in finish and finish[b] * TICKS <= t) for b in jobs[j].blockers) and jobs[j].earliest * TICKS <= t
        ]  # fmt: skip

    def settle():
        again = True
        while again:
            again = False
            ready = ready_jobs()
            for j in ready:  # nothing left to do: done as soon as it is ready, without using anybody's time
                if jobs[j].effort == 0:
                    start[j] = finish[j] = F(t, TICKS)
                    waiting.discard(j)
                    again = True
            if again:
                continue
            for pid in sorted(rates):
                if pid in running or rates[pid] <= 0:
                    continue
                mine = [j for j in ready if jobs[j].person == pid]
                if mine:
                    j = min(mine, key=lambda x: (jobs[x].order, x))
                    start[j], running[pid], remaining[j] = F(t, TICKS), j, jobs[j].effort
                    waiting.discard(j)
                    again = True
            for j in ready:
                if j in waiting and (jobs[j].person is None or jobs[j].person not in rates):
                    start[j], running[("u", j)], remaining[j] = F(t, TICKS), j, jobs[j].effort
                    waiting.discard(j)
                    again = True

    while True:
        settle()
        if t >= horizon * TICKS or (not running and not any(jobs[j].earliest * TICKS > t for j in waiting)):
            break
        for worker, j in list(running.items()):
            remaining[j] -= (unassigned_rate if isinstance(worker, tuple) else rates[worker]) / TICKS
        t += 1
        for worker, j in list(running.items()):
            if remaining[j] == 0:
                finish[j] = F(t, TICKS)
                del running[worker]
    return {j: (start[j], finish.get(j)) for j in start}


def random_instance(rng):
    people = {pid: F(rng.choice((8, 8, 4, 2, 0 if rng.random() < 0.15 else 8))) for pid in range(1, rng.randint(1, 3) + 1)}
    jobs = {}
    for jid in range(1, rng.randint(1, 12) + 1):
        person = None if rng.random() < 0.15 else rng.choice(list(people))
        blockers = [b for b in range(1, jid) if rng.random() < 0.3]
        jobs[jid] = Job(jid, F(0 if rng.random() < 0.1 else rng.randint(1, 20)), person, frozenset(blockers), rng.choice((0, 0, 0, 1, 2, 3, 5)), (rng.randint(0, 3), rng.randint(0, 3)))
    return jobs, people


class AgainstTheOracleTests(unittest.TestCase):
    def test_the_engine_and_the_oracle_agree_on_thousands_of_random_projects(self):
        rng = random.Random(20261016)
        finished_jobs = 0
        for case in range(3000):
            jobs, people = random_instance(rng)
            horizon = rng.choice((1000, 1000, 6, 12))  # now and then a short horizon, so that "beyond the horizon" is exercised too
            got = schedule(jobs, people, EIGHT, horizon)
            expected = oracle(jobs, people, EIGHT, horizon)
            for jid, placement in got.items():
                if placement.status == "scheduled":
                    self.assertEqual((placement.start, placement.finish), expected.get(jid), "case %d job %d" % (case, jid))
                    finished_jobs += 1
                else:
                    self.assertIsNone(expected.get(jid, (None, None))[1], "case %d job %d should not finish (%s)" % (case, jid, placement.status))
        self.assertGreater(finished_jobs, 10000)  # the cases are not trivially empty

    def test_the_schedule_obeys_its_own_rules(self):
        """Independent of the oracle: dependencies, start days and one-job-at-a-time hold for every random schedule."""
        rng = random.Random(7)
        for _ in range(1500):
            jobs, people = random_instance(rng)
            got = schedule(jobs, people, EIGHT, 1000)
            for jid, p in got.items():
                if p.status != "scheduled":
                    continue
                job_ = jobs[jid]
                self.assertGreaterEqual(p.start, job_.earliest)
                pace = EIGHT if job_.person is None or job_.person not in people else people[job_.person]
                self.assertEqual(p.finish - p.start, job_.effort / pace if job_.effort else 0)  # work with nothing left needs nobody's time
                for b in job_.blockers:
                    self.assertEqual(got[b].status, "scheduled")
                    self.assertGreaterEqual(p.start, got[b].finish)
            for pid in people:  # a person never works on two things at once
                mine = sorted((p.start, p.finish) for jid, p in got.items() if p.status == "scheduled" and jobs[jid].person == pid and jobs[jid].effort > 0)
                for (_, end), (begin, _) in zip(mine, mine[1:], strict=False):
                    self.assertLessEqual(end, begin)

    def test_the_explanations_point_at_something_that_really_ended_when_the_job_began(self):
        rng = random.Random(11)
        checked = 0
        for _ in range(1500):
            jobs, people = random_instance(rng)
            got = schedule(jobs, people, EIGHT, 1000)
            for jid, p in got.items():
                if p.status != "scheduled" or p.why["kind"] == "immediately":
                    continue
                other = p.why["job"]
                if p.why["kind"] in ("blocker", "queue"):
                    self.assertEqual(got[other].finish, p.start, "job %d says it followed %d" % (jid, other))
                    self.assertEqual(p.why["at"], p.start)
                    if p.why["kind"] == "queue":
                        self.assertEqual(jobs[other].person, jobs[jid].person)
                    else:
                        self.assertIn(other, jobs[jid].blockers)
                else:
                    self.assertEqual(p.start, jobs[jid].earliest)
                checked += 1
        self.assertGreater(checked, 2000)


class CalendarTests(unittest.TestCase):
    def test_the_first_working_day_skips_the_weekend(self):
        self.assertEqual(first_workday(date(2026, 10, 16)), date(2026, 10, 16))  # a Friday
        self.assertEqual(first_workday(date(2026, 10, 17)), date(2026, 10, 19))  # Saturday -> Monday
        self.assertEqual(first_workday(date(2026, 10, 18)), date(2026, 10, 19))  # Sunday -> Monday
        self.assertEqual(first_workday(date(2026, 10, 19)), date(2026, 10, 19))

    def test_working_days_are_counted_from_the_base(self):
        base = date(2026, 10, 16)  # Friday
        self.assertEqual([workday_date(base, i).isoformat() for i in range(7)], ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"])
        self.assertEqual([workday_index(base, date(2026, 10, d)) for d in (14, 16, 17, 18, 19, 20, 23, 24, 25, 26)], [0, 0, 1, 1, 1, 2, 5, 6, 6, 6])

    def test_dates_and_indexes_are_inverses_for_every_working_day(self):
        for base in (date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 16)):  # a Monday, a Wednesday, a Friday
            for offset in range(0, 400):
                day = base + timedelta(days=offset)
                index = workday_index(base, day)
                if day.weekday() < 5:
                    self.assertEqual(workday_date(base, index), day)
                else:
                    self.assertEqual(workday_date(base, index), day + timedelta(days=7 - day.weekday()))  # the following Monday
            for index in range(300):
                self.assertEqual(workday_index(base, workday_date(base, index)), index)
                self.assertLess(workday_date(base, index).weekday(), 5)

    def test_what_time_means_as_a_date(self):
        base = date(2026, 10, 19)  # Monday
        self.assertEqual(date_of_time(base, F(0)), date(2026, 10, 19))  # nothing to do: done today
        self.assertEqual(date_of_time(base, F(1, 4)), date(2026, 10, 19))
        self.assertEqual(date_of_time(base, F(1)), date(2026, 10, 19))  # exactly one day: the end of day 0
        self.assertEqual(date_of_time(base, F(5, 4)), date(2026, 10, 20))
        self.assertEqual(date_of_time(base, F(5)), date(2026, 10, 23))
        self.assertEqual(date_of_time(base, F(11, 2)), date(2026, 10, 26))  # into the following week
        self.assertEqual(start_date_of_time(base, F(0)), date(2026, 10, 19))
        self.assertEqual(start_date_of_time(base, F(3, 2)), date(2026, 10, 20))
        self.assertEqual(start_date_of_time(base, F(5)), date(2026, 10, 26))  # starts at the beginning of day 5


if __name__ == "__main__":
    unittest.main()
