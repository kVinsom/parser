import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from budget_guard import BudgetGuard
from proxy_budget import BURST, DEMAND_TTL, QUANTUM, BudgetPause, ProxyBudget
from source_proxy import open_tunnel


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "budget.sqlite3"
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.budget = self.make()

    def tearDown(self):
        self.temp.cleanup()

    def make(self, **options):
        return ProxyBudget(self.path, clock=lambda: self.now, tz=timezone.utc, **options)

    def active(self):
        self.now = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def test_prepaid_month_stays_closed_and_does_not_change_personal_control(self):
        self.assertEqual(self.budget.status()["code"], "ProxyBudgetNotStarted")
        with self.assertRaises(BudgetPause):
            self.budget.stream()
        with self.assertRaises(BudgetPause):
            self.budget.admit("details")
        self.active()
        state = self.budget.status()
        self.assertEqual(state["charged_bytes"], 0)
        self.assertEqual(state["limit_bytes"], 40_000_000_000)
        self.assertEqual(state["stop_bytes"], 36_000_000_000)

    def test_count_is_reserved_before_io_and_restored_across_instances(self):
        self.active()
        stream = self.budget.stream()
        self.assertEqual(self.budget.status()["charged_bytes"], QUANTUM)
        self.assertEqual(stream.allowance(), QUANTUM)
        stream.consumed(123)
        self.assertGreaterEqual(self.budget.status()["charged_bytes"], 123)
        restored = self.make()
        self.assertEqual(restored.status()["charged_bytes"], QUANTUM)
        self.assertEqual(stream.allowance(100), 100)
        self.assertEqual(restored.status()["charged_bytes"], QUANTUM)

    def test_overlapping_work_does_not_teach_each_request_the_entire_batch_cost(self):
        self.active()
        first, second = self.budget.admit("catalog"), self.budget.admit("catalog")
        self.budget.reserve(409600)
        self.budget.finish(first)
        self.budget.finish(second)
        state = self.budget.status()
        self.assertEqual(state["charged_bytes"], 409600)
        self.assertEqual(state["estimated_request_bytes"], 163328)

    def test_transferred_bytes_replace_the_hold_without_spending_it_twice(self):
        self.active()
        ticket = self.budget.admit("details")
        initial = self.budget.status()["credit_bytes"]
        self.budget.reserve(262144)
        self.assertEqual(self.budget.status()["credit_bytes"], initial)
        self.budget.reserve(262144)
        self.assertEqual(self.budget.status()["credit_bytes"], initial)
        self.budget.reserve(QUANTUM)
        self.assertEqual(self.budget.status()["credit_bytes"], initial - QUANTUM)
        self.budget.finish(ticket)
        self.assertEqual(self.budget.status()["charged_bytes"], 524288 + QUANTUM)
        self.assertEqual(self.budget.status()["credit_bytes"], BURST - 524288 - QUANTUM)

    def test_failed_or_cancelled_work_refunds_only_the_unspent_hold_once(self):
        self.active()
        ticket = self.budget.admit("details")
        self.budget.reserve(QUANTUM)
        self.budget.finish(ticket)
        self.budget.finish(ticket)
        self.assertEqual(self.budget.status()["credit_bytes"], BURST - QUANTUM)
        self.assertEqual(self.make().status()["charged_bytes"], QUANTUM)

    def test_changing_overlap_attributes_each_byte_once_across_the_batch(self):
        self.active()
        long = self.budget.admit("details")
        self.budget.reserve(131072)
        first = self.budget.admit("catalog")
        self.budget.reserve(262144)
        first_observed = self.budget.held[first].observed
        self.budget.finish(first)
        second = self.budget.admit("catalog")
        self.budget.reserve(131072)
        total = first_observed + sum(entry.observed for entry in self.budget.held.values())
        self.assertEqual(total, 524288)
        self.assertEqual(self.budget.held[long].observed, 327680)
        self.budget.finish(second)
        self.budget.finish(long)
        self.assertEqual(self.budget.status()["credit_bytes"], BURST - 524288)
        self.assertEqual(self.budget.status()["charged_bytes"], 524288)

    def test_restart_never_refunds_unsettled_reservations(self):
        self.active()
        self.budget.admit("details")
        self.budget.reserve(QUANTUM)
        restored = self.make()
        self.assertEqual(restored.status()["credit_bytes"], BURST - 524288)
        self.assertEqual(restored.status()["charged_bytes"], QUANTUM)

    def test_expensive_lane_gets_a_turn_despite_repeated_cheap_requests(self):
        self.active()
        self.budget.reserve(BURST - 200000)
        self.assertEqual(self.budget.status("details", demand=True)["code"], "ProxyBudgetWait")
        for _ in range(40):
            self.assertEqual(self.budget.status("catalog", demand=True)["code"], "ProxyBudgetWait")
            with self.assertRaises(BudgetPause):
                self.budget.admit("catalog")
            self.now += timedelta(seconds=1)
            ready = self.budget.status("details", demand=True)["code"]
            if ready == "ready":
                break
        else:
            self.fail("The expensive lane never accumulated its allowance")
        ticket = self.budget.admit("details")
        self.assertNotIn("details", self.budget.waiting)
        self.assertEqual(next(iter(self.budget.waiting)), "catalog")
        self.budget.reserve(524288)
        self.budget.finish(ticket)
        for _ in range(12):
            self.now += timedelta(seconds=1)
            if self.budget.status("catalog", demand=True)["code"] == "ready":
                self.budget.admit("catalog")
                break
        else:
            self.fail("The catalog lane never received the following turn")

    def test_no_demand_from_health_reads_and_abandoned_demand_expires(self):
        self.active()
        self.budget.reserve(BURST - 200000)
        self.budget.status("details")
        self.assertFalse(self.budget.waiting)
        self.assertEqual(self.budget.status("catalog")["code"], "ready")
        self.budget.status("details", demand=True)
        self.assertEqual(self.budget.status("catalog")["code"], "ProxyBudgetWait")
        self.now += timedelta(seconds=DEMAND_TTL)
        self.assertEqual(self.budget.status("catalog")["code"], "ready")
        self.assertFalse(self.budget.waiting)

    def test_ready_competing_requests_still_run_concurrently_when_both_fit(self):
        self.active()
        self.budget.status("details", demand=True)
        self.assertFalse(self.budget.waiting)
        catalog = self.budget.admit("catalog")
        details = self.budget.admit("details")
        self.assertEqual(len(self.budget.held), 2)
        self.budget.reserve(2 * QUANTUM)
        self.budget.finish(catalog)
        self.budget.finish(details)
        self.assertEqual(self.budget.status()["credit_bytes"], BURST - 2 * QUANTUM)

    def test_failed_commit_cannot_release_an_unpaid_hold(self):
        from contextlib import contextmanager
        self.active()
        ticket = self.budget.admit("catalog")
        original = self.budget._transaction

        @contextmanager
        def failing():
            with original() as db:
                yield db
                raise sqlite3.OperationalError("commit failed")

        with patch.object(self.budget, "_transaction", failing), self.assertRaises(BudgetPause):
            self.budget.reserve(QUANTUM)
        self.assertEqual(self.budget.held[ticket].observed, 0)
        self.assertEqual(self.budget.held[ticket].remaining, 131072)
        restored = self.make()
        self.assertEqual(restored.status()["charged_bytes"], 0)
        self.assertEqual(restored.status()["credit_bytes"], BURST - 131072)

    def test_protocol_negotiation_uses_the_same_prepaid_bytes_for_http_and_socks(self):
        self.active()
        for scheme, reply in (
            ("http", b"HTTP/1.1 200 OK\r\nX-Padding: " + b"x" * 9000 + b"\r\n\r\n"),
            ("socks5", b"\x05\x02\x01\x00\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb"),
        ):
            with self.subTest(scheme=scheme):
                incoming = io.BytesIO(reply)
                connection = Mock()
                connection.recv.side_effect = incoming.read
                stream = self.budget.stream()
                with patch("source_proxy.socket.create_connection", return_value=connection):
                    self.assertIs(open_tunnel("www.vinted.de", 443, {
                        "server": scheme + "://localhost:10000", "username": "test", "password": "test"
                    }, stream), connection)
                sent = sum(len(call.args[0]) for call in connection.sendall.call_args_list)
                self.assertEqual(stream.consumed_bytes, sent + len(reply))
                self.assertGreaterEqual(stream.reserved_bytes, stream.consumed_bytes)
                self.assertLess(stream.reserved_bytes - stream.consumed_bytes, QUANTUM)

    def test_large_negotiation_cannot_read_past_shared_quota(self):
        self.active()
        self.budget.status()
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE months SET charged=?', (self.budget.stop - QUANTUM,))
            db.execute('INSERT INTO daily_baseline VALUES (?,?)', ('2026-11', self.budget.stop - QUANTUM))
        stream = self.budget.stream()
        reply = io.BytesIO(b"HTTP/1.1 200 OK\r\nX-Padding: " + b"x" * 9000 + b"\r\n\r\n")
        connection = Mock()
        connection.recv.side_effect = reply.read
        with patch("source_proxy.socket.create_connection", return_value=connection), self.assertRaises(BudgetPause):
            open_tunnel("www.vinted.fr", 443, {"server": "http://localhost:10000"}, stream)
        connection.close.assert_called_once()
        self.assertEqual(self.budget.status()["charged_bytes"], self.budget.stop)
        self.assertLess(stream.consumed_bytes, len(reply.getvalue()))

    def test_concurrent_clients_cannot_overspend_shared_month(self):
        self.active()
        self.budget.status()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE months SET charged=?", (self.budget.stop - 4 * QUANTUM,))
            db.execute('INSERT INTO daily_baseline VALUES (?,?)', ('2026-11', self.budget.stop - 4 * QUANTUM))
        db.close()
        peer = self.make()

        def spend(index):
            try:
                (self.budget if index % 2 else peer).reserve(QUANTUM)
                return True
            except BudgetPause:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(spend, range(16))), 4)
        self.assertEqual(peer.status()["charged_bytes"], self.budget.stop)
        self.assertEqual(peer.status()["code"], "ProxyBudgetExceeded")
        self.assertEqual(self.make().status()["code"], "ProxyBudgetExceeded")

    def test_pacing_wait_refunds_estimate_but_never_actual_bytes(self):
        self.active()
        ticket = self.budget.admit("details")
        self.budget.reserve(4 * 1024 * 1024)
        self.assertEqual(self.budget.status("details")["code"], "ProxyBudgetWait")
        self.budget.finish(ticket)
        self.budget.finish(ticket)
        self.assertEqual(self.budget.status()["charged_bytes"], 4 * 1024 * 1024)
        self.now += timedelta(minutes=10)
        self.assertEqual(self.budget.status()["code"], "ready")

    def test_missing_corrupt_and_reconfigured_storage_fail_closed(self):
        self.active()
        self.budget.reserve(QUANTUM)
        with self.assertRaises(BudgetPause):
            self.make(stop_bytes=35_000_000_000).stream()
        self.path.unlink()
        self.assertEqual(self.budget.status()["code"], "ProxyBudgetUnavailable")
        self.assertFalse(self.make().available)
        self.path.write_bytes(b"corrupt")
        self.assertEqual(self.make().status()["code"], "ProxyBudgetUnavailable")

    def test_calendar_rollover_closes_old_tunnel_and_clock_rollback_cannot_reset(self):
        self.active()
        stream = self.budget.stream()
        stream.allowance()
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        with self.assertRaises(BudgetPause):
            stream.allowance()
        self.assertEqual(self.budget.status()["charged_bytes"], 0)
        self.budget.reserve(QUANTUM)
        self.now = datetime(2026, 11, 30, tzinfo=timezone.utc)
        self.assertEqual(self.budget.status()["code"], "ProxyBudgetUnavailable")
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.assertEqual(self.budget.status()["charged_bytes"], QUANTUM)

    def test_stored_future_timestamp_blocks_instead_of_minting_credit(self):
        self.active()
        self.now += timedelta(days=1)
        self.budget.reserve(QUANTUM)
        self.now -= timedelta(seconds=10)
        self.assertEqual(self.budget.status()["code"], "ProxyBudgetUnavailable")

    def test_empty_ledger_during_active_month_cannot_reset_allowance(self):
        self.active()
        self.path.unlink()
        self.path.touch()
        self.assertEqual(self.make().status()["code"], "ProxyBudgetUnavailable")

    def test_pause_ends_at_start_of_month_without_extra_day(self):
        self.now = datetime(2026, 10, 31, 23, 59, 10, tzinfo=timezone.utc)
        self.assertEqual(self.budget.status()["retry_after"], 50)
        self.now += timedelta(seconds=50)
        self.assertEqual(self.budget.status()["code"], "ready")

    def test_temporary_sqlite_lock_blocks_request_then_recovers(self):
        self.active()
        self.budget.reserve(QUANTUM)
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with patch.object(self.budget, "_transaction", side_effect=error):
            self.assertEqual(self.budget.status()["code"], "ProxyBudgetUnavailable")
        self.assertTrue(self.budget.available)
        self.assertEqual(self.budget.status()["charged_bytes"], QUANTUM)
        self.assertEqual(self.budget.status()["code"], "ready")


class GuardTests(unittest.TestCase):
    def test_preflight_never_uses_proxy_or_follows_redirects_and_preserves_pause(self):
        target = "http://byparr:8191/proxy-budget?kind=details&demand=true"
        for code in ("ready", "ProxyBudgetWait", "ProxyBudgetNotStarted", "ProxyBudgetExceeded", "ProxyBudgetUnavailable"):
            with self.subTest(code=code), patch("budget_guard.urllib.request.build_opener") as build:
                response = Mock(status=200)
                response.geturl.return_value = target
                response.read.return_value = json.dumps({"code": code, "retry_after": 77}).encode()
                build.return_value.open.return_value.__enter__.return_value = response
                guard = BudgetGuard("http://byparr:8191")
                if code == "ready":
                    guard.check("details")
                else:
                    with self.assertRaises(BudgetPause) as error:
                        guard.check("details")
                    self.assertEqual((error.exception.code, error.exception.retry_after), (code, 77))
                self.assertEqual(build.call_args.args[0].proxies, {})
                self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, "", {}, "http://other/"))

    def test_unreachable_internal_budget_never_authorizes_source_io(self):
        with patch("budget_guard.urllib.request.build_opener") as build:
            build.return_value.open.side_effect = OSError("unavailable")
            with self.assertRaises(BudgetPause) as error:
                BudgetGuard("http://byparr:8191").check("catalog")
            self.assertEqual(error.exception.code, "ProxyBudgetUnavailable")
