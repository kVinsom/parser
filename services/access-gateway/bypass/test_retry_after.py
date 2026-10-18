import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from byparr_runtime import REQUEST_RETRY_AFTER, install_navigation_timing
from vinted import retry_after_seconds


class RetryAfterTests(unittest.IsolatedAsyncioTestCase):
    def test_seconds_dates_and_invalid_values(self):
        now = datetime(2026, 9, 28, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("900"), 900)
        self.assertEqual(retry_after_seconds("Mon, 28 Sep 2026 00:02:00 GMT", now), 120)
        self.assertIsNone(retry_after_seconds("invalid"))
        self.assertIsNone(retry_after_seconds(None))

    async def test_upstream_retry_header_collected_without_timing_mode(self):
        response = SimpleNamespace(status=429, header_value=AsyncMock(return_value="900"))
        endpoints = SimpleNamespace(_navigate_and_solve=AsyncMock(return_value=(False, "", response)),
                                    remaining_ms=lambda timer: 10000)
        install_navigation_timing(endpoints)
        retry = {}
        token = REQUEST_RETRY_AFTER.set(retry)
        try:
            await endpoints._navigate_and_solve(
                SimpleNamespace(), SimpleNamespace(url="https://www.vinted.fr/catalog"), None
            )
            self.assertEqual(retry, {"seconds": 900})
        finally:
            REQUEST_RETRY_AFTER.reset(token)
