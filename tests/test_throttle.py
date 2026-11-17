"""Brute-force protection and per-user rate limiting: the limiter itself, then the HTTP behaviour around it."""

import dataclasses
import threading
import unittest
from datetime import timedelta

from helpers import ApiTestCase, Client

from zylker_pm import throttle as th
from zylker_pm.app import App
from zylker_pm.config import Config
from zylker_pm.throttle import Throttle

IP = "198.51.100.9"


class FailureLimiterTests(unittest.TestCase):
    def fail(self, t, now, ip=IP, n=1):
        out = None
        for _ in range(n):
            out = t.on_failure(ip, now)
        return out

    def test_it_is_off_unless_configured(self):
        t = Throttle()
        self.assertEqual(self.fail(t, 0, n=500).outcome, "off")
        self.assertEqual(t.take(1, 0), (True, 0, 0, 0))

    def test_failures_below_the_limit_are_only_counted(self):
        t = Throttle(failure_limit=5)
        self.assertEqual(self.fail(t, 0, n=4).outcome, "counted")

    def test_the_failure_that_reaches_the_limit_starts_a_one_minute_block(self):
        t = Throttle(failure_limit=5)
        v = self.fail(t, 0, n=5)
        self.assertEqual((v.outcome, v.retry_after, v.strikes, v.failures), ("blocked_now", 60, 1, 5))
        v = t.on_failure(IP, 10)
        self.assertEqual((v.outcome, v.retry_after), ("blocked", 50))

    def test_blocked_attempts_do_not_extend_the_block(self):
        t = Throttle(failure_limit=2)
        self.fail(t, 0, n=2)
        for second in range(1, 59):
            t.on_failure(IP, second)
        self.assertEqual(t.on_failure(IP, 59).retry_after, 1)
        self.assertEqual(t.on_failure(IP, 61).outcome, "counted")  # over: a fresh count starts

    def test_old_failures_age_out_of_the_window(self):
        t = Throttle(failure_limit=3)
        self.fail(t, 0, n=2)
        self.assertEqual(self.fail(t, th.FAILURE_WINDOW + 1).outcome, "counted")  # the two old ones no longer count
        self.assertEqual(self.fail(t, th.FAILURE_WINDOW + 2).outcome, "counted")
        self.assertEqual(self.fail(t, th.FAILURE_WINDOW + 3).outcome, "blocked_now")

    def test_each_repeat_doubles_the_block_up_to_an_hour(self):
        t = Throttle(failure_limit=1)
        now, seen = 0.0, []
        for _ in range(9):
            v = t.on_failure(IP, now)
            seen.append(v.retry_after)
            now += v.retry_after + 1
        self.assertEqual(seen, [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600])

    def test_a_quiet_hour_forgives_the_strikes(self):
        t = Throttle(failure_limit=1)
        t.on_failure(IP, 0)
        t.on_failure(IP, 61)  # second strike: 120 s, ends at 181
        v = t.on_failure(IP, 181 + th.STRIKE_MEMORY + 1)
        self.assertEqual((v.outcome, v.retry_after, v.strikes), ("blocked_now", 60, 1))

    def test_addresses_are_independent_and_a_missing_one_has_its_own_bucket(self):
        t = Throttle(failure_limit=2)
        self.fail(t, 0, n=2)
        self.assertEqual(t.on_failure("203.0.113.1", 1).outcome, "counted")
        self.assertEqual(self.fail(t, 0, ip=None, n=2).outcome, "blocked_now")
        self.assertEqual([b["ip"] for b in t.blocked(1)], [IP, "unknown"])

    def test_unblock_one_or_all(self):
        t = Throttle(failure_limit=1)
        t.on_failure(IP, 0)
        t.on_failure("203.0.113.1", 0)
        self.assertEqual(t.unblock("203.0.113.1"), 1)
        self.assertEqual(t.unblock("203.0.113.1"), 0)
        self.assertEqual([b["ip"] for b in t.blocked(1)], [IP])
        self.assertEqual(t.unblock(), 1)
        self.assertEqual(t.blocked(1), [])
        self.assertEqual(t.on_failure(IP, 2).strikes, 1)  # history is gone too

    def test_concurrent_failures_cannot_all_slip_under_the_limit(self):
        t = Throttle(failure_limit=5)
        outcomes, gate = [], threading.Barrier(40)

        def worker():
            gate.wait()
            outcomes.append(t.on_failure(IP, 0).outcome)

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for x in threads:
            x.start()
        for x in threads:
            x.join()
        self.assertEqual(sorted(set(outcomes)), ["blocked", "blocked_now", "counted"])
        self.assertEqual((outcomes.count("counted"), outcomes.count("blocked_now"), outcomes.count("blocked")), (4, 1, 35))


class BudgetTests(unittest.TestCase):
    def test_burst_then_refusal_with_a_retry_time(self):
        t = Throttle(rate_per_minute=60)  # one a second, burst 12
        results = [t.take(1, 0) for _ in range(13)]
        self.assertEqual([r[0] for r in results], [True] * 12 + [False])
        self.assertEqual(results[0][1:], (0, 60, 11))  # (retry, limit, remaining)
        self.assertEqual(results[-1][1:], (1, 60, 0))

    def test_it_refills_at_the_sustained_rate_and_never_beyond_the_burst(self):
        t = Throttle(rate_per_minute=60)
        for _ in range(12):
            t.take(1, 0)
        self.assertEqual(t.take(1, 5)[:1], (True,))  # 5 s later: 5 tokens, one spent
        self.assertEqual(t.take(1, 5 + 3600)[3], 11)  # an hour later it is full, not overfull

    def test_users_have_separate_budgets(self):
        t = Throttle(rate_per_minute=60)
        for _ in range(12):
            t.take(1, 0)
        self.assertFalse(t.take(1, 0)[0])
        self.assertTrue(t.take(2, 0)[0])

    def test_small_rates_have_a_burst_no_bigger_than_the_rate(self):
        self.assertEqual([th.burst_for(r) for r in (3, 10, 60, 600, 6000)], [3, 10, 12, 120, 1200])

    def test_concurrent_requests_never_exceed_the_budget(self):
        t = Throttle(rate_per_minute=60)
        allowed, gate = [], threading.Barrier(50)

        def worker():
            gate.wait()
            allowed.append(t.take(1, 0)[0])

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for x in threads:
            x.start()
        for x in threads:
            x.join()
        self.assertEqual(sum(allowed), 12)

    def test_memory_stays_bounded(self):
        t = Throttle(failure_limit=3, rate_per_minute=60)
        for i in range(th.MAX_TRACKED + 500):
            t.take(i, float(i))
        self.assertLessEqual(t.tracked(), th.MAX_TRACKED)
        for i in range(th.MAX_TRACKED + 500):
            t.on_failure("10.%d.%d.%d" % (i >> 16 & 255, i >> 8 & 255, i & 255), float(i))
        self.assertLessEqual(t.tracked(), th.MAX_TRACKED)


class ThrottledApi(ApiTestCase):
    """The fixture app with throttling switched on: 3 failures per address, 60 requests a minute."""

    failure_limit, rate = 3, 60

    def setUp(self):
        super().setUp()
        self.config = dataclasses.replace(self.config, auth_failure_limit=self.failure_limit, rate_limit_per_minute=self.rate)
        self.app = App(self.config, clock=lambda: self.now, notifier=self.notifier)
        self.admin = self.client("admin")

    def bad(self, n=1, ip=IP, token="zpm_nope"):
        return [self.client().get("/api/me", token=token, remote_addr=ip) for _ in range(n)]

    def audit(self, **q):
        return self.ok(self.admin.get("/api/audit", query=q))["items"]


class BruteForceTests(ThrottledApi):
    def test_the_third_failure_is_still_a_401_and_the_fourth_is_refused_with_a_wait(self):
        statuses = [r.status for r in self.bad(3)]
        self.assertEqual(statuses, [401, 401, 401])
        r = self.bad()[0]
        self.assertEqual((r.status, r.error_code), (429, "too_many_failures"))
        self.assertEqual(r.headers["Retry-After"], "60")
        self.assertEqual(r.json["error"]["details"], {"scope": "ip", "retry_after": 60})
        self.assertIn("X-Request-Id", r.headers)

    def test_a_valid_token_from_a_blocked_address_still_works(self):
        self.bad(5)
        self.assertEqual(self.client("bob").get("/api/me", remote_addr=IP).status, 200)  # a shared office address is not locked out

    def test_other_addresses_are_unaffected(self):
        self.bad(5)
        self.assertEqual(self.bad(1, ip="203.0.113.50")[0].status, 401)

    def test_missing_tokens_count_as_failures_too(self):
        for _ in range(3):
            self.client().get("/api/me", token=None, remote_addr=IP)
        self.assertEqual(self.client().get("/api/me", token=None, remote_addr=IP).status, 429)

    def test_the_block_expires_and_the_next_one_is_twice_as_long(self):
        self.bad(4)
        self.now += timedelta(seconds=61)
        self.assertEqual(self.bad(1)[0].status, 401)  # allowed to try again
        self.bad(2)  # third failure of the new window: blocked for 120 s
        r = self.bad()[0]
        self.assertEqual((r.status, r.headers["Retry-After"]), (429, "120"))

    def test_a_blocked_address_can_be_listed_and_released_by_an_admin(self):
        self.bad(4)
        info = self.ok(self.admin.get("/api/security/blocks"))
        [b] = info["blocked"]
        self.assertEqual((b["ip"], b["strikes"], b["retry_after"]), (IP, 1, 60))
        self.assertEqual(info["settings"]["failure_limit"], 3)
        self.assertEqual(self.ok(self.admin.delete("/api/security/blocks", query={"ip": IP})), {"removed": 1})
        self.assertEqual(self.bad(1)[0].status, 401)
        self.assertEqual(self.ok(self.admin.get("/api/security/blocks"))["blocked"], [])

    def test_releasing_everything_and_bad_input(self):
        self.bad(4)
        self.bad(4, ip="203.0.113.50")
        self.assertEqual(self.ok(self.admin.delete("/api/security/blocks")), {"removed": 2})
        r = self.admin.delete("/api/security/blocks", query={"ip": "not-an-ip"})
        self.assertEqual((r.status, "ip" in r.json["error"]["details"]), (422, True))

    def test_only_admins_see_or_change_blocks(self):
        for user in ("alice", "bob", "vera"):
            self.assertEqual(self.client(user).get("/api/security/blocks").status, 403, user)
            self.assertEqual(self.client(user).delete("/api/security/blocks").status, 403, user)
        self.assertEqual(self.client().get("/api/security/blocks", remote_addr="203.0.113.77").status, 401)

    def test_every_step_is_in_the_audit_log_and_the_summary(self):
        self.bad(3)
        self.bad(6)
        started = self.audit(action="auth.throttled")
        reasons = sorted((e["details"]["reason"], e["details"].get("count", 1)) for e in started)
        self.assertEqual(reasons, [("blocked", 1), ("still_blocked", 6)])  # the repeats merged into one counted row
        blocked = next(e for e in started if e["details"]["reason"] == "blocked")
        self.assertEqual((blocked["details"]["strikes"], blocked["details"]["seconds"], blocked["severity"], blocked["ip"]), (1, 60, "warning", IP))
        self.assertEqual(sum(e["details"]["count"] for e in self.audit(action="auth.failed")), 3)  # only real attempts are "failed sign-ins"
        security = self.ok(self.admin.get("/api/audit/summary"))["security"]
        self.assertEqual((security["failed_sign_ins"], security["throttled"]), (3, 7))
        self.admin.delete("/api/security/blocks")
        [e] = self.audit(action="auth.unblocked")
        self.assertEqual((e["details"], e["actor"]["username"], e["severity"]), ({"ip": "all", "removed": 1}, "admin", "notice"))

    def test_a_flood_from_threads_lets_exactly_the_limit_through(self):
        statuses, gate = [], threading.Barrier(30)

        def worker():
            gate.wait()
            statuses.append(self.client().get("/api/me", token="zpm_nope", remote_addr="203.0.113.99").status)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for x in threads:
            x.start()
        for x in threads:
            x.join()
        self.assertEqual((statuses.count(401), statuses.count(429)), (3, 27))

    def test_behind_a_proxy_each_client_is_judged_on_its_own_address(self):
        cfg = dataclasses.replace(self.config, proxy_hops=1)
        app = App(cfg, clock=lambda: self.now)

        def hit(client_ip):
            return Client(app).get("/api/me", token="zpm_nope", remote_addr="10.0.0.2", headers={"X-Forwarded-For": client_ip}).status

        self.assertEqual([hit("198.51.100.1") for _ in range(4)], [401, 401, 401, 429])
        self.assertEqual(hit("198.51.100.2"), 401)  # same proxy, different client: not blocked


class RateLimitTests(ThrottledApi):
    def test_headers_count_down_and_the_budget_runs_out(self):
        bob = self.client("bob")
        first = bob.get("/api/me")
        self.assertEqual((first.headers["X-RateLimit-Limit"], first.headers["X-RateLimit-Remaining"]), ("60", "11"))
        for _ in range(11):
            r = bob.get("/api/me")
        self.assertEqual(r.headers["X-RateLimit-Remaining"], "0")
        r = bob.get("/api/me")
        self.assertEqual((r.status, r.error_code, r.headers["Retry-After"]), (429, "rate_limited", "1"))
        self.assertEqual(r.json["error"]["details"], {"scope": "user", "retry_after": 1})
        self.assertEqual(r.headers["X-RateLimit-Remaining"], "0")

    def test_it_refills_while_you_wait(self):
        bob = self.client("bob")
        for _ in range(12):
            bob.get("/api/me")
        self.assertEqual(bob.get("/api/me").status, 429)
        self.now += timedelta(seconds=3)
        self.assertEqual([bob.get("/api/me").status for _ in range(4)], [200, 200, 200, 429])

    def test_budgets_are_per_user_and_health_and_the_page_are_exempt(self):
        bob = self.client("bob")
        for _ in range(13):
            bob.get("/api/me")
        self.assertEqual(self.client("carol").get("/api/me").status, 200)
        for _ in range(30):
            self.assertEqual(self.client().get("/api/health", token=None).status, 200)
            self.assertEqual(self.client().get("/", token=None).status, 200)

    def test_writes_are_charged_and_a_refused_request_does_nothing(self):
        self.make_project("WEB", members=("bob",))
        bob = self.client("bob")
        for _ in range(12):
            bob.get("/api/me")
        r = bob.post("/api/projects/WEB/tasks", {"title": "Over budget"})
        self.assertEqual(r.status, 429)
        self.assertEqual(self.ok(self.admin.get("/api/tasks", query={"project": "WEB"}))["total"], 0)

    def test_it_is_audited_against_the_user_and_merged(self):
        bob = self.client("bob")
        for _ in range(20):
            bob.get("/api/me")
        [e] = self.audit(action="auth.throttled")
        self.assertEqual((e["actor"]["username"], e["details"]["reason"], e["details"]["scope"], e["details"]["count"]), ("bob", "rate_limited", "user", 8))

    def test_a_denied_role_still_costs_a_request(self):
        vera = self.client("vera")
        for _ in range(12):
            vera.post("/api/projects", {"key": "XX", "name": "x"})  # 403, but counted
        self.assertEqual(vera.get("/api/me").status, 429)


class ConfigurationTests(ApiTestCase):
    def test_programmatic_defaults_are_off_and_the_environment_defaults_are_on(self):
        self.assertEqual((Config().auth_failure_limit, Config().rate_limit_per_minute), (0, 0))
        env = Config.from_env({})
        self.assertEqual((env.auth_failure_limit, env.rate_limit_per_minute), (th.DEFAULT_FAILURE_LIMIT, th.DEFAULT_RATE_PER_MINUTE))
        off = Config.from_env({"ZYLKER_AUTH_FAILURE_LIMIT": "0", "ZYLKER_RATE_LIMIT_PER_MINUTE": "0"})
        self.assertEqual((off.auth_failure_limit, off.rate_limit_per_minute), (0, 0))

    def test_bad_values_are_refused(self):
        for name, bad in (("AUTH_FAILURE_LIMIT", "-1"), ("AUTH_FAILURE_LIMIT", "1001"), ("AUTH_FAILURE_LIMIT", "x"), ("RATE_LIMIT_PER_MINUTE", "-5"),
                          ("RATE_LIMIT_PER_MINUTE", "100001"), ("RATE_LIMIT_PER_MINUTE", "")):  # fmt: skip
            with self.assertRaises(ValueError, msg=(name, bad)):
                Config.from_env({"ZYLKER_" + name: bad})

    def test_with_throttling_off_nothing_is_ever_refused(self):
        for _ in range(60):
            self.assertEqual(self.client().get("/api/me", token="zpm_nope").status, 401)
        for _ in range(300):
            self.assertEqual(self.client("bob").get("/api/me").status, 200)
        r = self.client("bob").get("/api/me")
        self.assertNotIn("X-RateLimit-Limit", r.headers)
        self.assertEqual(self.ok(self.admin.get("/api/security/blocks"))["blocked"], [])


class OverARealSocketTests(ApiTestCase):
    def test_the_429_and_its_headers_reach_a_real_http_client(self):
        import urllib.error
        import urllib.request

        from zylker_pm.cli import make_threaded_server

        cfg = dataclasses.replace(self.config, auth_failure_limit=2, rate_limit_per_minute=60)
        server = make_threaded_server(App(cfg), "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/api/me" % server.server_port

        def call(token):
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers)

        self.assertEqual([call("zpm_nope")[0] for _ in range(2)], [401, 401])
        status, headers = call("zpm_nope")
        self.assertEqual((status, headers["Retry-After"]), (429, "60"))
        self.assertTrue(headers["X-Request-Id"])
        status, headers = call(self.tokens["bob"])
        self.assertEqual((status, headers["X-RateLimit-Limit"]), (200, "60"))  # a valid token still gets through
