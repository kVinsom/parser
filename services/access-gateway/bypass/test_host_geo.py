import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple
from unittest.mock import Mock

from host_geo import HostGeoCache


class Geo(NamedTuple):
    timezone: str
    egress_ip: str | None = None


class HostGeoTests(unittest.TestCase):
    def test_auto_modes_reuse_only_host_data_until_expiry(self):
        now = [0]
        prepare = Mock(side_effect=[Geo("Europe/Berlin"), Geo("Europe/Paris")])
        cache = HostGeoCache(prepare, clock=lambda: now[0])
        for timezone in (None, "auto", "", " AUTO "):
            self.assertEqual(cache.resolve(timezone), Geo("Europe/Berlin"))
        prepare.assert_called_once_with(None, None)
        now[0] = 299.9
        self.assertEqual(cache.resolve("auto"), Geo("Europe/Berlin"))
        now[0] = 300
        self.assertEqual(cache.resolve("auto"), Geo("Europe/Paris"))
        self.assertEqual(prepare.call_args.args, ("auto", None))

    def test_explicit_timezone_and_failed_fallback_are_never_cached(self):
        prepare = Mock(side_effect=lambda timezone, proxy: Geo(timezone or ""))
        cache = HostGeoCache(prepare)
        for timezone in ("UTC", "Europe/Paris", "Europe/Berlin", "UTC", None, ""):
            self.assertEqual(cache.resolve(timezone), Geo(timezone or ""))
        self.assertEqual(prepare.call_count, 6)
        prepare.side_effect = OSError("lookup failed")
        for _ in range(2):
            with self.assertRaises(OSError):
                cache.resolve("auto")
        self.assertEqual(prepare.call_count, 8)

    def test_parallel_browser_launches_share_one_successful_host_lookup(self):
        entered, release = threading.Event(), threading.Event()

        def resolve(timezone, proxy):
            self.assertIsNone(proxy)
            entered.set()
            self.assertTrue(release.wait(2))
            return Geo("Europe/Berlin")

        prepare = Mock(side_effect=resolve)
        cache = HostGeoCache(prepare)
        with ThreadPoolExecutor(max_workers=4) as executor:
            first = executor.submit(cache.resolve, "auto")
            self.assertTrue(entered.wait(2))
            others = [executor.submit(cache.resolve, "auto") for _ in range(3)]
            release.set()
            self.assertEqual([future.result() for future in [first, *others]], [Geo("Europe/Berlin")] * 4)
        prepare.assert_called_once_with("auto", None)
