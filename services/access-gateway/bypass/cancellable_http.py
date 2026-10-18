"""Interrupt the internal Byparr HTTP connection when its gRPC caller leaves.

Only the owning thread closes the connection. The cancellation callback uses
socket.shutdown, which also wakes a thread blocked waiting for HTTP headers/body.
No proxy configuration or source cookies are involved in this internal transport.
"""

import http.client
import json
import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit


class RequestCancelled(OSError):
    pass


@dataclass
class HTTPReply:
    status_code: int
    headers: object
    body: bytes

    def json(self):
        return json.loads(self.body)


class RequestCancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.socket = None

    def check(self):
        if self.event.is_set():
            raise RequestCancelled("Caller cancelled the browser request")

    def cancel(self):
        self.event.set()
        with self.lock:
            connection = self.socket
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # The owner may have finished and closed it concurrently.

    def bind(self, connection):
        with self.lock:
            self.socket = connection
        if self.event.is_set():
            self.cancel()

    def post(self, url, *, payload, headers, timeout):
        self.check()
        parts = urlsplit(url)
        factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = factory(parts.hostname, parts.port, timeout=timeout)
        try:
            connection.connect()
            self.bind(connection.sock)
            self.check()
            connection.request("POST", parts.path or "/", body=json.dumps(payload).encode("utf-8"),
                               headers={"Content-Type": "application/json", **headers})
            with connection.getresponse() as response:
                body = response.read(32 * 1024 * 1024 + 1)
                self.check()
                if len(body) > 32 * 1024 * 1024:
                    raise ValueError("Byparr response exceeds 32 MiB")
                return HTTPReply(response.status, response.headers, body)
        finally:
            self.bind(None)
            connection.close()
