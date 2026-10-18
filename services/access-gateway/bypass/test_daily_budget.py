import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from proxy_budget import ProxyBudget, BudgetPause, QUANTUM


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'budget.sqlite3'
        self.now = datetime(2026,10,16,tzinfo=timezone.utc)
        self.budget = self.make()

    def tearDown(self):
        self.temp.cleanup()

    def make(self):
        return ProxyBudget(self.path, clock=lambda:self.now, tz=ZoneInfo('Europe/Kyiv'))

    def active(self, year=2026, month=11, day=1):
        self.now = datetime(year,month,day,tzinfo=ZoneInfo('Europe/Kyiv'))

    def test_day_caps_for_all_calendar_lengths_and_leap_year(self):
        for year,month,days in ((2026,11,30),(2026,12,31),(2027,2,28),(2028,2,29)):
            self.active(year,month)
            state=self.budget.status()
            self.assertEqual(state['daily_limit_bytes'],36_000_000_000//days)
            self.assertLessEqual(state['daily_limit_bytes']*days,36_000_000_000)

    def test_daily_cutoff_is_atomic_across_instances_and_restores_next_day(self):
        self.active()
        cap=self.budget.status()['daily_limit_bytes']
        self.budget.reserve(cap-4*QUANTUM)
        peer=self.make()
        def spend(i):
            try:
                (peer if i%2 else self.budget).reserve(QUANTUM)
                return True
            except BudgetPause:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(spend,range(20))),4)
        self.assertEqual(peer.status()['pause_reason'],'daily_limit')
        for kind in ('catalog','details'):
            with self.assertRaises(BudgetPause): peer.admit(kind)
        self.assertEqual(self.make().report()['daily_charged_bytes'],cap)
        self.now += timedelta(days=1)
        self.budget.reserve(QUANTUM)
        report=self.budget.report()
        self.assertEqual(report['monthly_charged_bytes'],cap+QUANTUM)
        self.assertEqual(report['daily_charged_bytes'],QUANTUM)
        self.assertEqual(len(report['daily_history']),2)

    def test_full_month_never_exceeds_working_limit_then_new_month(self):
        self.active()
        for day in range(1,31):
            self.active(day=day)
            self.budget.reserve(1_200_000_000)
            with self.assertRaises(BudgetPause): self.budget.reserve(1)
        self.assertEqual(self.budget.status()['code'],'ProxyBudgetExceeded')
        self.assertEqual(self.budget.report()['monthly_charged_bytes'],36_000_000_000)
        self.active(month=12)
        self.budget.reserve(QUANTUM)
        self.assertEqual(self.budget.report()['monthly_charged_bytes'],QUANTUM)

    def test_midnight_kyiv_closes_prepaid_tunnel_and_clock_rollback_blocks(self):
        self.now=datetime(2026,11,1,21,59,59,tzinfo=timezone.utc)
        stream=self.budget.stream()
        self.assertEqual(self.budget.report()['day'],'2026-11-01')
        self.now+=timedelta(seconds=1)
        with self.assertRaises(BudgetPause): stream.allowance()
        self.budget.reserve(QUANTUM)
        self.assertEqual(self.budget.report()['day'],'2026-11-02')
        self.now-=timedelta(seconds=1)
        self.assertEqual(self.budget.status()['code'],'ProxyBudgetUnavailable')

    def test_dst_day_length_and_no_carryover_of_idle_days(self):
        self.active(2027,3,28)
        self.assertEqual(self.budget.status()['daily_reset_seconds'],23*3600)
        self.active(2027,10,31)
        self.assertEqual(self.budget.status()['daily_reset_seconds'],25*3600)
        self.assertEqual(self.budget.status()['daily_remaining_bytes'],36_000_000_000//31)

    def test_migration_preserves_original_month_without_fake_daily_history(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TABLE daily_usage')
            db.execute('DROP TABLE daily_baseline')
            db.execute('UPDATE config SET daily_version=0')
            db.execute('INSERT INTO months VALUES (?,?,?,?,?,?,?,?)',('2026-10',80805888,1234,self.now.timestamp(),174247,411781,353,0))
        restored=self.make()
        report=restored.report()
        self.assertEqual(report['monthly_charged_bytes'],80805888)
        self.assertEqual(report['historical_unallocated_bytes'],80805888)
        self.assertEqual(report['daily_history'],[])
        self.assertEqual(report['code'],'ProxyBudgetNotStarted')
        self.assertEqual(self.make().report(),report)
        self.assertEqual(restored.status()['code'],'ProxyBudgetNotStarted')

    def test_deleted_daily_table_or_row_cannot_reset_budget(self):
        self.active()
        self.budget.reserve(QUANTUM)
        with sqlite3.connect(self.path) as db: db.execute('DELETE FROM daily_usage')
        self.assertEqual(self.budget.status()['code'],'ProxyBudgetUnavailable')
        self.assertEqual(self.make().status()['code'],'ProxyBudgetUnavailable')
        with sqlite3.connect(self.path) as db: db.execute('DROP TABLE daily_usage')
        self.assertFalse(self.make().available)

    def test_report_while_stopped_does_not_spend_or_create_active_month(self):
        for _ in range(5):
            report=self.budget.report()
            self.assertEqual(report['months'],[])
            self.assertEqual(report['monthly_charged_bytes'],0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM months').fetchone()[0],0)

    def test_failed_transaction_rolls_back_daily_and_monthly_bytes_together(self):
        from unittest.mock import patch
        self.active()
        self.budget.status()
        with patch('daily_budget.charge',side_effect=sqlite3.OperationalError('disk error')):
            with self.assertRaises(BudgetPause): self.budget.reserve(QUANTUM)
        self.assertEqual(self.make().report()['monthly_charged_bytes'],0)
        self.assertEqual(self.make().report()['daily_charged_bytes'],0)
