import base64
import socket
import socketserver
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from source_proxy import SourceProxy, allowed_target


def headers(connection):
    data = b""
    while not data.endswith(b"\r\n\r\n"):
        part = connection.recv(1)
        if not part:
            break
        data += part
    return data


class SourceProxyTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Upstream(socketserver.StreamRequestHandler):
            def handle(self):
                data = b""
                while not data.endswith(b"\r\n\r\n"):
                    line = self.rfile.readline()
                    if not line:
                        return
                    data += line
                requests.append(data)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if self.rfile.read(11) == b"bot-request":
                    self.wfile.write(b"bot-response")

        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.upstream_thread.start()
        self.policy = SourceProxy()
        self.original = {
            "server": f"http://127.0.0.1:{self.upstream.server_address[1]}",
            "username": "upstream-user",
            "password": "upstream-password",
        }
        self.token, self.proxy = self.policy.register(self.original)

    def tearDown(self):
        self.policy.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=1)

    def connect(self, target, *, authorized=True, method="CONNECT"):
        connection = socket.create_connection(
            ("127.0.0.1", self.policy.server.server_port), timeout=2
        )
        auth = ""
        if authorized:
            value = base64.b64encode(("vpars:" + self.token).encode()).decode()
            auth = "Proxy-Authorization: Basic " + value + "\r\n"
        connection.sendall(
            (
                f"{method} {target} HTTP/1.1\r\nHost: {target}\r\n" + auth + "\r\n"
            ).encode()
        )
        return connection, headers(connection)

    def test_only_source_and_required_asset_domains_are_allowed(self):
        for host in (
            "www.vinted.fr",
            "www.vinted.it",
            "www.vinted.de",
            "www.vinted.co.uk",
            "geo.captcha-delivery.com",
            "cdn.cookielaw.org",
        ):
            self.assertTrue(allowed_target(host, 443))
            self.assertFalse(allowed_target(host, 444))
        for host in (
            "www.vinted.fr.example",
            "vinted.fr.example",
            "examplevinted.fr",
            "metrics.vinted.lt",
            "images1.vinted.net",
            "images2.vinted.net",
            "images.vinted.net",
            "marketplace-web-assets.vinted.com",
            "edgedl.me.gvt1.com",
            "firefox-settings-attachments.cdn.mozilla.net",
            "firefox.settings.services.mozilla.com",
            "incoming.telemetry.mozilla.org",
            "www.google.com",
            "127.0.0.1",
        ):
            self.assertFalse(allowed_target(host, 443))

    def test_background_and_unknown_requests_never_reach_upstream(self):
        for host in (
            "marketplace-web-assets.vinted.com",
            "edgedl.me.gvt1.com",
            "firefox-settings-attachments.cdn.mozilla.net",
            "firefox.settings.services.mozilla.com",
            "incoming.telemetry.mozilla.org",
            "outside.example",
            "metrics.vinted.lt",
            "images1.vinted.net",
            "images2.vinted.net",
            "www.vinted.fr.example",
        ):
            connection, response = self.connect(host + ":443")
            with connection:
                self.assertIn(b" 403 ", response)
        connection, response = self.connect("http://www.vinted.fr/", method="GET")
        with connection:
            self.assertIn(b" 403 ", response)
        self.assertEqual(self.requests, [])

    def test_tunnel_is_opaque_and_upstream_receives_its_own_credentials(self):
        connection, response = self.connect("www.vinted.fr:443")
        with connection:
            self.assertIn(b" 200 ", response)
            connection.sendall(b"bot-request")
            self.assertEqual(connection.recv(12), b"bot-response")
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(
            self.requests[0].startswith(b"CONNECT www.vinted.fr:443 HTTP/1.1")
        )
        expected = base64.b64encode(b"upstream-user:upstream-password")
        self.assertIn(b"Proxy-Authorization: Basic " + expected, self.requests[0])
        self.assertNotIn(self.token.encode(), self.requests[0])

    def test_missing_and_expired_local_authorization_fail_closed(self):
        connection, response = self.connect("www.vinted.it:443", authorized=False)
        with connection:
            self.assertIn(b" 407 ", response)
        self.policy.unregister(self.token)
        connection, response = self.connect("www.vinted.de:443")
        with connection:
            self.assertIn(b" 407 ", response)
        self.assertEqual(self.requests, [])

    def test_budget_pause_blocks_connect_before_opening_paid_upstream(self):
        from proxy_budget import ProxyBudget

        with tempfile.TemporaryDirectory() as folder:
            budget = ProxyBudget(Path(folder) / "budget.sqlite3",
                clock=lambda: datetime(2026, 10, 15, tzinfo=timezone.utc), tz=timezone.utc)
            self.policy.budget = budget
            for host in ("www.vinted.fr", "www.vinted.de", "www.vinted.it", "www.vinted.co.uk"):
                connection, response = self.connect(host + ":443")
                with connection:
                    self.assertIn(b" 503 ", response)
            self.assertEqual(self.requests, [])

    def test_geo_resolution_keeps_the_original_proxy_and_direct_mode(self):
        self.assertEqual(self.policy.original_proxy(self.proxy), self.original)
        token, proxy = self.policy.register(None)
        self.assertIsNone(self.policy.original_proxy(proxy))
        self.policy.unregister(token)
        with self.assertRaises(ValueError):
            self.policy.original_proxy(proxy)

    def test_active_month_counts_both_directions_for_two_profiles_and_stops_at_limit(self):
        from proxy_budget import QUANTUM, ProxyBudget

        with tempfile.TemporaryDirectory() as folder:
            current = [datetime(2026, 10, 15, tzinfo=timezone.utc)]
            budget = ProxyBudget(Path(folder) / "budget.sqlite3", clock=lambda: current[0], tz=timezone.utc)
            current[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
            self.policy.budget = budget
            first = self.token
            second, _ = self.policy.register({**self.original, "username": "second-profile"})
            for token, host in ((first, "www.vinted.fr"), (second, "www.vinted.de")):
                self.token = token
                connection, response = self.connect(host + ":443")
                with connection:
                    self.assertIn(b" 200 ", response)
                    connection.sendall(b"bot-request")
                    self.assertEqual(connection.recv(12), b"bot-response")
            self.assertEqual(budget.status()["charged_bytes"], 2 * QUANTUM)
            self.assertEqual(len(self.requests), 2)
            state = budget.status()
            import sqlite3
            with sqlite3.connect(budget.path) as db:
                db.execute('INSERT INTO daily_baseline VALUES (?,?)', ('2026-11', budget.stop - state['charged_bytes']))
                db.execute('UPDATE months SET charged=?', (budget.stop,))
            connection, response = self.connect("www.vinted.it:443")
            with connection:
                self.assertIn(b" 503 ", response)
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(budget.status()["charged_bytes"], budget.stop)


if __name__ == "__main__":
    unittest.main()
