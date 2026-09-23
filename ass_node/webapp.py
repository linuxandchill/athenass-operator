"""Secured localhost web application for operating an AthenaSS (A77) node."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ass_node.cli import SERVE_PID_PATH
from ass_node.operator_service import (
    OperatorError,
    begin_login,
    delete_node,
    get_state,
    list_nodes,
    poll_login,
    register_node,
    sign_out,
    update_node,
)

_SESSION_TOKEN = secrets.token_urlsafe(32)
_WEB_ROOT = files("ass_node").joinpath("web")


class RegisterRequest(BaseModel):
    name: str
    model_id: str
    command: str
    port: int = Field(ge=1, le=65535)
    machine_info_json: str = Field(min_length=1, max_length=16_000)

    def machine_info(self) -> dict[str, Any]:
        try:
            value = json.loads(self.machine_info_json)
        except json.JSONDecodeError as exc:
            raise OperatorError("Machine details must contain valid JSON") from exc
        if not isinstance(value, dict):
            raise OperatorError("Machine details must be a JSON object")
        if not value:
            raise OperatorError("Machine details must include at least one field")
        return value

class UpdateNodeRequest(RegisterRequest):
    node_id: str


class LoginPollRequest(BaseModel):
    device_code: str

class NodeDeleteRequest(BaseModel):
    name: str


class NodeStopRequest(BaseModel):
    force: bool = False


class NodeProcessManager:
    """Tracks the node worker process and its bounded local log stream."""

    def __init__(self, max_log_lines: int = 2000) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[tuple[int, str]] = deque(maxlen=max_log_lines)
        self._next_log_id = 1
        self._last_exit_code: int | None = None

    def _append_log(self, line: str) -> None:
        with self._lock:
            self._logs.append((self._next_log_id, line.rstrip("\n")))
            self._next_log_id += 1

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _persisted_pid(self, path: Path | None = None) -> int | None:
        path = path or SERVE_PID_PATH
        try:
            pid = int(path.read_text().strip())
        except (FileNotFoundError, OSError, ValueError):
            return None
        if self._pid_alive(pid):
            return pid
        path.unlink(missing_ok=True)
        return None

    def _stop_persisted_inference(self) -> None:
        path = SERVE_PID_PATH.with_name("inference.pid")
        pid = self._persisted_pid(path)
        if pid is None:
            return
        try:
            if os.name == "nt":
                os.kill(pid, signal.SIGTERM)
            else:
                os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            path.unlink(missing_ok=True)
            return
        deadline = time.monotonic() + 5
        while self._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._pid_alive(pid):
            if os.name == "nt":
                os.kill(pid, signal.SIGKILL)
            else:
                os.killpg(pid, signal.SIGKILL)
        path.unlink(missing_ok=True)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.status()["running"]:
                raise OperatorError("The configured Endpoint is already running")
            operator_state = get_state()
            if not operator_state["authenticated"]:
                raise OperatorError("Sign in before starting the Endpoint")
            if not operator_state["configured"]:
                raise OperatorError("Register an Endpoint before starting it")

            creation: dict[str, Any] = {}
            if os.name == "nt":
                creation["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                creation["start_new_session"] = True
            process = subprocess.Popen(
                [sys.executable, "-m", "ass_node.operator_worker", "serve"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **creation,
            )
            self._process = process
            self._last_exit_code = None
            self._logs.clear()
            self._next_log_id = 1

        self._append_log("AthenaSS Operator (A77) started the Endpoint worker.")
        threading.Thread(
            target=self._capture_output,
            args=(process,),
            name="athenass-node-logs",
            daemon=True,
        ).start()
        return self.status()

    def _capture_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                self._append_log(line)
        exit_code = process.wait()
        with self._lock:
            self._last_exit_code = exit_code
            if self._process is process:
                self._process = None
        self._append_log(f"Endpoint worker exited with status {exit_code}.")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            persisted_pid = self._persisted_pid()
        if process is not None and process.poll() is None:
            self._append_log("AthenaSS Operator (A77) requested Endpoint shutdown.")
            try:
                if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                self._stop_persisted_inference()
            return self.status()

        if persisted_pid is None:
            self._stop_persisted_inference()
            return self.status()

        self._append_log(
            "AthenaSS Operator (A77) found an existing Endpoint worker and "
            "requested shutdown."
        )
        try:
            os.kill(persisted_pid, signal.SIGTERM)
        except ProcessLookupError:
            SERVE_PID_PATH.unlink(missing_ok=True)
            self._stop_persisted_inference()
            return self.status()

        deadline = time.monotonic() + 15
        while self._pid_alive(persisted_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self._pid_alive(persisted_pid):
            os.kill(persisted_pid, signal.SIGKILL)
            self._stop_persisted_inference()
        SERVE_PID_PATH.unlink(missing_ok=True)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                running_pid = process.pid
            else:
                running_pid = self._persisted_pid()
                if running_pid is None:
                    running_pid = self._persisted_pid(
                        SERVE_PID_PATH.with_name("inference.pid")
                    )
            return {
                "running": running_pid is not None,
                "pid": running_pid,
                "last_exit_code": self._last_exit_code,
            }

    def logs(self, after: int) -> dict[str, Any]:
        with self._lock:
            lines = [
                {"id": log_id, "line": line}
                for log_id, line in self._logs
                if log_id > after
            ]
            cursor = self._logs[-1][0] if self._logs else after
        return {"cursor": cursor, "lines": lines, **self.status()}


process_manager = NodeProcessManager()


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    process_manager.stop()


app = FastAPI(
    title="AthenaSS Operator (A77)",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


def _require_session(
    x_athenass_session: Annotated[str | None, Header()] = None,
) -> None:
    if x_athenass_session is None or not secrets.compare_digest(
        x_athenass_session, _SESSION_TOKEN
    ):
        raise HTTPException(status_code=403, detail="Invalid local operator session")


def _api_error(exc: OperatorError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    template = _WEB_ROOT.joinpath("index.html").read_text(encoding="utf-8")
    content = template.replace("__ATHENA_SESSION_TOKEN__", _SESSION_TOKEN)
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/app.js")
def javascript() -> FileResponse:
    return FileResponse(
        _WEB_ROOT.joinpath("app.js"),
        media_type="text/javascript",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(
        _WEB_ROOT.joinpath("styles.css"),
        media_type="text/css",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/healthz")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/state", dependencies=[Depends(_require_session)])
def state() -> dict[str, Any]:
    return {**get_state(), "runner": process_manager.status()}


@app.post("/api/login/start", dependencies=[Depends(_require_session)])
def login_start() -> Any:
    try:
        result = begin_login(open_browser=True)
        verification_url = result.get("verification_url")
        if isinstance(verification_url, str):
            print(f"Authorize AthenaSS Operator: {verification_url}", flush=True)
        return result
    except OperatorError as exc:
        return _api_error(exc)


@app.post("/api/login/poll", dependencies=[Depends(_require_session)])
def login_poll(request: LoginPollRequest) -> Any:
    try:
        return poll_login(request.device_code)
    except OperatorError as exc:
        return _api_error(exc)

@app.post("/api/logout", dependencies=[Depends(_require_session)])
def logout() -> Any:
    try:
        return sign_out()
    except OperatorError as exc:
        return _api_error(exc)


@app.post("/api/nodes/register", dependencies=[Depends(_require_session)])
def node_register(request: RegisterRequest) -> Any:
    try:
        return register_node(
            request.name,
            request.model_id,
            request.command,
            request.port,
            request.machine_info(),
        )
    except OperatorError as exc:
        return _api_error(exc)

@app.post("/api/nodes/update", dependencies=[Depends(_require_session)])
def node_update(request: UpdateNodeRequest) -> Any:
    try:
        if process_manager.status()["running"]:
            raise OperatorError("Stop the Endpoint before editing its configuration")
        return update_node(
            request.node_id,
            request.name,
            request.model_id,
            request.command,
            request.port,
            request.machine_info(),
        )
    except OperatorError as exc:
        return _api_error(exc)


@app.get("/api/nodes", dependencies=[Depends(_require_session)])
def nodes() -> Any:
    try:
        return {"nodes": list_nodes()}
    except OperatorError as exc:
        return _api_error(exc)

@app.post("/api/nodes/delete", dependencies=[Depends(_require_session)])
def node_delete(request: NodeDeleteRequest) -> Any:
    try:
        current_node = get_state().get("node")
        if isinstance(current_node, dict) and current_node.get("name") == request.name:
            process_manager.stop()
        return delete_node(request.name)
    except OperatorError as exc:
        return _api_error(exc)


@app.post("/api/node/start", dependencies=[Depends(_require_session)])
def node_start() -> Any:
    try:
        return process_manager.start()
    except OperatorError as exc:
        return _api_error(exc)


@app.post("/api/node/stop", dependencies=[Depends(_require_session)])
def node_stop(request: NodeStopRequest) -> Any:
    if process_manager.status()["running"] and not request.force:
        try:
            current = get_state().get("node") or {}
            owned_node = next(
                (node for node in list_nodes() if node.get("id") == current.get("id")),
                None,
            )
            reserved = owned_node.get("reserved") if owned_node else None
        except OperatorError:
            reserved = None
        if reserved is not False:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "node_in_use" if reserved is True else "reservation_unknown",
                    "error": (
                        "This Endpoint is In Use. Stopping now will interrupt the renter's "
                        "reservation and any requests in progress."
                        if reserved is True
                        else "Reservation status could not be verified. Stopping now "
                        "may interrupt a renter's reservation and requests."
                    ),
                },
            )
    return process_manager.stop()


@app.get("/api/node/logs", dependencies=[Depends(_require_session)])
def node_logs(after: int = 0) -> dict[str, Any]:
    return process_manager.logs(max(after, 0))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run AthenaSS Operator (A77) on localhost"
    )
    parser.add_argument("--port", type=int, default=8930)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    url = f"http://127.0.0.1:{args.port}"
    print(f"AthenaSS Operator (A77): {url}", flush=True)
    print("Press Ctrl+C to stop the local operator service.", flush=True)
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
