import json
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import uvicorn

from ass_node import webapp


class FakeNodeWorker:
    def __init__(self) -> None:
        self.pid = 4242
        self.stdout = ()
        self.released = threading.Event()

    def poll(self):
        return 0 if self.released.is_set() else None

    def wait(self, timeout=None) -> int:
        if not self.released.wait(timeout):
            raise subprocess.TimeoutExpired("worker", timeout)
        return 0




class LocalhostOperatorWebTests(unittest.TestCase):
    def test_stop_requires_confirmation_for_reserved_or_unknown_node(self) -> None:
        for reserved in (True, None):
            with (
                self.subTest(reserved=reserved),
                patch.object(webapp.process_manager, "status", return_value={"running": True}),
                patch.object(webapp.process_manager, "stop") as stop,
                patch.object(webapp, "get_state", return_value={"node": {"id": "owned"}}),
                patch.object(webapp, "list_nodes", return_value=[{"id": "owned", "reserved": reserved}]),
            ):
                response = webapp.node_stop(webapp.NodeStopRequest())
                self.assertEqual(response.status_code, 409)
                stop.assert_not_called()

    def test_stop_checks_current_node_not_another_owned_node(self) -> None:
        with (
            patch.object(webapp.process_manager, "status", return_value={"running": True}),
            patch.object(webapp.process_manager, "stop", return_value={"running": False}),
            patch.object(webapp, "get_state", return_value={"node": {"id": "local"}}),
            patch.object(webapp, "list_nodes", return_value=[
                {"id": "other", "reserved": True}, {"id": "local", "reserved": False},
            ]),
        ):
            self.assertFalse(webapp.node_stop(webapp.NodeStopRequest())["running"])

    def test_stop_lookup_failure_requires_confirmation_but_force_can_stop(self) -> None:
        with (
            patch.object(webapp.process_manager, "status", return_value={"running": True}),
            patch.object(webapp.process_manager, "stop", return_value={"running": False}) as stop,
            patch.object(webapp, "get_state", return_value={"node": {"id": "owned"}}),
            patch.object(webapp, "list_nodes", side_effect=webapp.OperatorError("unreachable")),
        ):
            response = webapp.node_stop(webapp.NodeStopRequest())
            self.assertEqual(json.loads(response.body)["code"], "reservation_unknown")
            stop.assert_not_called()
            self.assertFalse(webapp.node_stop(webapp.NodeStopRequest(force=True))["running"])

    def test_process_manager_launches_isolated_worker_and_stops_group(self) -> None:
        worker = FakeNodeWorker()
        manager = webapp.NodeProcessManager()
        with (
            patch.object(
                webapp,
                "get_state",
                return_value={"authenticated": True, "configured": True},
            ),
            patch.object(webapp.subprocess, "Popen", return_value=worker) as popen,
        ):
            status = manager.start()

        self.assertTrue(status["running"])
        self.assertEqual(
            popen.call_args.args[0],
            [
                webapp.sys.executable,
                "-m",
                "ass_node.operator_worker",
                "serve",
            ],
        )
        with patch.object(
            webapp.os,
            "killpg",
            side_effect=lambda _pid, _signal: worker.released.set(),
        ) as kill_group:
            stopped = manager.stop()
        self.assertFalse(stopped["running"])
        kill_group.assert_called_once_with(worker.pid, signal.SIGTERM)

    def test_process_manager_rejects_signed_out_start(self) -> None:
        manager = webapp.NodeProcessManager()
        with (
            patch.object(
                webapp,
                "get_state",
                return_value={"authenticated": False, "configured": True},
            ),
            patch.object(webapp.subprocess, "Popen") as popen,
            self.assertRaisesRegex(webapp.OperatorError, "Sign in"),
        ):
            manager.start()

        popen.assert_not_called()

    def test_process_manager_recovers_persisted_worker_status(self) -> None:
        manager = webapp.NodeProcessManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "serve.pid"
            pid_path.write_text("4242")
            with (
                patch.object(webapp, "SERVE_PID_PATH", pid_path),
                patch.object(manager, "_pid_alive", return_value=True),
            ):
                status = manager.status()

        self.assertEqual(
            status,
            {"running": True, "pid": 4242, "last_exit_code": None},
        )

    def test_process_manager_stops_persisted_worker(self) -> None:
        manager = webapp.NodeProcessManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            pid_path = Path(temp_dir) / "serve.pid"
            inference_path = pid_path.with_name("inference.pid")
            pid_path.write_text("4242")
            inference_path.write_text("5252")

            def stop_worker(_pid: int, _signal: int) -> None:
                pid_path.unlink(missing_ok=True)
                inference_path.unlink(missing_ok=True)

            with (
                patch.object(webapp, "SERVE_PID_PATH", pid_path),
                patch.object(
                    manager,
                    "_pid_alive",
                    side_effect=[True, False, False],
                ),
                patch.object(webapp.os, "kill", side_effect=stop_worker) as kill,
            ):
                status = manager.stop()

        self.assertFalse(status["running"])
        kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_machine_details_are_forwarded_for_registration(self) -> None:
        machine_info = {
            "gpu": "NVIDIA RTX 4090 24 GB",
            "cpu": "Intel Core i9-14900K",
            "memory": "64 GB",
            "operating_system": "Ubuntu 24.04",
            "engine": "ExLlamaV3",
            "context": "190K",
        }
        request = webapp.RegisterRequest(
            name="home-gpu",
            model_id="Qwen3.8-27B-EXL3",
            command="./start.sh --port 8000",
            port=8000,
            machine_info_json=json.dumps(machine_info),
        )
        with patch.object(webapp, "register_node", return_value={}) as register:
            webapp.node_register(request)

        register.assert_called_once_with(
            "home-gpu",
            "Qwen3.8-27B-EXL3",
            "./start.sh --port 8000",
            8000,
            machine_info,
        )

    def test_invalid_machine_json_never_reaches_registration_service(self) -> None:
        request = webapp.RegisterRequest(
            name="home-gpu",
            model_id="model",
            command="./start.sh",
            port=8000,
            machine_info_json="gpu: RTX 4090",
        )
        with patch.object(webapp, "register_node") as register:
            response = webapp.node_register(request)

        self.assertEqual(response.status_code, 400)
        register.assert_not_called()

    def test_legacy_wrapped_plain_text_payload_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            webapp.RegisterRequest(
                name="home-gpu",
                model_id="model",
                command="./start.sh",
                port=8000,
                machine_info={"setup_details": "RTX 4090"},
            )

    def test_deleting_configured_node_stops_worker_first(self) -> None:
        request = webapp.NodeDeleteRequest(name="home-gpu")
        with (
            patch.object(
                webapp,
                "get_state",
                return_value={"node": {"name": "home-gpu"}},
            ),
            patch.object(webapp.process_manager, "stop") as stop,
            patch.object(
                webapp,
                "delete_node",
                return_value={"status": "deleted", "name": "home-gpu"},
            ) as delete,
        ):
            result = webapp.node_delete(request)

        self.assertEqual(result, {"status": "deleted", "name": "home-gpu"})
        stop.assert_called_once_with()
        delete.assert_called_once_with("home-gpu")

    def test_node_update_forwards_configured_node_details(self) -> None:
        request = webapp.UpdateNodeRequest(
            node_id="node-1",
            name="new-name",
            model_id="new-model",
            command="./serve",
            port=9000,
            machine_info_json='{"gpu":"RTX 4090"}',
        )
        with (
            patch.object(
                webapp.process_manager,
                "status",
                return_value={"running": False},
            ),
            patch.object(webapp, "update_node", return_value={}) as update,
        ):
            webapp.node_update(request)

        update.assert_called_once_with(
            "node-1",
            "new-name",
            "new-model",
            "./serve",
            9000,
            {"gpu": "RTX 4090"},
        )

    def test_login_start_returns_and_prints_authorization_url(self) -> None:
        authorization = {
            "device_code": "device-code",
            "user_code": "SE5V-YEKV",
            "verification_url": (
                "https://athenass.com/cli/authorize?code=SE5V-YEKV"
            ),
            "interval": 5,
        }
        with (
            patch.object(webapp, "begin_login", return_value=authorization),
            patch("builtins.print") as output,
        ):
            result = webapp.login_start()

        self.assertEqual(result, authorization)
        output.assert_called_once_with(
            "Authorize AthenaSS Operator: "
            "https://athenass.com/cli/authorize?code=SE5V-YEKV",
            flush=True,
        )

    @classmethod
    def setUpClass(cls) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]

        config = uvicorn.Config(
            webapp.app,
            host="127.0.0.1",
            port=cls.port,
            log_level="error",
        )
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 5
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.02)
        if not cls.server.started:
            raise RuntimeError("Test operator server did not start")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def request(
        self,
        path: str,
        session: str | None = None,
        method: str = "GET",
    ) -> urllib.request.Request:
        headers = {"X-AthenaSS-Session": session} if session else {}
        return urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers=headers,
            method=method,
        )

    def test_root_injects_session_without_putting_it_in_url(self) -> None:
        with urllib.request.urlopen(self.request("/"), timeout=5) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(webapp._SESSION_TOKEN, body)
        self.assertNotIn("__ATHENA_SESSION_TOKEN__", body)

    def test_browser_assets_cannot_use_stale_cached_validation(self) -> None:
        for path in ("/app.js?v=4", "/styles.css?v=4"):
            with self.subTest(path=path):
                with urllib.request.urlopen(self.request(path), timeout=5) as response:
                    self.assertEqual(
                        response.headers["Cache-Control"],
                        "no-store, must-revalidate",
                    )

    def test_control_api_rejects_missing_session_header(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.request("/api/state"), timeout=5)
        self.assertEqual(raised.exception.code, 403)

    def test_authenticated_state_includes_runner_status(self) -> None:
        local_state = {
            "authenticated": True,
            "user_id": "user-1",
            "configured": False,
            "node": None,
        }
        with patch.object(webapp, "get_state", return_value=local_state):
            with urllib.request.urlopen(
                self.request("/api/state", webapp._SESSION_TOKEN), timeout=5
            ) as response:
                result = json.loads(response.read())
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["runner"]["running"])

    def test_logout_endpoint_returns_signed_out_state(self) -> None:
        signed_out = {
            "authenticated": False,
            "user_id": None,
            "configured": True,
            "node": {"id": "node-1"},
        }
        with patch.object(webapp, "sign_out", return_value=signed_out) as sign_out:
            with urllib.request.urlopen(
                self.request(
                    "/api/logout",
                    webapp._SESSION_TOKEN,
                    method="POST",
                ),
                timeout=5,
            ) as response:
                result = json.loads(response.read())
        self.assertFalse(result["authenticated"])
        self.assertTrue(result["configured"])
        sign_out.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
