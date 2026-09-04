import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from ass_node import operator_service


class OperatorServiceTests(unittest.TestCase):
    def test_register_node_is_engine_agnostic_and_saves_private_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            credentials_path = config_dir / "credentials.json"
            config_path = config_dir / "config.json"
            credentials_path.write_text(
                json.dumps({"access_token": "user-token", "user_id": "user-1"})
            )
            registration = {
                "node_id": "node-1",
                "access_token": "node-token",
            }
            connectivity = {
                "routing_id": "route-1",
                "internal_endpoint": "https://route-1.internal",
                "public_endpoint": "https://route-1.nodes.athenass.com",
                "agent_authtoken": "ngrok-token",
            }
            machine_info = {
                "gpu": "NVIDIA RTX 4090 24 GB",
                "memory": "64 GB",
                "operating_system": "Ubuntu 24.04",
                "engine": "ExLlamaV3 5.0 bpw",
                "context": "128K",
            }

            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(operator_service, "CONFIG_PATH", config_path),
                patch.object(
                    operator_service,
                    "_request_json",
                    side_effect=[registration, connectivity],
                ) as request_json,
            ):
                state = operator_service.register_node(
                    "mac-studio",
                    "local-model",
                    "any-inference-server --port 8123",
                    8123,
                    machine_info,
                )

            self.assertTrue(state["configured"])
            self.assertEqual(
                request_json.call_args_list,
                [
                    call(
                        "POST",
                        "/v1/cli/node/register",
                        {
                            "name": "mac-studio",
                            "model_id": "local-model",
                            "command": "any-inference-server --port 8123",
                            "machine_info": machine_info,
                        },
                        "user-token",
                    ),
                    call(
                        "POST",
                        "/v1/cli/connectivity/provision",
                        {},
                        "node-token",
                    ),
                ],
            )
            saved = json.loads(config_path.read_text())
            self.assertEqual(saved["command"], "any-inference-server --port 8123")
            self.assertEqual(saved["endpoint"], "http://127.0.0.1:8123")
            self.assertEqual(saved["machine_info"], machine_info)
            self.assertEqual(state["node"]["machine_info"], machine_info)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

    def test_list_nodes_hides_soft_deleted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            credentials_path.write_text(
                json.dumps({"access_token": "user-token", "user_id": "user-1"})
            )
            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(
                    operator_service,
                    "_request_json",
                    return_value={
                        "nodes": [
                            {"name": "active-node", "deleted_at": None},
                            {"name": "old-node", "deleted_at": "2026-08-20T18:00:00Z"},
                        ]
                    },
                ),
            ):
                nodes = operator_service.list_nodes()

            self.assertEqual(nodes, [{"name": "active-node", "deleted_at": None}])

    def test_delete_node_soft_deletes_remotely_and_removes_matching_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            config_path = Path(temp_dir) / "config.json"
            credentials_path.write_text(
                json.dumps({"access_token": "user-token", "user_id": "user-1"})
            )
            config_path.write_text(
                json.dumps({"node_id": "node-1", "node_name": "home-gpu"})
            )
            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(operator_service, "CONFIG_PATH", config_path),
                patch.object(
                    operator_service,
                    "_request_json",
                    return_value={"status": "deleted", "name": "home-gpu"},
                ) as request_json,
            ):
                result = operator_service.delete_node(" home-gpu ")

            self.assertEqual(result, {"status": "deleted", "name": "home-gpu"})
            request_json.assert_called_once_with(
                "POST",
                "/v1/cli/node/delete",
                {"name": "home-gpu"},
                "user-token",
            )
            self.assertFalse(config_path.exists())

    def test_update_node_edits_in_place_and_preserves_connectivity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            config_path = Path(temp_dir) / "config.json"
            credentials_path.write_text(
                json.dumps({"access_token": "user-token", "user_id": "user-1"})
            )
            config_path.write_text(
                json.dumps(
                    {
                        "node_id": "node-1",
                        "node_name": "old-name",
                        "access_token": "node-token",
                        "connectivity": {"routing_id": "route-1"},
                    }
                )
            )
            machine_info = {"gpu": "RTX 4090 24 GB"}
            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(operator_service, "CONFIG_PATH", config_path),
                patch.object(
                    operator_service,
                    "_request_json",
                    return_value={"status": "updated"},
                ) as request_json,
            ):
                state = operator_service.update_node(
                    "node-1",
                    "new-name",
                    "new-model",
                    "./serve --port 9000",
                    9000,
                    machine_info,
                )

            request_json.assert_called_once_with(
                "POST",
                "/v1/cli/node/update",
                {
                    "node_id": "node-1",
                    "name": "new-name",
                    "model_id": "new-model",
                    "command": "./serve --port 9000",
                    "machine_info": machine_info,
                },
                "user-token",
            )
            saved = json.loads(config_path.read_text())
            self.assertEqual(saved["node_id"], "node-1")
            self.assertEqual(saved["node_name"], "new-name")
            self.assertEqual(saved["endpoint"], "http://127.0.0.1:9000")
            self.assertEqual(saved["access_token"], "node-token")
            self.assertEqual(saved["connectivity"], {"routing_id": "route-1"})
            self.assertEqual(state["node"]["machine_info"], machine_info)

    def test_update_node_uses_existing_heartbeat_api_when_name_is_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            config_path = Path(temp_dir) / "config.json"
            credentials_path.write_text(json.dumps({"access_token": "user-token"}))
            config_path.write_text(
                json.dumps(
                    {
                        "node_id": "node-1",
                        "node_name": "home-gpu",
                        "access_token": "node-token",
                    }
                )
            )
            machine_info = {"gpu": "RTX 4090 24 GB"}
            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(operator_service, "CONFIG_PATH", config_path),
                patch.object(
                    operator_service,
                    "_request_json",
                    return_value={"status": "ok"},
                ) as request_json,
            ):
                operator_service.update_node(
                    "node-1",
                    "home-gpu",
                    "Qwen3.8-27B",
                    "./start.sh",
                    8888,
                    machine_info,
                )

            request_json.assert_called_once_with(
                "POST",
                "/v1/nodes/heartbeat",
                {
                    "command": "./start.sh",
                    "machine_info": machine_info,
                    "model_id": "Qwen3.8-27B",
                    "status": "offline",
                },
                "node-token",
            )

    def test_update_node_explains_when_rename_api_is_not_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            config_path = Path(temp_dir) / "config.json"
            credentials_path.write_text(json.dumps({"access_token": "user-token"}))
            config_path.write_text(
                json.dumps(
                    {
                        "node_id": "node-1",
                        "node_name": "old-name",
                        "access_token": "node-token",
                    }
                )
            )
            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(operator_service, "CONFIG_PATH", config_path),
                patch.object(
                    operator_service,
                    "_request_json",
                    side_effect=operator_service.OperatorError(
                        "not found",
                        status_code=404,
                    ),
                ),
                self.assertRaisesRegex(
                    operator_service.OperatorError,
                    "latest AthenaSS API deployment",
                ),
            ):
                operator_service.update_node(
                    "node-1",
                    "new-name",
                    "model",
                    "./start.sh",
                    8888,
                )

    def test_register_node_rejects_invalid_port_before_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            credentials_path.write_text(
                json.dumps({"access_token": "user-token", "user_id": "user-1"})
            )
            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(operator_service, "_request_json") as request_json,
                self.assertRaisesRegex(operator_service.OperatorError, "Port"),
            ):
                operator_service.register_node("node", "model", "server", 0)
            request_json.assert_not_called()

    def test_sign_out_removes_account_credential_and_keeps_node_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            credentials_path = Path(temp_dir) / "credentials.json"
            config_path = Path(temp_dir) / "config.json"
            credentials_path.write_text(
                json.dumps({"access_token": "user-token", "user_id": "user-1"})
            )
            config_path.write_text(
                json.dumps({"node_id": "node-1", "node_name": "home-gpu"})
            )
            with (
                patch.object(operator_service, "CREDENTIALS_PATH", credentials_path),
                patch.object(operator_service, "CONFIG_PATH", config_path),
            ):
                state = operator_service.sign_out()

            self.assertFalse(credentials_path.exists())
            self.assertTrue(config_path.exists())
            self.assertFalse(state["authenticated"])
            self.assertTrue(state["configured"])


if __name__ == "__main__":
    unittest.main()
