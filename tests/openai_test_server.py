"""Deterministic OpenAI-compatible server for multi-node routing tests."""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class OpenAITestHandler(BaseHTTPRequestHandler):
    label = "node"
    model = "routing-test-model"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") != "/v1/models":
            self._send_json({"error": "not found"}, 404)
            return
        self._send_json(
            {
                "object": "list",
                "data": [
                    {
                        "id": self.model,
                        "object": "model",
                        "owned_by": self.label,
                    }
                ],
            }
        )

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            request = json.loads(self.rfile.read(length) if length else b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON"}, 400)
            return

        if request.get("stream"):
            self._send_stream()
            return

        self._send_json(
            {
                "id": f"chatcmpl-{self.label}",
                "object": "chat.completion",
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": f"response from {self.label}",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }
        )

    def _send_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        chunks = (
            {
                "object": "chat.completion.chunk",
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {"role": "assistant", "content": "response "},
                    }
                ],
            },
            {
                "object": "chat.completion.chunk",
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {"content": f"from {self.label}"},
                    }
                ],
            },
            {
                "object": "chat.completion.chunk",
                "model": self.model,
                "choices": [],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        )
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.1)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_json(self, body: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--label", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    handler = type(
        "BoundOpenAITestHandler",
        (OpenAITestHandler,),
        {"label": args.label, "model": f"{args.label}-model"},
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(
        f"OpenAI test server {args.label} listening on http://127.0.0.1:{args.port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
