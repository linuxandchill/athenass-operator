import http.server
import json
import queue
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from ass_node import cli


class FakeProcess:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self) -> None:
        self.events.append(f"stop:{self.name}")
        self.running = False

    def wait(self, timeout=None) -> int:
        self.events.append(f"wait:{self.name}")
        return 0

    def kill(self) -> None:
        self.events.append(f"kill:{self.name}")
        self.running = False


class FakeProxy:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def shutdown(self) -> None:
        self.events.append("stop:proxy")

    def server_close(self) -> None:
        self.events.append("close:proxy")


class FakeConnectivity:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.connected = False

    def start(self) -> None:
        self.events.append("start:ngrok")
        self.connected = True

    def stop(self) -> None:
        self.events.append("stop:ngrok")
        self.connected = False


class ServeConnectivityTests(unittest.TestCase):
    def _run_serve(
        self,
        config: dict,
        *,
        proxy_port: int = 8931,
    ) -> list[str]:
        events: list[str] = []
        inference = FakeProcess("inference", events)
        proxy = FakeProxy(events)
        connectivity = FakeConnectivity(events)

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            config_path = config_dir / "config.json"
            pid_path = config_dir / "serve.pid"
            config_path.write_text(json.dumps(config))


            def heartbeat(_token, _cfg, status="available") -> None:
                events.append(f"heartbeat:{status}")

            def wait_with_heartbeat(*_args) -> None:
                events.append("wait")
                raise KeyboardInterrupt

            def build_connectivity(**kwargs):
                self.assertEqual(kwargs["agent_authtoken"], "agent-secret")
                self.assertEqual(
                    kwargs["internal_endpoint"],
                    "https://node-routing.internal",
                )
                self.assertEqual(kwargs["proxy_port"], proxy_port)
                return connectivity

            with (
                patch.object(cli, "CONFIG_DIR", config_dir),
                patch.object(cli, "CONFIG_PATH", config_path),
                patch.object(cli, "SERVE_PID_PATH", pid_path),
                patch.object(cli.signal, "signal"),
                patch.object(cli.subprocess, "Popen", return_value=inference) as popen,
                patch.object(cli, "_wait_for_health", side_effect=lambda *_: events.append("healthy")),
                patch.object(
                    cli,
                    "_start_usage_proxy",
                    side_effect=lambda *_: (events.append("start:proxy"), proxy)[1],
                ) as start_proxy,
                patch.object(cli, "_heartbeat", side_effect=heartbeat),
                patch.object(cli, "_wait_with_heartbeat", side_effect=wait_with_heartbeat),
                patch.object(cli, "NgrokConnectivity", side_effect=build_connectivity) as ngrok_connectivity,
            ):
                cli.serve(
                    local=False,
                    timeout=1,
                    usage_proxy_port=proxy_port,
                )

            start_proxy.assert_called_once_with(
                "http://127.0.0.1:8000",
                "node-secret",
                proxy_port,
            )
            self.assertFalse(pid_path.exists())
            ngrok_connectivity.assert_called_once()
            self.assertEqual(popen.call_count, 1)

        return events

    def test_ngrok_starts_after_health_and_proxy_then_stops_before_them(self) -> None:
        events = self._run_serve(
            {
                "command": "inference-command",
                "endpoint": "http://127.0.0.1:8000",
                "model_id": "model",
                "machine_info": {"gpu_model": "Test GPU", "gpu_count": 1},
                "node_id": "node-id",
                "access_token": "node-secret",
                "connectivity": {
                    "internal_endpoint": "https://node-routing.internal",
                    "public_endpoint": "https://routing.nodes.athenass.com",
                    "agent_authtoken": "agent-secret",
                },
            },
            proxy_port=8932,
        )

        self.assertEqual(
            events,
            [
                "healthy",
                "start:proxy",
                "start:ngrok",
                "heartbeat:available",
                "wait",
                "heartbeat:offline",
                "stop:ngrok",
                "stop:proxy",
                "close:proxy",
                "stop:inference",
                "wait:inference",
            ],
        )

    def test_heartbeat_reports_public_runtime_specs(self) -> None:
        config = {
            "command": "llama-server --model model.gguf",
            "model_id": "test-model",
            "machine_info": {"gpu_model": "Test GPU", "gpu_count": 2},
        }
        with patch.object(cli, "_api_post") as api_post:
            cli._heartbeat("node-secret", config)

        api_post.assert_called_once_with(
            f"{cli.API_BASE}/v1/nodes/heartbeat",
            {
                "status": "available",
                "active_requests": 0,
                "command": config["command"],
                "model_id": "test-model",
                "machine_info": config["machine_info"],
            },
            bearer="node-secret",
        )

    def test_legacy_cloudflare_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            config_path = config_dir / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "command": "inference-command",
                        "endpoint": "http://127.0.0.1:8000",
                        "node_id": "node-id",
                        "access_token": "node-secret",
                        "tunnel": {
                            "name": "legacy-tunnel",
                            "url": "https://legacy.example.com",
                        },
                        "tunnel_token": "tunnel-secret",
                    }
                )
            )

            with (
                patch.object(cli, "CONFIG_DIR", config_dir),
                patch.object(cli, "CONFIG_PATH", config_path),
                patch.object(cli, "SERVE_PID_PATH", config_dir / "serve.pid"),
                patch.object(cli.subprocess, "Popen") as popen,
                self.assertRaises(cli.typer.Exit),
            ):
                cli.serve(local=False, timeout=1, usage_proxy_port=8931)

            popen.assert_not_called()


class UsageProxyTests(unittest.TestCase):
    def test_streams_sse_before_upstream_finishes_and_reports_usage(self) -> None:
        first_sent = threading.Event()
        release_stream = threading.Event()
        usage_reported = threading.Event()
        received: queue.Queue[object] = queue.Queue()
        reports: list[tuple[dict, int, str]] = []

        class StreamingHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"1"}}]}\n\n')
                self.wfile.flush()
                first_sent.set()
                release_stream.wait(2)
                self.wfile.write(
                    b'data: {"model":"test-model","usage":{"prompt_tokens":2,'
                    b'"completion_tokens":1,"total_tokens":3}}\n\n'
                    b"data: [DONE]\n\n"
                )
                self.wfile.flush()

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StreamingHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        def capture_usage(data: dict, status: int, token: str) -> None:
            reports.append((data, status, token))
            usage_reported.set()

        proxy = None
        client_thread = None
        try:
            with (
                patch.object(cli, "_report_usage_data", side_effect=capture_usage),
                patch.object(cli, "_reservation_is_valid", return_value=True) as validate,
            ):
                endpoint = f"http://127.0.0.1:{upstream.server_port}"
                proxy = cli._start_usage_proxy(endpoint, "node-token", 0)

                def read_stream() -> None:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
                        data=b"{}",
                    )
                    request.add_header("Authorization", "Bearer reservation-token")
                    with urllib.request.urlopen(request, timeout=2) as response:
                        received.put(
                            (
                                response.status,
                                response.headers.get_content_type(),
                                response.headers.get("Content-Length"),
                                response.headers.get("Access-Control-Allow-Origin"),
                                response.readline(),
                            )
                        )
                        received.put(response.read())

                client_thread = threading.Thread(target=read_stream)
                client_thread.start()

                self.assertTrue(first_sent.wait(1))
                status, content_type, content_length, cors_origin, first_line = received.get(timeout=1)
                self.assertEqual(status, 200)
                self.assertEqual(content_type, "text/event-stream")
                self.assertIsNone(content_length)
                self.assertEqual(cors_origin, "*")
                self.assertIn(b'"content":"1"', first_line)

                release_stream.set()
                remainder = received.get(timeout=2)
                self.assertIn(b"data: [DONE]", remainder)
                self.assertTrue(usage_reported.wait(1))
                self.assertEqual(
                    reports,
                    [
                        (
                            {
                                "model": "test-model",
                                "usage": {
                                    "prompt_tokens": 2,
                                    "completion_tokens": 1,
                                    "total_tokens": 3,
                                },
                            },
                            200,
                            "node-token",
                        )
                    ],
                )
                validate.assert_called_once_with("reservation-token", "node-token")
        finally:
            release_stream.set()
            if client_thread is not None:
                client_thread.join(timeout=2)
            if proxy is not None:
                proxy.shutdown()
                proxy.server_close()
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)

    def test_validates_reservation_with_control_plane(self) -> None:
        received: queue.Queue[tuple[str | None, dict]] = queue.Queue()

        class ValidationHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                )
                received.put((self.headers.get("Authorization"), body))
                response = json.dumps({"valid": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ValidationHandler)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        try:
            with patch.object(cli, "API_BASE", f"http://127.0.0.1:{api.server_port}"):
                self.assertTrue(
                    cli._reservation_is_valid("reservation-token", "node-token")
                )
            self.assertEqual(
                received.get(timeout=1),
                (
                    "Bearer node-token",
                    {"reservation_token": "reservation-token"},
                ),
            )
        finally:
            api.shutdown()
            api.server_close()

    def test_rejects_requests_without_active_reservation(self) -> None:
        upstream_called = threading.Event()

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                pass

            def do_GET(self) -> None:
                upstream_called.set()
                self.send_response(200)
                self.end_headers()

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        proxy = cli._start_usage_proxy(
            f"http://127.0.0.1:{upstream.server_port}",
            "node-token",
            0,
        )
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{proxy.server_port}/v1/models",
                    timeout=2,
                )
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(
                raised.exception.headers.get("Access-Control-Allow-Origin"),
                "*",
            )
            raised.exception.close()
            self.assertFalse(upstream_called.is_set())
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_rejects_unlisted_backend_routes_before_validation(self) -> None:
        upstream_called = threading.Event()

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                pass

            def do_GET(self) -> None:
                upstream_called.set()
                self.send_response(200)
                self.end_headers()

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        proxy = cli._start_usage_proxy(
            f"http://127.0.0.1:{upstream.server_port}",
            "node-token",
            0,
        )
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/admin/shutdown",
                headers={"Authorization": "Bearer reservation-token"},
            )
            with (
                patch.object(cli, "_reservation_is_valid", return_value=True) as validate,
                self.assertRaises(urllib.error.HTTPError) as raised,
            ):
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()
            validate.assert_not_called()
            self.assertFalse(upstream_called.is_set())
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_rejects_oversized_request_before_upstream(self) -> None:
        upstream_called = threading.Event()

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                pass

            def do_POST(self) -> None:
                upstream_called.set()
                self.send_response(200)
                self.end_headers()

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        proxy = cli._start_usage_proxy(
            f"http://127.0.0.1:{upstream.server_port}",
            "node-token",
            0,
            max_request_bytes=8,
        )
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
                data=b"123456789",
                headers={"Authorization": "Bearer reservation-token"},
            )
            with (
                patch.object(cli, "_reservation_is_valid", return_value=True),
                self.assertRaises(urllib.error.HTTPError) as raised,
            ):
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 413)
            raised.exception.close()
            self.assertFalse(upstream_called.is_set())
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_rejects_requests_above_concurrency_limit(self) -> None:
        upstream_started = threading.Event()
        release_upstream = threading.Event()
        first_result: queue.Queue[int] = queue.Queue()

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                pass

            def do_GET(self) -> None:
                upstream_started.set()
                release_upstream.wait(2)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        proxy = cli._start_usage_proxy(
            f"http://127.0.0.1:{upstream.server_port}",
            "node-token",
            0,
            max_concurrent_requests=1,
        )

        def first_request() -> None:
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/v1/models",
                headers={"Authorization": "Bearer first-reservation"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                response.read()
                first_result.put(response.status)

        first_thread = threading.Thread(target=first_request)
        try:
            with patch.object(cli, "_reservation_is_valid", return_value=True):
                first_thread.start()
                self.assertTrue(upstream_started.wait(1))
                second = urllib.request.Request(
                    f"http://127.0.0.1:{proxy.server_port}/v1/models",
                    headers={"Authorization": "Bearer second-reservation"},
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(second, timeout=2)
                self.assertEqual(raised.exception.code, 429)
                raised.exception.close()
                release_upstream.set()
                self.assertEqual(first_result.get(timeout=2), 200)
        finally:
            release_upstream.set()
            first_thread.join(timeout=2)
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_allows_browser_preflight_without_a_reservation(self) -> None:
        proxy = cli._start_usage_proxy("http://127.0.0.1:1", "node-token", 0)
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
                method="OPTIONS",
                headers={
                    "Origin": "https://athenass.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization, content-type",
                },
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(
                    response.headers.get("Access-Control-Allow-Origin"),
                    "*",
                )
                self.assertIn(
                    "Authorization",
                    response.headers.get("Access-Control-Allow-Headers", ""),
                )
        finally:
            proxy.shutdown()
            proxy.server_close()

    def test_validates_reservation_and_strips_buyer_token_upstream(self) -> None:
        received_authorization: queue.Queue[str | None] = queue.Queue()

        class UpstreamHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                pass

            def do_GET(self) -> None:
                received_authorization.put(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        proxy = cli._start_usage_proxy(
            f"http://127.0.0.1:{upstream.server_port}",
            "node-token",
            0,
        )
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/v1/models",
                headers={"Authorization": "Bearer reservation-token"},
            )
            with patch.object(cli, "_reservation_is_valid", return_value=True) as validate:
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)

            validate.assert_called_once_with("reservation-token", "node-token")
            self.assertIsNone(received_authorization.get(timeout=1))
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()


if __name__ == "__main__":
    unittest.main()
