"""Loopback CONNECT gate: only source resources can reach the upstream proxy.

HTTPS remains opaque. This gate never terminates target TLS or changes its
certificate checks. No listening port is exposed outside this container.
"""

import base64
import logging
import re
import secrets
import select
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ALLOWED_SUFFIXES = (
    "vinted.fr",
    "vinted.it",
    "vinted.de",
    "vinted.co.uk",
    "vinted.com",
    "vinted.net",
    "vinted.lt",
    "datadome.co",
    "captcha-delivery.com",
    "cookielaw.org",
    "onetrust.com",
)
ALLOWED_HOSTS = frozenset({"challenges.cloudflare.com"})
DENIED_HOSTS = frozenset({"metrics.vinted.lt", "marketplace-web-assets.vinted.com"})
logger = logging.getLogger("uvicorn.error")


def photo_host(host):
    return bool(re.fullmatch(r"images[0-9]*\.vinted\.net", host.lower().rstrip(".")))


def allowed_target(host, port):
    host = host.lower().rstrip(".")
    return (
        port == 443
        and host not in DENIED_HOSTS
        and not photo_host(host)
        and (
            host in ALLOWED_HOSTS
            or any(
                host == suffix or host.endswith("." + suffix)
                for suffix in ALLOWED_SUFFIXES
            )
        )
    )


def read_exact(connection, size, meter=None):
    result = b""
    while len(result) < size:
        maximum = size - len(result)
        data = connection.recv(meter.allowance(maximum) if meter is not None else maximum)
        if not data:
            raise OSError("Proxy closed the connection")
        if meter is not None:
            meter.consumed(len(data))
        result += data
    return result


def send_reserved(connection, data, meter=None):
    while data:
        size = meter.allowance(len(data)) if meter is not None else len(data)
        if meter is not None:
            meter.consumed(size)  # A failed send retains its prepaid reservation.
        connection.sendall(data[:size])
        data = data[size:]


def open_tunnel(host, port, proxy, meter=None):
    if not proxy or not proxy.get("server") or proxy["server"] == "direct://":
        return socket.create_connection((host, port), timeout=8)
    address = urlsplit(proxy["server"])
    if (
        address.scheme not in {"http", "https", "socks5", "socks5h"}
        or not address.hostname
        or not address.port
    ):
        raise ValueError("Unsupported upstream proxy endpoint")
    connection = socket.create_connection((address.hostname, address.port), timeout=8)
    try:
        if address.scheme == "https":
            if meter is not None:
                meter.tls_overhead()
            connection = ssl.create_default_context().wrap_socket(
                connection, server_hostname=address.hostname
            )
        user = proxy.get("username") or ""
        password = proxy.get("password") or ""
        if address.scheme in {"http", "https"}:
            auth = ""
            if user or password:
                encoded = base64.b64encode((user + ":" + password).encode()).decode(
                    "ascii"
                )
                auth = "Proxy-Authorization: Basic " + encoded + "\r\n"
            send_reserved(connection,
                (
                    f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    + auth
                    + "\r\n"
                ).encode("ascii"), meter
            )
            header = bytearray()
            # Do not consume any bytes from the target's encrypted stream.
            while not header.endswith(b"\r\n\r\n"):
                header.extend(read_exact(connection, 1, meter))
                if len(header) > 16384:
                    raise OSError("Proxy response headers exceeded the limit")
            if bytes(header).split(b"\r\n", 1)[0].split(b" ")[1:2] != [b"200"]:
                raise OSError("Upstream proxy refused the tunnel")
        else:
            send_reserved(connection,
                b"\x05\x02\x00\x02" if user or password else b"\x05\x01\x00", meter
            )
            version, method = read_exact(connection, 2, meter)
            if version != 5 or method not in {0, 2}:
                raise OSError("SOCKS authentication unavailable")
            if method == 2:
                username, secret = user.encode(), password.encode()
                if not 1 <= len(username) <= 255 or len(secret) > 255:
                    raise ValueError("Invalid SOCKS credential length")
                send_reserved(connection,
                    b"\x01"
                    + bytes([len(username)])
                    + username
                    + bytes([len(secret)])
                    + secret, meter
                )
                if read_exact(connection, 2, meter) != b"\x01\x00":
                    raise OSError("SOCKS authentication refused")
            encoded_host = host.encode("idna")
            if not 1 <= len(encoded_host) <= 255:
                raise ValueError("Invalid target hostname")
            send_reserved(connection,
                b"\x05\x01\x00\x03"
                + bytes([len(encoded_host)])
                + encoded_host
                + port.to_bytes(2, "big"), meter
            )
            version, status, _, kind = read_exact(connection, 4, meter)
            if version != 5 or status != 0 or kind not in {1, 3, 4}:
                raise OSError("SOCKS target refused")
            length = (
                read_exact(connection, 1, meter)[0] if kind == 3 else 4 if kind == 1 else 16
            )
            read_exact(connection, length + 2, meter)
        return connection
    except BaseException:
        connection.close()
        raise


class SourceProxy:
    def __init__(self, budget=None):
        self.budget = budget
        self.lock = threading.Lock()
        self.configs = {}
        self.connections = set()
        self.blocked = {}
        self.closed = False
        self.slots = threading.BoundedSemaphore(64)
        policy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 10

            def log_message(self, *_):
                pass  # Never log proxy authorization headers or full URLs.

            def reject(self, status, authenticate=False):
                self.send_response(status)
                if authenticate:
                    self.send_header("Proxy-Authenticate", 'Basic realm="vpars-source"')
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def do_CONNECT(self):
                try:
                    target = urlsplit("//" + self.path)
                    host, port = target.hostname, target.port
                    if (
                        not host
                        or target.username
                        or target.password
                        or target.path
                        or target.query
                        or target.fragment
                    ):
                        raise ValueError
                    host = host.lower().rstrip(".")
                    if not allowed_target(host, port):
                        with policy.lock:
                            policy.blocked[host] = policy.blocked.get(host, 0) + 1
                        self.reject(403)
                        return
                    encoded = self.headers.get("Proxy-Authorization", "")
                    user, token = (
                        base64.b64decode(encoded.removeprefix("Basic "), validate=True)
                        .decode()
                        .split(":", 1)
                    )
                    with policy.lock:
                        known = user == "vpars" and token in policy.configs
                        upstream = policy.configs.get(token)
                    if not known:
                        self.reject(407, authenticate=True)
                        return
                except (ValueError, UnicodeError):
                    self.reject(407, authenticate=True)
                    return
                if policy.closed or not policy.slots.acquire(blocking=False):
                    self.reject(503)
                    return
                remote = None
                meter = None
                established = False
                try:
                    from proxy_budget import BudgetPause

                    paid = upstream and upstream.get("server") and upstream["server"] != "direct://"
                    meter = policy.budget.stream() if paid and policy.budget is not None else None
                    remote = open_tunnel(host, port, upstream, meter)
                    with policy.lock:
                        policy.connections.update((self.connection, remote))
                    self.send_response(200)
                    self.end_headers()
                    established = True
                    self.connection.settimeout(8)
                    remote.settimeout(8)
                    while not policy.closed:
                        buffered = (
                            isinstance(remote, ssl.SSLSocket) and remote.pending()
                        )
                        ready, _, _ = select.select(
                            [self.connection, remote], [], [], 0 if buffered else 1
                        )
                        if buffered and remote not in ready:
                            ready.append(remote)
                        for source in ready:
                            maximum = meter.allowance() if meter is not None else 65536
                            data = source.recv(maximum)
                            if not data:
                                return
                            if meter is not None:
                                meter.consumed(len(data))
                            (
                                remote if source is self.connection else self.connection
                            ).sendall(data)
                except BudgetPause:
                    if not established:
                        self.reject(503)
                except (OSError, ValueError):
                    if not established:
                        self.reject(502)
                finally:
                    self.close_connection = True
                    with policy.lock:
                        policy.connections.discard(self.connection)
                        policy.connections.discard(remote)
                    if remote is not None:
                        remote.close()
                    if meter is not None:
                        logger.info("Source proxy accounted host=%s application_bytes=%s reserved_bytes=%s",
                                    host, meter.consumed_bytes, meter.reserved_bytes)
                    policy.slots.release()

            def do_GET(self):
                self.reject(403)  # Source browsing is HTTPS; no plain-HTTP updates.

            do_POST = do_GET
            do_HEAD = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        self.thread.start()

    def register(self, upstream):
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.configs[token] = dict(upstream) if upstream else None
        return token, {
            "server": f"http://127.0.0.1:{self.server.server_port}",
            "username": "vpars",
            "password": token,
        }

    def unregister(self, token):
        with self.lock:
            self.configs.pop(token, None)

    def original_proxy(self, proxy):
        if (
            proxy
            and proxy.get("server") == f"http://127.0.0.1:{self.server.server_port}"
            and proxy.get("username") == "vpars"
        ):
            with self.lock:
                token = proxy.get("password")
                if token not in self.configs:
                    raise ValueError("Source proxy session expired")
                return self.configs[token]
        return proxy

    def close(self):
        self.closed = True
        with self.lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
