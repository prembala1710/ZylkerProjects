"""Background delivery of queued webhooks: claim, send, record, retry with back-off.

The queue is the ``webhook_deliveries`` table, so it survives restarts and is shared by every process that
runs a worker (``zylker-pm serve`` starts one; ``zylker-pm webhooks-worker`` runs one on its own for WSGI
deployments). A delivery is *claimed* in a short write transaction - status ``sending`` plus a lease - so two
workers never take the same one, and a worker that dies mid-send simply lets the lease run out and the
delivery is picked up again. Network I/O never happens inside a database transaction.

Delivery is at-least-once: receivers should de-duplicate on the event id (``X-Zylker-Delivery`` differs per
delivery, the payload ``id`` does not). A failed attempt - network error, timeout, any non-2xx answer - is
retried after 30 s, 2 min, 10 min, 1 h and 6 h; the sixth failure is final. Deliveries may arrive out of order.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import __version__, webhook_http
from .db import connect, transaction
from .services import webhooks
from .util import iso

log = logging.getLogger("zylker.webhooks")

_DUE_SQL = (
    "SELECT d.id, d.webhook_id, d.event, d.payload, d.status, d.attempts, d.max_attempts, w.url, w.secret "
    "FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id "
    "WHERE ((d.status = 'pending' AND d.next_attempt_at <= ?) OR (d.status = 'sending' AND d.lease_until <= ?)) "
    "AND (w.active = 1 OR d.event = ?) ORDER BY d.next_attempt_at, d.id LIMIT ?"
)


def retry_delay(attempts_made: int) -> int:
    return webhooks.RETRY_DELAYS[min(attempts_made, len(webhooks.RETRY_DELAYS)) - 1]


class WebhookWorker:
    def __init__(self, config, *, clock=None, sender=None, concurrency=4, poll_interval=1.0, batch=10):
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sender = sender or webhook_http.post
        self.concurrency = max(1, concurrency)
        self.poll_interval = poll_interval
        self.batch = batch
        # An attempt can take about two network time-outs plus name resolution; the lease must outlast that.
        self.lease_seconds = int(config.webhook_timeout * 3) + 60
        self._stop = threading.Event()
        self._thread = None
        self._pool = None
        self._last_beat = None

    # -- one pass -----------------------------------------------------------------------

    def run_once(self) -> int:
        """Claim what is due and attempt it. Returns how many deliveries were attempted."""
        now = self.clock()
        self._heartbeat(now)
        claimed = self._claim(now)
        if len(claimed) > 1 and self.concurrency > 1:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="webhook-send")
            list(self._pool.map(self._attempt, claimed))
        else:
            for delivery in claimed:
                self._attempt(delivery)
        return len(claimed)

    def run_until_idle(self, max_rounds=1000) -> int:
        total = 0
        for _ in range(max_rounds):
            done = self.run_once()
            if not done:
                break
            total += done
        return total

    def _heartbeat(self, now):
        if self._last_beat is not None and abs((now - self._last_beat).total_seconds()) < 10:  # abs(): a clock set back must not silence it
            return
        conn = connect(self.config.db_path)
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO webhook_state (key, value) VALUES ('worker_heartbeat', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (iso(now),),
                )
            self._last_beat = now
        finally:
            conn.close()

    def _claim(self, now):
        now_s, lease = iso(now), iso(now + timedelta(seconds=self.lease_seconds))
        conn = connect(self.config.db_path)
        try:
            if conn.execute(_DUE_SQL, (now_s, now_s, webhooks.PING_EVENT, 1)).fetchone() is None:
                return []  # idle: a plain read, so polling every second never takes the writer lock
            with transaction(conn):
                rows = conn.execute(_DUE_SQL, (now_s, now_s, webhooks.PING_EVENT, self.batch)).fetchall()
                claimed = []
                for row in rows:
                    if row["status"] == "sending" and row["attempts"] >= row["max_attempts"]:
                        # a worker died during the final attempt: nothing is left to try
                        self._finish(conn, row["id"], row["webhook_id"], now_s, ok=False, status=None, error="lease_expired", excerpt="", ms=None)
                        continue
                    conn.execute(
                        "UPDATE webhook_deliveries SET status = 'sending', attempts = attempts + 1, lease_until = ? WHERE id = ?",
                        (lease, row["id"]),
                    )
                    claimed.append({**dict(row), "attempts": row["attempts"] + 1})
                return claimed
        finally:
            conn.close()

    # -- sending and bookkeeping --------------------------------------------------------

    def _attempt(self, delivery):
        try:
            started = self.clock()
            body = delivery["payload"].encode("utf-8")
            stamp = int(started.timestamp())
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "ZylkerProjects/%s" % __version__,
                "X-Zylker-Event": delivery["event"],
                "X-Zylker-Delivery": str(delivery["id"]),
                "X-Zylker-Attempt": str(delivery["attempts"]),
                "X-Zylker-Timestamp": str(stamp),
                "X-Zylker-Signature": webhooks.sign(delivery["secret"], stamp, body),
                "Connection": "close",
            }
            try:
                result = self.sender(
                    delivery["url"], body, headers, timeout=self.config.webhook_timeout, allow_private=self.config.webhook_allow_private
                )
            except Exception:  # noqa: BLE001 - a sender bug must not kill the worker or strand the delivery
                log.exception("webhook sender crashed")
                result = webhook_http.SendResult(False, None, "internal_error", "", 0)
            self._record(delivery, result)
        except Exception:  # noqa: BLE001
            log.exception("could not process webhook delivery %s", delivery.get("id"))

    def _record(self, delivery, result):
        now_s = iso(self.clock())
        conn = connect(self.config.db_path)
        try:
            with transaction(conn):
                current = conn.execute("SELECT status, attempts FROM webhook_deliveries WHERE id = ?", (delivery["id"],)).fetchone()
                if current is None or current["status"] != "sending" or current["attempts"] != delivery["attempts"]:
                    return  # deleted meanwhile, or the lease ran out and another worker owns it now
                if result.ok or delivery["attempts"] >= delivery["max_attempts"]:
                    self._finish(conn, delivery["id"], delivery["webhook_id"], now_s, ok=result.ok, status=result.status,
                                 error=result.error, excerpt=result.excerpt, ms=result.duration_ms)  # fmt: skip
                else:
                    retry_at = iso(self.clock() + timedelta(seconds=retry_delay(delivery["attempts"])))
                    conn.execute(
                        "UPDATE webhook_deliveries SET status = 'pending', next_attempt_at = ?, lease_until = NULL, last_attempt_at = ?, "
                        "response_status = ?, error = ?, response_excerpt = ?, duration_ms = ? WHERE id = ?",
                        (retry_at, now_s, result.status, result.error, result.excerpt, result.duration_ms, delivery["id"]),
                    )
                    self._health(conn, delivery["webhook_id"], now_s, ok=False)
        finally:
            conn.close()

    def _finish(self, conn, delivery_id, webhook_id, now_s, *, ok, status, error, excerpt, ms):
        conn.execute(
            "UPDATE webhook_deliveries SET status = ?, next_attempt_at = NULL, lease_until = NULL, last_attempt_at = ?, "
            "response_status = ?, error = ?, response_excerpt = ?, duration_ms = ? WHERE id = ?",
            ("succeeded" if ok else "failed", now_s, status, error, excerpt, ms, delivery_id),
        )
        self._health(conn, webhook_id, now_s, ok=ok)
        # keep the log bounded: only the newest KEEP_DELIVERIES finished deliveries per webhook stay
        conn.execute(
            "DELETE FROM webhook_deliveries WHERE webhook_id = ? AND status IN ('succeeded','failed') AND id <= "
            "(SELECT id FROM webhook_deliveries WHERE webhook_id = ? AND status IN ('succeeded','failed') "
            "ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (webhook_id, webhook_id, webhooks.KEEP_DELIVERIES),
        )

    @staticmethod
    def _health(conn, webhook_id, now_s, *, ok):
        conn.execute(
            "UPDATE webhooks SET last_delivery_at = ?, last_status = ?, "
            "consecutive_failures = CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END WHERE id = ?",
            (now_s, "succeeded" if ok else "failed", 1 if ok else 0, webhook_id),
        )

    # -- thread lifecycle ---------------------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="webhook-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout=10.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    def run_forever(self):
        """Process the queue in the calling thread until ``stop()`` is called."""
        failures = 0
        while not self._stop.is_set():
            try:
                busy = self.run_once()
                failures = 0
            except Exception:  # noqa: BLE001 - e.g. the database is briefly unavailable
                failures += 1
                log.exception("webhook worker pass failed (%d in a row)", failures)
                busy = 0
            if not busy:
                # idle: poll again soon; failing: back off (up to 30 s) instead of filling the log
                self._stop.wait(min(30.0, self.poll_interval * 2 ** min(failures, 5)))
