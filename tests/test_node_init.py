import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from ass_node import cli


class NgrokNodeInitTests(unittest.TestCase):
    def test_registers_provisions_and_saves_restricted_config(self) -> None:
        registration = {
            "node_id": "node-id",
            "node_name": "seller-4090",
            "access_token": "ass_node_secret",
        }
        connectivity = {
            "routing_id": "routing-id",
            "internal_endpoint": "https://node-routing-id.internal",
            "public_endpoint": "https://routing-id.nodes.athenass.com",
            "agent_authtoken": "agent-secret",
        }
        runtime = {
            "model_id": "test-model",
            "command": "test-server --port 8000",
            "endpoint": "http://127.0.0.1:8000",
            "machine_info": {
                "gpu_model": "NVIDIA RTX 4090",
                "gpu_count": 1,
                "gpu_memory_gb": 24,
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "ass-node"
            config_path = config_dir / "config.json"
            output = io.StringIO()

            with (
                patch.object(cli, "CONFIG_DIR", config_dir),
                patch.object(cli, "CONFIG_PATH", config_path),
                patch.object(cli, "_load_credentials", return_value={"access_token": "user-secret"}),
                patch.object(cli, "_prompt_runtime_config", return_value=runtime),
                patch.object(cli.typer, "prompt", return_value="seller-4090"),
                patch.object(cli, "_api_post", side_effect=[registration, connectivity]) as api_post,
                contextlib.redirect_stdout(output),
            ):
                cli.node_init()

            self.assertEqual(
                api_post.call_args_list,
                [
                    call(
                        f"{cli.API_BASE}/v1/cli/node/register",
                        {
                            "name": "seller-4090",
                            "model_id": "test-model",
                            "command": "test-server --port 8000",
                            "machine_info": runtime["machine_info"],
                        },
                        bearer="user-secret",
                        no_exit=True,
                    ),
                    call(
                        f"{cli.API_BASE}/v1/cli/connectivity/provision",
                        {},
                        bearer="ass_node_secret",
                    ),
                ],
            )

            saved = json.loads(config_path.read_text())
            self.assertEqual(saved["machine_info"], runtime["machine_info"])
            self.assertNotIn("engine", saved)
            self.assertEqual(saved["connectivity"], connectivity)
            self.assertNotIn("tunnel", saved)
            self.assertNotIn("tunnel_token", saved)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertNotIn("agent-secret", output.getvalue())
            self.assertNotIn("ass_node_secret", output.getvalue())

    def test_registration_error_never_prompts_to_delete_node(self) -> None:
        runtime = {
            "model_id": "test-model",
            "command": "test-server --port 8000",
            "endpoint": "http://127.0.0.1:8000",
            "machine_info": {"gpu_count": 1},
        }
        errors = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "ass-node"
            config_path = config_dir / "config.json"
            with (
                patch.object(cli, "CONFIG_DIR", config_dir),
                patch.object(cli, "CONFIG_PATH", config_path),
                patch.object(
                    cli,
                    "_load_credentials",
                    return_value={"access_token": "user-secret"},
                ),
                patch.object(cli, "_prompt_runtime_config", return_value=runtime),
                patch.object(cli.typer, "prompt", return_value="seller-4090"),
                patch.object(cli.typer, "confirm") as confirm,
                patch.object(cli, "_api_post", return_value=None) as api_post,
                contextlib.redirect_stderr(errors),
                self.assertRaises(cli.typer.Exit),
            ):
                cli.node_init()

            confirm.assert_not_called()
            self.assertEqual(api_post.call_count, 1)
            self.assertFalse(config_path.exists())
            self.assertIn("No existing node was deleted", errors.getvalue())

    def test_machine_info_retries_until_json_object_is_valid(self) -> None:
        errors = io.StringIO()
        with (
            patch.object(
                cli.typer,
                "prompt",
                side_effect=["not-json", "[1, 2]", '{"gpu_count": 2}'],
            ),
            contextlib.redirect_stderr(errors),
        ):
            result = cli._prompt_machine_info()

        self.assertEqual(result, {"gpu_count": 2})
        self.assertIn("Invalid JSON", errors.getvalue())
        self.assertIn("must be a JSON object", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
