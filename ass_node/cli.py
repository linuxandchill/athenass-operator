"""
AthenaSS Operator (A77) internal node runtime.

Commands:
  ass login        — Authenticate with the AthenaSS (A77) service (user-level).
  ass logout       — Stop any running node, remove local credentials and configuration.
  ass status       — Show local node status.
  ass serve        — Launch inference server + ngrok connectivity + heartbeats.
  ass node init    — Register a node, configure runtime + connectivity.
  ass node list    — List all registered nodes for the current user.
  ass node delete  — Delete a registered node and its connectivity credential.
  ass node stop    — Stop a locally running node without removing credentials.
"""

import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import typer

from ass_node.connectivity import DEFAULT_MAX_REQUEST_BYTES, NgrokConnectivity

app = typer.Typer()
node_app = typer.Typer(help="Manage nodes: register, list, delete.")
app.add_typer(node_app, name="node")

# ─── Paths ─────────────────────────────────────────────────────────────────────
# User config dir, not the installed package dir — writing into site-packages
# would fail on a non-editable install (often read-only, shared across users)
# and isn't where per-user secrets belong.
CONFIG_DIR = Path(
    os.getenv("ASS_CONFIG_DIR")
    or os.path.join(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config")), "ass-node")
)
CONFIG_PATH = CONFIG_DIR / "config.json"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
SERVE_PID_PATH = CONFIG_DIR / "serve.pid"

API_BASE = os.getenv("ATHENASS_API_URL", "https://api.athenass.com").rstrip("/")

_HEALTH_CHECK_INTERVAL = 2
_HEARTBEAT_INTERVAL = 25  # seconds between heartbeats

# Port the local usage-tracking proxy listens on. ngrok forwards the node's
# restricted Internal Endpoint here instead of directly to the inference engine.
_USAGE_PROXY_PORT = 8931
_MAX_CONCURRENT_REQUESTS = 8
_MAX_BUFFERED_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_STREAM_RESPONSE_BYTES = 64 * 1024 * 1024
_CLIENT_READ_TIMEOUT = 30


# ═══════════════════════════════════════════════════════════════════════════════
# CLI commands
# ═══════════════════════════════════════════════════════════════════════════════


@app.callback()
def main() -> None:
    """Internal command interface for AthenaSS Operator (A77)."""




# ─── ass node init ──────────────────────────────────────────────────────────────




def _prompt_machine_info() -> dict[str, Any]:
    """Prompt until the seller enters a valid JSON object."""
    typer.echo("\nDescribe this machine as a JSON object.")
    typer.echo(
        'Example: {"gpu_model":"NVIDIA RTX 4090","gpu_count":1,'
        '"gpu_memory_gb":24,"system_memory_gb":64}'
    )
    typer.echo("No fields are required; additional fields are allowed.")

    while True:
        raw = typer.prompt("Machine info JSON", default="{}")
        try:
            machine_info = json.loads(raw)
        except json.JSONDecodeError as exc:
            typer.echo(f"Invalid JSON: {exc.msg}. Try again.", err=True)
            continue
        if not isinstance(machine_info, dict):
            typer.echo("Machine info must be a JSON object. Try again.", err=True)
            continue
        return machine_info


def _prompt_runtime_config() -> dict[str, Any]:
    """Interactively collect model, launch command, endpoint, and machine facts."""
    typer.echo("\n--- Inference Runtime Configuration ---")
    model_id = typer.prompt("Model ID (e.g. Qwen3.8-27B)")
    port = typer.prompt("Inference server port", default="8000")
    endpoint = f"http://127.0.0.1:{port}"

    typer.echo("\nEnter the full shell command to start your inference server.")
    typer.echo("Example: ~/llama.cpp/build/bin/llama-server --model ~/models/model.gguf --port 8000")
    typer.echo(
        "This exact command will be public. Do not include API keys, tokens, "
        "passwords, or other secrets.",
        err=True,
    )
    while True:
        command = typer.prompt("Command", default=f"llama-server --port {port}")
        if typer.confirm("Publish this exact command to buyers?", default=False):
            break

    return {
        "model_id": model_id,
        "command": command,
        "endpoint": endpoint,
        "machine_info": _prompt_machine_info(),
    }



def _provision_connectivity_via_token(token: str) -> dict[str, str]:
    """Provision one restricted ngrok Agent credential server-side."""
    typer.echo("\n--- Provisioning Connectivity ---")

    result = _api_post(
        f"{API_BASE}/v1/cli/connectivity/provision",
        {},
        bearer=token,
    )
    typer.echo(f"Internal Endpoint: {result['internal_endpoint']}")
    typer.echo(f"Public Endpoint: {result['public_endpoint']}")
    return result


@node_app.command("init")
def node_init() -> None:
    """Register a new ngrok-connected node and save its local configuration.

    AthenaSS (A77) provisions a restricted Agent credential automatically. Sellers do
    not install ngrok or configure endpoint names.
    """
    # 1. Check user credentials
    user_creds = _load_credentials()
    if user_creds is None:
        typer.echo("Not logged in. Run 'ass login' first.", err=True)
        raise typer.Exit(1)
    user_token = user_creds["access_token"]

    # 2. Node name
    node_name = typer.prompt("Node name (e.g. home-gpu, office-rtx4090)")

    # 3. Public runtime configuration
    runtime_cfg = _prompt_runtime_config()
    registration = {
        "name": node_name,
        "model_id": runtime_cfg["model_id"],
        "command": runtime_cfg["command"],
        "machine_info": runtime_cfg["machine_info"],
    }

    # 4. Register node with AthenaSS (A77) API (creates node + node credential)
    typer.echo("\n--- Registering Node ---")
    node_result = _api_post(
        f"{API_BASE}/v1/cli/node/register",
        registration,
        bearer=user_token,
        no_exit=True,
    )

    if node_result is None:
        typer.echo(
            "Registration failed. No existing node was deleted. "
            "Resolve the API error and run 'ass node init' again.",
            err=True,
        )
        raise typer.Exit(1)

    node_token = node_result["access_token"]
    node_id = node_result["node_id"]
    typer.echo(f"Node registered: {node_name} ({node_id})")

    # 5. Provision restricted ngrok connectivity (uses node credential)
    connectivity = _provision_connectivity_via_token(node_token)

    # 6. Write config.json
    config = {
        "command": runtime_cfg["command"],
        "endpoint": runtime_cfg["endpoint"],
        "model_id": runtime_cfg["model_id"],
        "machine_info": runtime_cfg["machine_info"],
        "node_id": node_id,
        "node_name": node_name,
        "access_token": node_token,
        "connectivity": {
            "routing_id": connectivity["routing_id"],
            "internal_endpoint": connectivity["internal_endpoint"],
            "public_endpoint": connectivity["public_endpoint"],
            "agent_authtoken": connectivity["agent_authtoken"],
        },
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=4) + "\n")
    CONFIG_PATH.chmod(0o600)

    typer.echo(f"\nConfiguration saved to {CONFIG_PATH}")
    typer.echo(f"Run 'ass serve' to start your node.")






# ─── ass login ──────────────────────────────────────────────────────────────────


@app.command()
def login() -> None:
    """Authenticate with the AthenaSS (A77) service.

    Opens a device-authorization flow: you'll receive a URL to visit in
    your browser. After logging in there, the CLI saves credentials
    locally. Run 'ass node init' afterwards to register a node.
    """
    creds = _load_credentials()
    if creds is not None:
        typer.echo(
            f"Already logged in as user {creds.get('user_id', '?')}. Run 'ass logout' first.",
            err=True,
        )
        raise typer.Exit(1)

    # 1. Initiate device auth
    typer.echo("Contacting AthenaSS (A77) service...")
    init_data = _api_post(f"{API_BASE}/v1/cli/auth/init", {})

    device_code = init_data["device_code"]
    user_code = init_data["user_code"]
    verification_url = init_data["verification_url"]
    interval = init_data.get("interval", 5)

    typer.echo(f"\nOpen this URL in your browser:\n")
    typer.echo(f"  {verification_url}")
    typer.echo(f"\nYour code: {user_code}")
    typer.echo("\nWaiting for authorization...")

    # 2. Poll for approval
    while True:
        time.sleep(interval)
        result = _api_post(f"{API_BASE}/v1/cli/auth/token", {"device_code": device_code})

        status = result.get("status")
        if status == "pending":
            continue
        if status == "approved":
            break

        typer.echo(f"Authorization failed: {result.get('error', 'unknown')}", err=True)
        raise typer.Exit(1)

    # 3. Save user-level credentials
    creds = {
        "access_token": result["access_token"],
        "user_id": result["user_id"],
    }
    _save_credentials(creds)

    typer.echo(f"\nAuthenticated.")
    typer.echo("Run 'ass node init' to register a node.")
    typer.echo("Credentials saved.")


# ─── ass logout ─────────────────────────────────────────────────────────────────


@app.command()
def logout() -> None:
    """Stop any running node, then remove locally stored credentials and configuration."""
    cfg = _load_config()
    if cfg is not None:
        if _stop_running_serve():
            typer.echo("Stopped running node.")
        token = cfg.get("access_token")
        if token:
            _heartbeat(token, cfg, status="offline")

    removed = False
    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
        removed = True
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        removed = True
    if not removed:
        typer.echo("Nothing to remove.")
        raise typer.Exit(0)

    typer.echo("Credentials and configuration removed.")


# ─── ass node list ──────────────────────────────────────────────────────────────


@node_app.command("list")
def node_list() -> None:
    """List all registered nodes for the current user."""
    creds = _load_credentials()
    if creds is None:
        typer.echo("Not logged in. Run 'ass login' first.", err=True)
        raise typer.Exit(1)

    user_token = creds["access_token"]
    nodes = _api_get(f"{API_BASE}/v1/cli/nodes", bearer=user_token)

    if not nodes:
        typer.echo("No nodes registered.")
        return

    typer.echo(f"\n{'NAME':<20} {'STATUS':<12} {'MODEL':<24} {'ID'}")
    typer.echo("-" * 86)
    for n in nodes:
        deleted = " [deleted]" if n.get("deleted_at") else ""
        typer.echo(
            f"{n.get('name', '?'):<20} "
            f"{n.get('status', '?'):<12} "
            f"{(n.get('model_id') or '-'):<24} "
            f"{n.get('id', '?')}{deleted}"
        )


# ─── ass node delete ────────────────────────────────────────────────────────────


@node_app.command("delete")
def node_delete(
    name: str = typer.Argument(..., help="Name of the node to delete"),
) -> None:
    """Delete a registered node and its connectivity credential."""
    creds = _load_credentials()
    if creds is None:
        typer.echo("Not logged in. Run 'ass login' first.", err=True)
        raise typer.Exit(1)

    user_token = creds["access_token"]
    result = _api_post(
        f"{API_BASE}/v1/cli/node/delete",
        {"name": name},
        bearer=user_token,
    )

    typer.echo(f"Node '{result['name']}' deleted.")

    # If this was the node in config.json, stop it and remove the local config too
    cfg = _load_config()
    if cfg and cfg.get("node_name") == name:
        if _stop_running_serve():
            typer.echo("Stopped running node.")
        CONFIG_PATH.unlink()
        typer.echo("Local config removed (matched deleted node).")


# ─── ass node stop ──────────────────────────────────────────────────────────────


@node_app.command("stop")
def node_stop() -> None:
    """Stop a locally running `ass serve` process, if any."""
    if _stop_running_serve():
        typer.echo("Node stopped.")
    else:
        typer.echo("No running node found.")


# ─── ass status ─────────────────────────────────────────────────────────────────


@app.command()
def status() -> None:
    """Show the registered node's current status."""
    cfg = _load_config()
    if cfg is None:
        typer.echo("No node configured. Run 'ass node init' first.", err=True)
        raise typer.Exit(1)

    token = cfg["access_token"]
    result = _api_post(
        f"{API_BASE}/v1/nodes/status",
        {},
        bearer=token,
    )

    node = result.get("node", {})
    cred_info = result.get("credential", {})

    typer.echo(f"Node:       {node.get('name', '?')}")
    typer.echo(f"ID:         {node.get('id', '?')}")
    typer.echo(f"Status:     {node.get('status', '?')}")
    typer.echo(f"Command:    {node.get('command', '-') or '-'}")
    typer.echo(f"Model:      {node.get('model_id', '-') or '-'}")
    typer.echo(f"Last seen:  {node.get('last_seen_at', '-') or '-'}")
    deleted = node.get("deleted_at")
    if deleted:
        typer.echo(f"Deleted:    {deleted}")
    typer.echo(f"Credential: {cred_info.get('id', '?')}")


# ─── ass serve ──────────────────────────────────────────────────────────────────


@app.command()
def serve(
    local: bool = typer.Option(
        False, "--local", help="Run without external connectivity (local-only)"
    ),
    timeout: int = typer.Option(
        300, "--timeout", help="Max seconds to wait for inference server startup"
    ),
    usage_proxy_port: int = typer.Option(
        _USAGE_PROXY_PORT,
        "--usage-proxy-port",
        min=1,
        max=65535,
        help="Local usage-proxy port for ngrok connectivity",
    ),
) -> None:
    """Launch the inference server, usage proxy, and ngrok connectivity.

    Ctrl+C or SIGTERM stops every component cleanly.
    """
    # Refuse to start if another `ass serve` is already running for this config
    if SERVE_PID_PATH.exists():
        try:
            existing_pid = int(SERVE_PID_PATH.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None
        if existing_pid and _pid_alive(existing_pid):
            typer.echo(
                f"A node is already running (pid {existing_pid}). Use Stop in AthenaSS Operator.",
                err=True,
            )
            raise typer.Exit(1)
        SERVE_PID_PATH.unlink(missing_ok=True)

    if not CONFIG_PATH.exists():
        typer.echo(f"Config file not found: {CONFIG_PATH}", err=True)
        raise typer.Exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    command = cfg.get("command")
    if not command:
        typer.echo("Missing 'command' in config.json", err=True)
        raise typer.Exit(1)

    token = cfg.get("access_token")
    node_id = cfg.get("node_id")
    if not token or not node_id:
        typer.echo(
            "Missing node credentials. Register the node again in AthenaSS Operator.",
            err=True,
        )
        raise typer.Exit(1)

    endpoint = cfg.get("endpoint", "http://127.0.0.1:8000")
    connectivity_cfg = cfg.get("connectivity")
    if not isinstance(connectivity_cfg, dict):
        typer.echo(
            "Missing connectivity configuration. Register the node again in AthenaSS Operator.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        connectivity_session = NgrokConnectivity(
            agent_authtoken=connectivity_cfg["agent_authtoken"],
            internal_endpoint=connectivity_cfg["internal_endpoint"],
            proxy_port=usage_proxy_port,
        )
        public_url = connectivity_cfg["public_endpoint"]
    except (KeyError, TypeError, ValueError) as exc:
        typer.echo(f"Invalid ngrok connectivity config: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"Command:  {command}")
    typer.echo(f"Model:    {cfg.get('model_id', 'unknown')}")
    typer.echo(f"Endpoint: {endpoint}")
    typer.echo(f"Node ID:  {node_id}")

    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _raise_keyboard_interrupt)

    typer.echo("\nStarting inference server...")
    inference_proc = _launch_inference(command)
    _write_serve_pid()
    SERVE_PID_PATH.with_name("inference.pid").write_text(str(inference_proc.pid))

    usage_proxy: http.server.ThreadingHTTPServer | None = None

    try:
        _wait_for_health(endpoint, inference_proc, timeout)

        if not local:
            typer.echo("\nStarting usage-tracking proxy...")
            usage_proxy = _start_usage_proxy(endpoint, token, usage_proxy_port)

        if not local:
            typer.echo("\nStarting ngrok connectivity...")
            try:
                connectivity_session.start()
            except Exception as exc:
                typer.echo(f"Failed to start ngrok connectivity: {exc}", err=True)
                raise typer.Exit(1) from exc
            if not connectivity_session.connected:
                typer.echo("ngrok connectivity did not become ready.", err=True)
                raise typer.Exit(1)
            typer.echo(f"\nLocal endpoint:  {endpoint}")
            typer.echo(f"Public endpoint: {public_url}")
        else:
            typer.echo(f"\nLocal endpoint:  {endpoint}")
            typer.echo("Connectivity disabled (--local mode)")

        _heartbeat(token, cfg)

        typer.echo("\nReady for requests.")
        typer.echo("Press Ctrl+C to stop.")

        _wait_with_heartbeat(
            inference_proc,
            connectivity_session if not local else None,
            token,
            cfg,
        )

    except KeyboardInterrupt:
        typer.echo("\nShutting down...")
    finally:
        typer.echo("Marking node offline...")
        try:
            _heartbeat(token, cfg, status="offline")
        finally:
            try:
                _cleanup(
                    inference_proc,
                    connectivity_session if not local else None,
                    usage_proxy,
                )
            finally:
                SERVE_PID_PATH.with_name("inference.pid").unlink(missing_ok=True)
                _remove_serve_pid()
# ═══════════════════════════════════════════════════════════════════════════════
# Credential helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _credentials_path() -> Path:
    return CREDENTIALS_PATH


def _load_credentials() -> dict[str, Any] | None:
    path = _credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if "access_token" in data and "user_id" in data:
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _load_config() -> dict[str, Any] | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text())
        if "access_token" in data and "node_id" in data:
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _save_credentials(data: dict[str, Any]) -> None:
    path = _credentials_path()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    # Restrict permissions (Unix only)
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _api_post(
    url: str,
    body: dict[str, Any],
    bearer: str | None = None,
    no_exit: bool = False,
) -> dict[str, Any] | None:
    """POST JSON to *url* and return parsed JSON response.

    When no_exit=True, returns None on HTTP/connection errors
    instead of raising typer.Exit.
    """
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AthenaSSOperator/0.1",
    }
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msg = detail.get("error", str(e))
        except Exception:
            msg = str(e)
        if no_exit:
            typer.echo(f"API error: {msg}", err=True)
            return None
        typer.echo(f"API error: {msg}", err=True)
        raise typer.Exit(1)
    except urllib.error.URLError as e:
        if no_exit:
            typer.echo(f"Connection error: {e.reason}", err=True)
            return None
        typer.echo(f"Connection error: {e.reason}", err=True)
        raise typer.Exit(1)



def _api_get(
    url: str,
    bearer: str | None = None,
) -> list[dict[str, Any]]:
    """GET JSON from *url* and return parsed response."""
    headers = {
        "User-Agent": "AthenaSSOperator/0.1",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("nodes", [])
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msg = detail.get("error", str(e))
        except Exception:
            msg = str(e)
        typer.echo(f"API error: {msg}", err=True)
        raise typer.Exit(1)
    except urllib.error.URLError as e:
        typer.echo(f"Connection error: {e.reason}", err=True)
        raise typer.Exit(1)
# ═══════════════════════════════════════════════════════════════════════════════
# Heartbeat
# ═══════════════════════════════════════════════════════════════════════════════


def _heartbeat(token: str, cfg: dict[str, Any], status: str = "available") -> None:
    """Send a single heartbeat to the AthenaSS (A77) API."""
    body: dict[str, Any] = {
        "status": status,
        "active_requests": 0,
    }
    if cfg.get("command"):
        body["command"] = cfg["command"]
    if cfg.get("model_id"):
        body["model_id"] = cfg["model_id"]
    if isinstance(cfg.get("machine_info"), dict):
        body["machine_info"] = cfg["machine_info"]
    if cfg.get("context_length"):
        body["context_length"] = cfg["context_length"]

    _api_post(
        f"{API_BASE}/v1/nodes/heartbeat",
        body,
        bearer=token,
        no_exit=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Usage-tracking proxy
# ═══════════════════════════════════════════════════════════════════════════════
#
# Buyers hit the seller through its ngrok endpoint; `api.athenass.com` is never
# in the inference request path. This local proxy sits between ngrok and the
# inference engine, forwarding requests and reporting OpenAI-compatible usage
# back to AthenaSS (A77).

def _reservation_is_valid(reservation_token: str, node_token: str) -> bool:
    payload = json.dumps({"reservation_token": reservation_token}).encode("utf-8")
    request = urllib.request.Request(
        f"{API_BASE}/v1/nodes/reservations/validate",
        data=payload,
        headers={
            "Authorization": f"Bearer {node_token}",
            "Content-Type": "application/json",
            "User-Agent": "AthenaSSOperator/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                return False
            data = json.loads(response.read().decode("utf-8"))
            return data.get("valid") is True
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return False



_ALLOWED_PROXY_ROUTES = frozenset(
    {
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
        ("OPTIONS", "/v1/chat/completions"),
    }
)


def _proxy_route_is_allowed(method: str, target: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(target)
    except ValueError:
        return False
    return (
        not parsed.scheme
        and not parsed.netloc
        and (method, parsed.path) in _ALLOWED_PROXY_ROUTES
    )


class _UsageProxyHandler(http.server.BaseHTTPRequestHandler):
    endpoint = ""
    token = ""
    max_request_bytes = DEFAULT_MAX_REQUEST_BYTES

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silence default per-request access log

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_OPTIONS(self) -> None:
        if not _proxy_route_is_allowed(self.command, self.path):
            self._send_json_error(404, "Endpoint not found")
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.end_headers()

    def _proxy(self) -> None:
        if not _proxy_route_is_allowed(self.command, self.path):
            self._send_json_error(404, "Endpoint not found")
            return

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer ") or not auth_header[7:]:
            self._send_access_error()
            return
        reservation_token = auth_header[7:]
        if not _reservation_is_valid(reservation_token, self.token):
            self._send_access_error()
            return

        body = self._read_request_body()
        if body is None:
            return

        req = urllib.request.Request(
            f"{self.endpoint}{self.path}",
            data=body or None,
            method=self.command,
        )
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in (
                "authorization",
                "connection",
                "content-length",
                "cookie",
                "host",
                "keep-alive",
                "proxy-authorization",
                "proxy-connection",
                "te",
                "trailer",
                "transfer-encoding",
                "upgrade",
            ) or lowered.startswith("x-forwarded-"):
                continue
            req.add_header(key, value)

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                status = resp.status
                resp_headers = resp.getheaders()
                content_type = resp.headers.get_content_type()
                if content_type == "text/event-stream":
                    self._send_proxy_headers(status, resp_headers)
                    usage_data = self._stream_event_source(resp)
                    if usage_data is not None:
                        _report_usage_data(usage_data, status, self.token)
                    return
                resp_body = resp.read(_MAX_BUFFERED_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_headers = exc.headers.items()
            resp_body = exc.read(_MAX_BUFFERED_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError):
            self._send_json_error(502, "Inference server unavailable")
            return

        if len(resp_body) > _MAX_BUFFERED_RESPONSE_BYTES:
            self._send_json_error(502, "Inference response exceeded the size limit")
            return

        self._send_proxy_headers(status, resp_headers, len(resp_body))
        self.wfile.write(resp_body)

        if urllib.parse.urlsplit(self.path).path == "/v1/chat/completions":
            _report_usage(resp_body, status, self.token)

    def _read_request_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self._send_json_error(400, "Transfer-Encoding is not supported")
            return None

        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) > 1:
            self._send_json_error(400, "Multiple Content-Length headers")
            return None
        try:
            length = int(content_lengths[0]) if content_lengths else 0
        except ValueError:
            self._send_json_error(400, "Invalid Content-Length")
            return None
        if length < 0:
            self._send_json_error(400, "Invalid Content-Length")
            return None
        if length > self.max_request_bytes:
            self._send_json_error(413, "Request body exceeded the size limit")
            return None

        try:
            body = self.rfile.read(length) if length else b""
        except TimeoutError:
            self._send_json_error(408, "Request body timed out")
            return None
        if len(body) != length:
            self._send_json_error(400, "Incomplete request body")
            return None
        return body

    def _send_access_error(self) -> None:
        self._send_json_error(401, "A valid active reservation is required")

    def _send_json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_proxy_headers(
        self,
        status: int,
        resp_headers: Any,
        content_length: int | None = None,
    ) -> None:
        self.send_response(status)
        for key, value in resp_headers:
            if key.lower() in (
                "access-control-allow-origin",
                "connection",
                "content-length",
                "server",
                "set-cookie",
                "transfer-encoding",
            ):
                continue
            self.send_header(key, value)
        self.send_header("Access-Control-Allow-Origin", "*")
        if content_length is None:
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(content_length))
        self.end_headers()

    def _stream_event_source(self, resp: Any) -> dict[str, Any] | None:
        report_data: dict[str, Any] = {}
        saw_event = False
        transferred = 0
        for line in resp:
            transferred += len(line)
            if transferred > _MAX_STREAM_RESPONSE_BYTES:
                break
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            saw_event = True
            if data.get("model") and not report_data.get("model"):
                report_data["model"] = data["model"]

            usage = data.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            current_usage = report_data.get("usage")
            current_total = (
                current_usage.get("total_tokens")
                if isinstance(current_usage, dict)
                else None
            )
            candidate_total = usage.get("total_tokens")
            if (
                not isinstance(current_usage, dict)
                or (
                    type(candidate_total) in (int, float)
                    and (
                        type(current_total) not in (int, float)
                        or candidate_total > current_total
                    )
                )
            ):
                report_data["usage"] = usage

        return report_data if saw_event else None


def _report_usage(resp_body: bytes, status_code: int, token: str) -> None:
    """Best-effort: parse `usage` from JSON and report it to AthenaSS (A77)."""
    try:
        data = json.loads(resp_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    _report_usage_data(data, status_code, token)


def _report_usage_data(data: dict[str, Any], status_code: int, token: str) -> None:
    """Best-effort: report parsed usage to AthenaSS (A77)."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    body = {
        "status_code": status_code,
        "model": data.get("model"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    try:
        _api_post(f"{API_BASE}/v1/nodes/usage", body, bearer=token, no_exit=True)
    except SystemExit:
        pass


class _BoundedThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[http.server.BaseHTTPRequestHandler],
        max_concurrent_requests: int,
    ) -> None:
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        super().__init__(server_address, request_handler)

    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        request.settimeout(_CLIENT_READ_TIMEOUT)
        if not self._request_slots.acquire(blocking=False):
            body = b'{"error":"Too many concurrent requests"}'
            response = (
                b"HTTP/1.1 429 Too Many Requests\r\n"
                b"Content-Type: application/json\r\n"
                b"Connection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            try:
                request.sendall(response)
            except OSError:
                pass
            self.shutdown_request(request)
            return

        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


def _start_usage_proxy(
    endpoint: str,
    token: str,
    port: int,
    *,
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    max_concurrent_requests: int = _MAX_CONCURRENT_REQUESTS,
) -> http.server.ThreadingHTTPServer:
    """Start and return the bounded local usage-tracking proxy."""
    if max_request_bytes < 1:
        raise ValueError("max_request_bytes must be positive")
    if max_concurrent_requests < 1:
        raise ValueError("max_concurrent_requests must be positive")

    handler = type(
        "_BoundUsageProxyHandler",
        (_UsageProxyHandler,),
        {
            "endpoint": endpoint,
            "token": token,
            "max_request_bytes": max_request_bytes,
        },
    )
    server = _BoundedThreadingHTTPServer(
        ("127.0.0.1", port),
        handler,
        max_concurrent_requests,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ═══════════════════════════════════════════════════════════════════════════════
# Process management
# ═══════════════════════════════════════════════════════════════════════════════


def _wait_for_health(
    endpoint: str, inference_proc: subprocess.Popen[str], timeout: int
) -> None:
    """Poll the local endpoint until the inference server responds 200."""
    health_url = f"{endpoint}/v1/models"
    deadline = time.time() + timeout

    typer.echo("Waiting for inference server to become healthy...")

    while time.time() < deadline:
        if inference_proc.poll() is not None:
            typer.echo("Inference server exited before becoming healthy.", err=True)
            raise typer.Exit(1)

        try:
            resp = urllib.request.urlopen(health_url, timeout=5)
            if resp.status == 200:
                typer.echo("Inference server is healthy.")
                return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass

        time.sleep(_HEALTH_CHECK_INTERVAL)

    typer.echo("Inference server did not become healthy within timeout.", err=True)
    raise typer.Exit(1)


def _wait_with_heartbeat(
    inference_proc: subprocess.Popen[str],
    connectivity_session: NgrokConnectivity | None,
    token: str,
    cfg: dict[str, Any],
) -> None:
    """Block while inference and connectivity run, sending heartbeats."""
    next_hb = time.time() + _HEARTBEAT_INTERVAL

    while True:
        time.sleep(1)

        if inference_proc.poll() is not None:
            typer.echo("\nInference server exited.", err=True)
            break
        if connectivity_session is not None and not connectivity_session.connected:
            typer.echo("\nngrok connectivity stopped.", err=True)
            break

        now = time.time()
        if now >= next_hb:
            _heartbeat(token, cfg)
            next_hb = now + _HEARTBEAT_INTERVAL


def _cleanup(
    inference_proc: subprocess.Popen[str],
    connectivity_session: NgrokConnectivity | None,
    usage_proxy: http.server.ThreadingHTTPServer | None,
) -> None:
    """Stop connectivity, proxy, then inference without leaking resources."""
    if connectivity_session is not None:
        try:
            connectivity_session.stop()
        except Exception as exc:
            typer.echo(f"Failed to stop ngrok connectivity cleanly: {exc}", err=True)


    if usage_proxy is not None:
        usage_proxy.shutdown()
        usage_proxy.server_close()

    _terminate_process(inference_proc)


def _launch_inference(command: str) -> subprocess.Popen[str]:
    """Launch the operator-supplied command in its own process group."""
    process_options: dict[str, Any] = {
        "shell": True,
        "env": os.environ.copy(),
        "text": True,
    }
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["executable"] = os.environ.get("SHELL", "/bin/sh")
        process_options["start_new_session"] = True
    return subprocess.Popen(command, **process_options)


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    process_group = getattr(proc, "pid", None)
    try:
        if os.name != "nt" and process_group is not None:
            os.killpg(process_group, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt" and process_group is not None:
            os.killpg(process_group, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()


# ═══════════════════════════════════════════════════════════════════════════════
# Serve process tracking (for `ass node stop` / `ass logout`)
# ═══════════════════════════════════════════════════════════════════════════════


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def _write_serve_pid() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SERVE_PID_PATH.write_text(str(os.getpid()))


def _remove_serve_pid() -> None:
    SERVE_PID_PATH.unlink(missing_ok=True)


def _raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt()


def _stop_running_serve(timeout: int = 10) -> bool:
    """Signal a locally running `ass serve` process to shut down gracefully.

    Returns True if a running process was found and signaled.
    """
    if not SERVE_PID_PATH.exists():
        return False

    try:
        pid = int(SERVE_PID_PATH.read_text().strip())
    except (ValueError, OSError):
        SERVE_PID_PATH.unlink(missing_ok=True)
        return False

    if not _pid_alive(pid):
        SERVE_PID_PATH.unlink(missing_ok=True)
        return False

    os.kill(pid, signal.SIGTERM)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.5)

    return True
