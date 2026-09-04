"""Engine-agnostic application service used by the desktop operator."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from ass_node.cli import API_BASE, CONFIG_PATH, CREDENTIALS_PATH

_USER_AGENT = "AthenaSSOperator/0.1"


class OperatorError(RuntimeError):
    """A user-facing operator action failed."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _request_json(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    bearer: str | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    request = urllib.request.Request(
        f"{API_BASE}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("error") or detail.get("detail") or str(exc)
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = str(exc)
        raise OperatorError(
            f"AthenaSS (A77) API error: {message}",
            status_code=exc.code,
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OperatorError(f"Could not reach AthenaSS (A77): {reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OperatorError("AthenaSS (A77) returned an invalid response") from exc

    if not isinstance(result, dict):
        raise OperatorError("AthenaSS (A77) returned an invalid response")
    return result


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        result = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2)
            file.write("\n")
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def get_state() -> dict[str, Any]:
    credentials = _read_json(CREDENTIALS_PATH)
    config = _read_json(CONFIG_PATH)
    return {
        "authenticated": bool(credentials and credentials.get("access_token")),
        "user_id": credentials.get("user_id") if credentials else None,
        "configured": bool(config and config.get("node_id")),
        "node": {
            "id": config.get("node_id"),
            "name": config.get("node_name"),
            "model_id": config.get("model_id"),
            "command": config.get("command"),
            "machine_info": config.get("machine_info")
            if isinstance(config.get("machine_info"), dict)
            else {},
            "endpoint": config.get("endpoint"),
            "public_endpoint": (config.get("connectivity") or {}).get(
                "public_endpoint"
            ),
        }
        if config
        else None,
    }

def sign_out() -> dict[str, Any]:
    """Remove the local account credential without deleting node configuration."""
    try:
        CREDENTIALS_PATH.unlink(missing_ok=True)
    except OSError as exc:
        raise OperatorError(f"Could not remove local credentials: {exc}") from exc
    return get_state()


def begin_login(open_browser: bool = True) -> dict[str, Any]:
    result = _request_json("POST", "/v1/cli/auth/init", {})
    verification_url = result.get("verification_url")
    if open_browser and isinstance(verification_url, str):
        webbrowser.open(verification_url)
    return result


def poll_login(device_code: str) -> dict[str, Any]:
    if not device_code.strip():
        raise OperatorError("Missing device authorization code")
    result = _request_json(
        "POST", "/v1/cli/auth/token", {"device_code": device_code}
    )
    if result.get("status") == "approved":
        _write_private_json(
            CREDENTIALS_PATH,
            {
                "access_token": result["access_token"],
                "user_id": result["user_id"],
            },
        )
    return result


def register_node(
    name: str,
    model_id: str,
    command: str,
    port: int,
    machine_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    credentials = _read_json(CREDENTIALS_PATH)
    if not credentials or not credentials.get("access_token"):
        raise OperatorError("Sign in before registering a node")
    if not name.strip() or not model_id.strip() or not command.strip():
        raise OperatorError("Node name, model, and launch command are required")
    if not 1 <= port <= 65535:
        raise OperatorError("Port must be between 1 and 65535")
    if machine_info is not None and not isinstance(machine_info, dict):
        raise OperatorError("Machine information must be an object")
    supplied_machine_info = dict(machine_info or {})

    node = _request_json(
        "POST",
        "/v1/cli/node/register",
        {
            "name": name.strip(),
            "model_id": model_id.strip(),
            "command": command.strip(),
            "machine_info": supplied_machine_info,
        },
        credentials["access_token"],
    )
    connectivity = _request_json(
        "POST",
        "/v1/cli/connectivity/provision",
        {},
        node["access_token"],
    )
    config = {
        "command": command.strip(),
        "endpoint": f"http://127.0.0.1:{port}",
        "model_id": model_id.strip(),
        "machine_info": supplied_machine_info,
        "node_id": node["node_id"],
        "node_name": name.strip(),
        "access_token": node["access_token"],
        "connectivity": {
            "routing_id": connectivity["routing_id"],
            "internal_endpoint": connectivity["internal_endpoint"],
            "public_endpoint": connectivity["public_endpoint"],
            "agent_authtoken": connectivity["agent_authtoken"],
        },
    }
    _write_private_json(CONFIG_PATH, config)
    return get_state()

def update_node(
    node_id: str,
    name: str,
    model_id: str,
    command: str,
    port: int,
    machine_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    credentials = _read_json(CREDENTIALS_PATH)
    config = _read_json(CONFIG_PATH)
    if not credentials or not credentials.get("access_token"):
        raise OperatorError("Sign in before editing a node")
    if not config or config.get("node_id") != node_id:
        raise OperatorError("Only the node configured on this machine can be edited")
    if not name.strip() or not model_id.strip() or not command.strip():
        raise OperatorError("Node name, model, and launch command are required")
    if not 1 <= port <= 65535:
        raise OperatorError("Port must be between 1 and 65535")
    if machine_info is not None and not isinstance(machine_info, dict):
        raise OperatorError("Machine information must be an object")
    supplied_machine_info = dict(machine_info or {})

    node_name = name.strip()
    update_body = {
        "node_id": node_id,
        "name": node_name,
        "model_id": model_id.strip(),
        "command": command.strip(),
        "machine_info": supplied_machine_info,
    }
    if config.get("node_name") == node_name:
        _request_json(
            "POST",
            "/v1/nodes/heartbeat",
            {
                "command": update_body["command"],
                "machine_info": supplied_machine_info,
                "model_id": update_body["model_id"],
                "status": "offline",
            },
            config.get("access_token"),
        )
    else:
        try:
            _request_json(
                "POST",
                "/v1/cli/node/update",
                update_body,
                credentials["access_token"],
            )
        except OperatorError as exc:
            if exc.status_code == 404:
                raise OperatorError(
                    "Renaming nodes requires the latest AthenaSS API deployment. "
                    "Setup details and other configuration can still be saved "
                    "without changing the node name."
                ) from exc
            raise
    config.update(
        {
            "command": command.strip(),
            "endpoint": f"http://127.0.0.1:{port}",
            "machine_info": supplied_machine_info,
            "model_id": model_id.strip(),
            "node_name": node_name,
        }
    )
    _write_private_json(CONFIG_PATH, config)
    return get_state()


def list_nodes() -> list[dict[str, Any]]:
    credentials = _read_json(CREDENTIALS_PATH)
    if not credentials or not credentials.get("access_token"):
        raise OperatorError("Sign in before listing nodes")
    result = _request_json(
        "GET", "/v1/cli/nodes", bearer=credentials["access_token"]
    )
    nodes = result.get("nodes", [])
    if not isinstance(nodes, list):
        raise OperatorError("AthenaSS (A77) returned an invalid node list")
    return [
        node
        for node in nodes
        if isinstance(node, dict) and not node.get("deleted_at")
    ]


def delete_node(name: str) -> dict[str, Any]:
    credentials = _read_json(CREDENTIALS_PATH)
    if not credentials or not credentials.get("access_token"):
        raise OperatorError("Sign in before deleting a node")
    node_name = name.strip()
    if not node_name:
        raise OperatorError("Missing node name")

    result = _request_json(
        "POST",
        "/v1/cli/node/delete",
        {"name": node_name},
        credentials["access_token"],
    )
    config = _read_json(CONFIG_PATH)
    if config and config.get("node_name") == node_name:
        try:
            CONFIG_PATH.unlink(missing_ok=True)
        except OSError as exc:
            raise OperatorError(
                f"Node deleted, but its local configuration could not be removed: {exc}"
            ) from exc
    return result
