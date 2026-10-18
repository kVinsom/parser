"""Durable shared proxy allowance; reserve bytes before forwarding opaque TLS.

The counter is a conservative local allowance, not the provider's billing meter.
All users and markets share it. Missing/corrupt storage blocks paid source I/O.
"""

import json
import logging
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import daily_budget

logger = logging.getLogger("uvicorn.error")
QUANTUM = 8192
BURST = 4 * 1024 * 1024
DEMAND_TTL = 10  # Renewed by local preflight retries; abandoned work cannot block a lane.
CODES = frozenset({"ProxyBudgetWait", "ProxyBudgetExceeded", "ProxyBudgetNotStarted", "ProxyBudgetUnavailable"})


class BudgetPause(Exception):
    def __init__(self, code, retry_after=30):
        self.code = code
        self.retry_after = max(1, min(86400, math.ceil(retry_after)))
        super().__init__(code)


@dataclass
class Admission:
    month: str
    kind: str
    remaining: float
    observed: float = 0


class ProxyBudget:
    def __init__(self, path, *, start_month="2026-11", limit_bytes=40_000_000_000,
                 stop_bytes=36_000_000_000, clock=None, tz=None):
        self.path = Path(path)
        self.start_month, self.limit, self.stop = start_month, limit_bytes, stop_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tz = tz or ZoneInfo("Europe/Kyiv")
        self.lock = threading.RLock()
        self.held = {}
        self.waiting = {}  # Insertion order is the turn order across request kinds.
        self.sequence = 0
        self.available = True
        if (len(start_month) != 7 or datetime.strptime(start_month, "%Y-%m").replace(tzinfo=self.tz).strftime("%Y-%m") != start_month
                or not 0 < stop_bytes < limit_bytes):
            raise ValueError("Invalid proxy budget configuration")
        try:
            # A new empty volume cannot silently reset an already active month.
            if not self.path.exists() and self._now().strftime("%Y-%m") >= start_month:
                raise OSError("Budget baseline required before active period")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._transaction() as db:
                db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
                expected = json.dumps([1, start_month, limit_bytes, stop_bytes])
                previous = db.execute("SELECT value FROM config WHERE id=1").fetchone()
                if previous is None and self._now().strftime("%Y-%m") >= start_month:
                    raise ValueError("Budget baseline missing during active period")
                if previous and previous[0] != expected:
                    raise ValueError("Budget configuration mismatch")
                db.execute("INSERT OR IGNORE INTO config (id,value) VALUES (1, ?)", (expected,))
                db.execute("""CREATE TABLE IF NOT EXISTS months (
                    month TEXT PRIMARY KEY, charged INTEGER NOT NULL CHECK(charged>=0),
                    credit REAL NOT NULL, updated REAL NOT NULL,
                    catalog_cost REAL NOT NULL, details_cost REAL NOT NULL,
                    admitted INTEGER NOT NULL, warned INTEGER NOT NULL)""")
                daily_budget.migrate(db)
            os.chmod(self.path, 0o600)
        except (OSError, ValueError, sqlite3.Error):
            self.available = False
            logger.error("Proxy budget storage unavailable; paid source I/O blocked")

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Budget clock must be timezone aware")
        return value.astimezone(self.tz)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _row(self, db, now):
        month = now.strftime("%Y-%m")
        if month < self.start_month:
            start = datetime.strptime(self.start_month, "%Y-%m").replace(tzinfo=self.tz)
            raise BudgetPause("ProxyBudgetNotStarted", start.timestamp() - now.timestamp())
        latest = db.execute("SELECT MAX(month) FROM months").fetchone()[0]
        if latest and month < latest:
            raise BudgetPause("ProxyBudgetUnavailable")
        year, number = now.year, now.month
        end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=self.tz).timestamp()
        row = db.execute("SELECT * FROM months WHERE month=?", (month,)).fetchone()
        if row is None:
            db.execute("INSERT INTO months VALUES (?,0,?,?,131072,524288,0,0)",
                       (month, min(BURST, self.stop), now.timestamp()))
            row = db.execute("SELECT * FROM months WHERE month=?", (month,)).fetchone()
        if (len(row) != 8 or not 0 <= row[1] <= self.stop or not all(
                math.isfinite(value) for value in row[2:6]) or row[3] > now.timestamp() + 1):
            raise BudgetPause("ProxyBudgetUnavailable")
        rate = max(0, self.stop - row[1]) / max(1, end - now.timestamp())
        daily = daily_budget.state(db, now, month, row[1], self.stop)
        rate = min(rate, daily['daily_remaining_bytes'] / daily['daily_reset_seconds'])
        credit = min(BURST, row[2] + max(0, now.timestamp() - row[3]) * rate)
        return row, credit, rate, end

    @contextmanager
    def _state(self):
        with self.lock:
            if not self.available or not self.path.exists():
                raise BudgetPause("ProxyBudgetUnavailable")
            try:
                with self._transaction() as db:
                    now = self._now()
                    row, credit, rate, end = self._row(db, now)
                    yield db, now, row, credit, rate, end
            except (OSError, ValueError, sqlite3.Error) as exc:
                transient_lock = (isinstance(exc, sqlite3.OperationalError)
                                  and getattr(exc, "sqlite_errorcode", 0) & 255
                                  in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
                if not transient_lock:
                    self.available = False
                logger.error("Proxy budget accounting failed: %s; source I/O blocked", type(exc).__name__)
                raise BudgetPause("ProxyBudgetUnavailable") from None

    def _admission_delay(self, kind, now, row, credit, rate, *, demand):
        stamp = now.timestamp()
        self.waiting = {key: seen for key, seen in self.waiting.items()
                        if 0 <= stamp - seen < DEMAND_TTL}
        first = next(iter(self.waiting), kind)
        cost = row[4 if kind == "catalog" else 5]
        delay = max(0, (cost - credit) / max(1, rate))
        if first != kind:
            # Let an older, more expensive lane accumulate its estimate instead
            # of letting a stream of cheap requests spend every replenished byte.
            first_cost = row[4 if first == "catalog" else 5]
            if credit < first_cost + cost:
                delay = max(1, (first_cost + cost - credit) / max(1, rate))
        if demand and (delay > 0 or kind in self.waiting):
            # Updating a retry's heartbeat must not move it behind newer work.
            # An affordable new preflight creates no lease: it may complete
            # through the direct API without ever requesting a browser ticket.
            self.waiting[kind] = stamp
        return delay

    def status(self, kind="catalog", *, demand=False):
        if kind not in {"catalog", "details"}:
            raise ValueError("Unknown request kind")
        base = {"limit_bytes": self.limit, "stop_bytes": self.stop,
                "reserve_bytes": self.limit - self.stop, "start_month": self.start_month}
        try:
            with self._state() as (db, now, row, credit, rate, end):
                daily = daily_budget.state(db, now, row[0], row[1], self.stop)
                cost = row[4 if kind == "catalog" else 5]
                code, delay = "ready", 0
                if self.stop - row[1] < QUANTUM:
                    code, delay = "ProxyBudgetExceeded", min(86400, end - now.timestamp())
                elif daily['daily_remaining_bytes'] < QUANTUM:
                    code, delay = 'ProxyBudgetWait', daily['daily_reset_seconds']
                else:
                    delay = self._admission_delay(kind, now, row, credit, rate, demand=demand)
                    if delay > 0:
                        code = "ProxyBudgetWait"
                return {**base, **daily, "month": row[0], "charged_bytes": row[1], "credit_bytes": round(credit),
                        'pause_reason': 'daily_limit' if daily['daily_remaining_bytes'] < QUANTUM else None,
                        "bytes_per_second": round(rate, 2), "estimated_request_bytes": round(cost),
                        "admitted_requests": row[6], "code": code,
                        "retry_after": max(1, min(86400, math.ceil(delay))) if code != "ready" else 0}
        except BudgetPause as exc:
            return {**base, "code": exc.code, "retry_after": exc.retry_after, "charged_bytes": None}

    def admit(self, kind):
        with self.lock:
            return self._admit(kind)

    def report(self):
        """Local-only audit, including stopped/pre-release months; never source I/O."""
        with self.lock:
            try:
                if not self.available or not self.path.exists():
                    raise ValueError('Budget unavailable')
                with self._transaction() as db:
                    now = self._now()
                    month = now.strftime('%Y-%m')
                    rows = db.execute('SELECT month,charged FROM months ORDER BY month').fetchall()
                    if rows and rows[-1][0] > month:
                        raise ValueError('Clock rollback')
                    charged = next((value for name, value in rows if name == month), 0)
                    daily = daily_budget.state(db, now, month, charged, self.stop)
                    elapsed = max(1, now.timestamp() - datetime(now.year, now.month, now.day, tzinfo=self.tz).timestamp())
                    duration = elapsed + daily['daily_reset_seconds']
                    forecast = round(daily['daily_charged_bytes'] / elapsed * duration * daily['days_in_month'])
                    return {**daily, 'month': month, 'start_month': self.start_month,
                            'code': 'ready' if month >= self.start_month else 'ProxyBudgetNotStarted',
                            'monthly_charged_bytes': charged, 'monthly_limit_bytes': self.limit,
                            'monthly_stop_bytes': self.stop, 'monthly_remaining_bytes': max(0,self.stop-charged),
                            'reserve_bytes': self.limit-self.stop,
                            'today_pace_monthly_projection_bytes': forecast,
                            'projection_is_estimate': True, 'provider_usage_verified': False,
                            'months': [{'month': name, 'charged_bytes': value} for name,value in rows]}
            except (OSError, ValueError, sqlite3.Error):
                return {'code': 'ProxyBudgetUnavailable', 'provider_usage_verified': False}

    def _admit(self, kind):
        if kind not in {"catalog", "details"}:
            raise ValueError("Unknown request kind")
        with self._state() as (db, now, row, credit, rate, end):
            if self.stop - row[1] < QUANTUM:
                raise BudgetPause("ProxyBudgetExceeded", end - now.timestamp())
            daily = daily_budget.state(db, now, row[0], row[1], self.stop)
            if daily['daily_remaining_bytes'] < QUANTUM:
                raise BudgetPause('ProxyBudgetWait', daily['daily_reset_seconds'])
            cost = row[4 if kind == "catalog" else 5]
            delay = self._admission_delay(kind, now, row, credit, rate, demand=True)
            if delay > 0:
                raise BudgetPause("ProxyBudgetWait", delay)
            db.execute("UPDATE months SET credit=?, updated=?, admitted=admitted+1 WHERE month=?",
                       (credit - cost, now.timestamp(), row[0]))
            month = row[0]
        self.sequence += 1
        ticket = self.sequence
        self.waiting.pop(kind, None)
        self.held[ticket] = Admission(month, kind, cost)
        return ticket

    def finish(self, ticket):
        # Refund the admission estimate; byte reservations remain irrevocable.
        with self.lock:
            entry = self.held.pop(ticket, None)
            if entry is None:
                return
            month, kind = entry.month, entry.kind
            try:
                with self._state() as (db, now, row, credit, _, _):
                    if month != row[0]:
                        return
                    observed = max(QUANTUM, min(BURST, entry.observed))
                    # Each byte belongs to one shared batch allocation, rather
                    # than being relearned by every overlapping request window.
                    previous = row[4 if kind == "catalog" else 5]
                    learned = max(QUANTUM, min(BURST, previous * 0.75 + observed * 0.25))
                    column = "catalog_cost" if kind == "catalog" else "details_cost"
                    db.execute(f"UPDATE months SET credit=?, updated=?, {column}=? WHERE month=?",
                               (min(BURST, credit + entry.remaining), now.timestamp(), learned, month))
            except BudgetPause:
                logger.error("Proxy budget admission could not be settled; allowance remains reserved")

    def reserve(self, amount, *, month=None, day=None):
        with self.lock:
            return self._reserve(amount, month=month, day=day)

    def _reserve(self, amount, *, month=None, day=None):
        if type(amount) is not int or amount <= 0:
            raise ValueError("Invalid byte reservation")
        with self._state() as (db, now, row, credit, _, end):
            if month is not None and row[0] != month:
                raise BudgetPause("ProxyBudgetWait", 1)  # Close old-month tunnels.
            if day is not None and now.date().isoformat() != day:
                raise BudgetPause('ProxyBudgetWait', 1)
            if row[1] + amount > self.stop:
                raise BudgetPause("ProxyBudgetExceeded", end - now.timestamp())
            daily = daily_budget.state(db, now, row[0], row[1], self.stop)
            if amount > daily['daily_remaining_bytes']:
                raise BudgetPause('ProxyBudgetWait', daily['daily_reset_seconds'])
            charged = row[1] + amount
            entries = [entry for entry in self.held.values() if entry.month == row[0]]
            share = amount / len(entries) if entries else 0
            released = sum(min(entry.remaining, share) for entry in entries)
            warned = row[7] or charged >= min(30_000_000_000, self.stop * 5 // 6)
            db.execute("UPDATE months SET charged=?, credit=?, updated=?, warned=? WHERE month=?",
                       (charged, credit - amount + released, now.timestamp(), int(warned), row[0]))
            daily_budget.charge(db, daily['day'], amount)
            if warned and not row[7]:
                logger.warning("Proxy allowance warning month=%s reserved_bytes=%s stop_bytes=%s", row[0], charged, self.stop)
            month = row[0]
        # Only consume admission holds after the durable byte transaction commits.
        # reserve() retains the outer reentrant lock through these memory updates.
        for entry in entries:
            entry.remaining -= min(entry.remaining, share)
            entry.observed += share
        return month

    def stream(self):
        # The same prepaid block covers negotiation and the opaque target TLS.
        day = self._now().date().isoformat()
        month = self.reserve(QUANTUM, day=day)
        return BudgetStream(self, month, day)


class BudgetStream:
    def __init__(self, budget, month, day):
        self.budget, self.month, self.credit = budget, month, QUANTUM
        self.day = day
        self.reserved_bytes, self.consumed_bytes = QUANTUM, 0

    def allowance(self, maximum=65536):
        if self.budget._now().date().isoformat() != self.day:
            raise BudgetPause("ProxyBudgetWait", 1)
        if self.credit == 0:
            self.budget.reserve(QUANTUM, month=self.month, day=self.day)
            self.credit = QUANTUM
            self.reserved_bytes += QUANTUM
        return min(maximum, self.credit)

    def consumed(self, amount):
        if type(amount) is not int or not 0 <= amount <= self.credit:
            raise ValueError("Unreserved proxy bytes")
        self.credit -= amount
        self.consumed_bytes += amount

    def tls_overhead(self):
        # Outer HTTPS-proxy TLS negotiation is performed by the SSL library.
        self.budget.reserve(16384, month=self.month, day=self.day)
        self.reserved_bytes += 16384


def configured_budget():
    path = os.environ.get("VINTED_PROXY_BUDGET_PATH", "/var/lib/vinted-budget/budget.sqlite3")
    return ProxyBudget(path, start_month=os.environ.get("VINTED_PROXY_BUDGET_START", "2026-11"))
