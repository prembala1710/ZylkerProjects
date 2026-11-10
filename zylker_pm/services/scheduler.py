"""A small, deterministic scheduling engine: *when will each piece of open work actually finish?*

The engine knows nothing about projects or databases. It is given jobs (hours of work still to do, who does them, what
must finish first, and the earliest working day each may start) and people (hours they can work per working day), and
it plays the work forward in time:

* time is measured in **working days** from the start of today (0 = the first working day); a person with ``r`` hours
  a day does ``r`` hours of work per day, on **one job at a time**, and does not stop in the middle of a job;
* whenever a person is free, they take the ready job that sorts first by ``order`` (the caller puts priority first, then
  the due date). A job is *ready* once everything it waits for has finished and its start day has come. Nobody sits idle
  while there is something they could do: that is why this is a "parallel" schedule generation scheme;
* jobs without a person proceed at a fixed pace, each on its own, without queueing;
* a job whose person has no capacity, and everything waiting for such a job, never finishes.

All arithmetic uses :class:`fractions.Fraction`, so a job that needs exactly three days finishes at 3 - not at
3.0000000000000004, which would put it on the wrong day. Every decision is deterministic: the same input always gives
the same schedule, which is what lets the numbers be explained and tested.

For every job that finishes the engine also records *why it started when it did* (``why``): it was free to start
immediately, it waited for another job to finish, it waited behind another job in its person's queue, or it waited for its
start day. Following those links back from a late job leads to the root cause of the delay.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

ZERO = Fraction(0)


@dataclass(frozen=True)
class Job:
    id: int
    effort: Fraction  # hours of work still to do (0 = nothing left but to close it)
    person: int | None  # who does it; None = nobody in particular
    blockers: frozenset  # ids of jobs that must finish first (ids that are not jobs are ignored)
    earliest: int = 0  # first working day on which it may start (0 = today)
    order: tuple = ()  # smaller sorts first: which ready job a free person picks


@dataclass
class Placement:
    status: str  # scheduled | no_capacity | waiting_for_unschedulable | beyond_horizon
    start: Fraction | None = None
    finish: Fraction | None = None
    why: dict | None = None  # {"kind": immediately|blocker|queue|start_date, "job": id or None, "at": Fraction}
    waits_for: int | None = None  # for waiting_for_unschedulable: the job it is stuck behind


def schedule(jobs: dict, rates: dict, unassigned_rate: Fraction, horizon: int) -> dict:
    """Play the jobs forward and return ``{job id: Placement}``.

    ``rates`` maps a person id to hours per working day (0 means "cannot work"); ``horizon`` is how many working days to
    look ahead: a job that would finish later is reported as ``beyond_horizon``.
    """
    unmet = {jid: {b for b in job.blockers if b in jobs and b != jid} for jid, job in jobs.items()}
    dependents = defaultdict(list)
    for jid in sorted(unmet):
        for blocker in unmet[jid]:
            dependents[blocker].append(jid)

    now = ZERO
    finished: dict = {}
    placed: dict = {}
    ready_info: dict = {}  # job -> (when its last blocker finished, which blocker was last)
    eligible = {pid: [] for pid in rates}  # per person: heap of (order, job id) that could start now
    unassigned: list = []  # heap of (order, job id)
    waiting: list = []  # heap of (start time, order, job id): blockers done, but the start day is ahead
    last_free = {pid: (ZERO, None) for pid in rates}  # when a person last became free, and after which job
    running: dict = {}  # worker -> job id; worker is a person id or ("u", job id)
    due: list = []  # heap of (finish time, 0 = person / 1 = unassigned, person or job id, worker): a job's finish time is fixed the moment it starts
    pending: set = set()  # people who have something ready and may be free

    def key(jid):
        return (jobs[jid].order, jid)

    def record_start(jid, at, person):
        done_at, binding = ready_info[jid]
        queue_at, queue_job = last_free[person] if person is not None and person in last_free else (ZERO, None)
        earliest = Fraction(jobs[jid].earliest)
        if at == 0:
            why = {"kind": "immediately", "job": None, "at": ZERO}
        elif binding is not None and done_at == at:
            why = {"kind": "blocker", "job": binding, "at": done_at}
        elif queue_job is not None and queue_at == at:
            why = {"kind": "queue", "job": queue_job, "at": queue_at}
        elif earliest == at:
            why = {"kind": "start_date", "job": None, "at": earliest}
        else:  # unreachable in practice; stay explainable rather than fail
            why = {"kind": "queue", "job": queue_job, "at": queue_at}
        placed[jid] = Placement("scheduled", start=at, why=why)

    def finish(jid, at):
        finished[jid] = at
        placed[jid].finish = at
        for dep in dependents[jid]:
            unmet[dep].discard(jid)
            if not unmet[dep]:
                done = [b for b in jobs[dep].blockers if b in finished]
                latest = max(finished[b] for b in done)
                make_ready(dep, latest, min((b for b in done if finished[b] == latest), key=key))  # ties: the most important one

    def make_ready(jid, blockers_done_at, binding):
        ready_info[jid] = (blockers_done_at, binding)
        job = jobs[jid]
        at = max(blockers_done_at, Fraction(job.earliest))
        if at > now:
            heapq.heappush(waiting, (at, key(jid)[0], jid))
        elif job.effort == 0:
            record_start(jid, at, None)
            finish(jid, at)  # nothing left to do: it finishes the moment it becomes ready
        elif job.person is None or job.person not in rates:
            heapq.heappush(unassigned, key(jid))
        else:
            heapq.heappush(eligible[job.person], key(jid))
            pending.add(job.person)

    for jid in [j for j in sorted(jobs) if not unmet[j]]:  # the starting points, picked before any of them can release others
        make_ready(jid, ZERO, None)

    def start(worker, kind, who, jid, pace):
        running[worker] = jid
        heapq.heappush(due, (now + jobs[jid].effort / pace, kind, who, worker))

    while True:
        progressed = True
        while progressed:
            progressed = False
            while waiting and waiting[0][0] <= now:
                _, _, jid = heapq.heappop(waiting)
                make_ready(jid, ready_info[jid][0], ready_info[jid][1])
                progressed = True
            for pid in sorted(pending):
                pending.discard(pid)
                if pid in running or rates[pid] <= 0 or not eligible[pid]:
                    continue
                _, jid = heapq.heappop(eligible[pid])
                record_start(jid, now, pid)
                start(pid, 0, pid, jid, rates[pid])
                progressed = True
            while unassigned:
                _, jid = heapq.heappop(unassigned)
                record_start(jid, now, None)
                start(("u", jid), 1, jid, jid, unassigned_rate)
                progressed = True
        upcoming = [t for t in (due[0][0] if due else None, waiting[0][0] if waiting else None) if t is not None]
        if not upcoming:
            break
        now = min(upcoming)
        if now > horizon:
            break
        while due and due[0][0] == now:  # everything that finishes at this moment, people before unassigned work, lowest id first
            _, _, _, worker = heapq.heappop(due)
            jid = running.pop(worker)
            if not isinstance(worker, tuple):
                last_free[worker] = (now, jid)
                if eligible[worker]:
                    pending.add(worker)
            finish(jid, now)

    for jid, job in jobs.items():
        if jid in finished:
            continue
        if jid in placed:  # started but not finished within the horizon
            placed[jid] = Placement("beyond_horizon", start=placed[jid].start)
        elif job.person is not None and job.person in rates and rates[job.person] <= 0:
            placed[jid] = Placement("no_capacity")
        elif unmet[jid]:
            placed[jid] = Placement("waiting_for_unschedulable", waits_for=min(unmet[jid], key=key))
        else:
            placed[jid] = Placement("beyond_horizon")
    return placed


# ---------------------------------------------------------------------------------------- the working calendar


def first_workday(today):
    """Index 0 of the schedule: today if it is Monday to Friday, otherwise the coming Monday."""
    return today + timedelta(days=max(0, 7 - today.weekday())) if today.weekday() >= 5 else today


def workday_index(base, day) -> int:
    """How many working days after ``base`` (a working day) the date ``day`` falls; weekends count as the next Monday."""
    if day <= base:
        return 0
    monday = base.toordinal() - base.weekday()
    weeks, dow = divmod(day.toordinal() - monday, 7)
    return weeks * 5 + min(dow, 5) - base.weekday()


def workday_date(base, index: int):
    """The calendar date of working day ``index`` counted from ``base``."""
    position = base.weekday() + index
    weeks, dow = divmod(position, 5)
    return date.fromordinal(base.toordinal() - base.weekday() + 7 * weeks + dow)


def date_of_time(base, at: Fraction):
    """The date on which something that finishes at time ``at`` is done (finishing at exactly 3 means the end of day 2)."""
    index = max(0, -((-at.numerator) // at.denominator) - 1)  # ceil(at) - 1, never below the first day
    return workday_date(base, index)


def start_date_of_time(base, at: Fraction):
    """The date on which something that starts at time ``at`` begins (starting at exactly 3 means the start of day 3)."""
    return workday_date(base, at.numerator // at.denominator)
