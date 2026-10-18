"""Real internal HTTP/gRPC cancellation, using loopback fixtures only."""

import http.client
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import grpc
from cancellable_http import RequestCancellation, RequestCancelled
from grpc_server import create_server
from vinted_rpc import access_pb2 as pb
from vinted_rpc import access_pb2_grpc as rpc


class CancellationTests(unittest.TestCase):
    def server(self, mode):
        started, disconnected = threading.Event(), threading.Event()
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(payload)
                if len(calls) == 1 and mode != "normal":
                    if mode == "body":
                        self.send_response(200)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        self.wfile.write(b"{")
                        self.wfile.flush()
                    started.set()
                    self.connection.settimeout(3)
                    if self.connection.recv(1) == b"":
                        disconnected.set()
                    return
                body = json.dumps({"status": "ok", "solution": {
                    "status": 200, "url": payload.get("url", "https://www.vinted.de/catalog"),
                    "response": '<title>Vinted</title><a href="/items/123-shirt">Shirt</a>',
                }}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", started, disconnected, calls

    def test_cancelled_before_connect_never_sends(self):
        cancellation = RequestCancellation()
        cancellation.cancel()
        with patch("cancellable_http.http.client.HTTPConnection") as connection:
            with self.assertRaises(RequestCancelled):
                cancellation.post("http://127.0.0.1:1/v1", payload={}, headers={}, timeout=2)
            connection.assert_not_called()

    def test_normal_response_does_not_use_environment_proxy(self):
        endpoint, _, _, calls = self.server("normal")
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            reply = RequestCancellation().post(endpoint + "/v1", payload={"example": True},
                                                headers={}, timeout=2)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.headers.get("content-type"), "application/json")
        self.assertEqual(reply.json()["status"], "ok")
        self.assertEqual(calls, [{"example": True}])

    def test_cancellation_interrupts_headers_and_body_without_waiting_for_timeout(self):
        for mode in ("headers", "body"):
            with self.subTest(mode=mode):
                endpoint, started, disconnected, _ = self.server(mode)
                cancellation = RequestCancellation()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(cancellation.post, endpoint + "/v1",
                                             payload={}, headers={}, timeout=30)
                    try:
                        self.assertTrue(started.wait(2))
                        cancellation.cancel()
                        with self.assertRaises((OSError, http.client.HTTPException)):
                            future.result(timeout=2)
                        self.assertTrue(disconnected.wait(2))
                    finally:
                        cancellation.cancel()

    def test_real_grpc_cancellation_closes_byparr_http_and_recovers_capacity(self):
        endpoint, started, disconnected, calls = self.server("headers")
        server, _, executor, port = create_server(endpoint=endpoint, listen="127.0.0.1:0")
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        try:
            grpc.channel_ready_future(channel).result(timeout=3)
            stub = rpc.VintedAccessStub(channel)
            call = stub.FetchCatalog.future(pb.FetchCatalogRequest(region="FR"), timeout=20)
            self.assertTrue(started.wait(2))
            call.cancel()
            self.assertTrue(disconnected.wait(2))
            until = time.monotonic() + 2
            while True:
                try:
                    result = stub.FetchCatalog(pb.FetchCatalogRequest(region="DE"), timeout=20)
                    break
                except grpc.RpcError as exc:
                    if exc.code() != grpc.StatusCode.RESOURCE_EXHAUSTED or time.monotonic() > until:
                        raise
                    time.sleep(0.01)
            self.assertEqual(result.outcome, pb.PAGE_LOADED)
            self.assertEqual(len(calls), 2)
        finally:
            channel.close()
            server.stop(0).wait()
            executor.shutdown(wait=True)
