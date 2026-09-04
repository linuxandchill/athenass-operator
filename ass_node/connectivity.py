"""Seller-side connectivity lifecycle."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import ngrok

DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_REQUESTS_PER_MINUTE = 60

_ALLOWED_REQUEST_EXPRESSION = (
    "(req.method == 'GET' && req.url.path == '/v1/models') || "
    "(req.method == 'POST' && req.url.path == '/v1/chat/completions') || "
    "(req.method == 'OPTIONS' && req.url.path == '/v1/chat/completions')"
)


def _build_traffic_policy(
    max_request_bytes: int,
    requests_per_minute: int,
) -> str:
    """Build defense-in-depth policy for the private Agent Endpoint."""
    policy = {
        "on_http_request": [
            {
                "name": "Reject oversized inference requests",
                "expressions": [f"req.content_length > {max_request_bytes}"],
                "actions": [{"type": "deny", "config": {"status_code": 413}}],
            },
            {
                "name": "Restrict the public inference API",
                "expressions": [f"!({_ALLOWED_REQUEST_EXPRESSION})"],
                "actions": [{"type": "deny", "config": {"status_code": 404}}],
            },
            {
                "name": "Rate limit unauthenticated requests",
                "expressions": ["!('Authorization' in req.headers)"],
                "actions": [
                    {
                        "type": "rate-limit",
                        "config": {
                            "name": "Unauthenticated requests",
                            "algorithm": "sliding_window",
                            "capacity": 10,
                            "rate": "60s",
                            "bucket_key": ["conn.client_ip"],
                        },
                    }
                ],
            },
            {
                "name": "Rate limit each reservation",
                "expressions": ["'Authorization' in req.headers"],
                "actions": [
                    {
                        "type": "rate-limit",
                        "config": {
                            "name": "Reservation requests",
                            "algorithm": "sliding_window",
                            "capacity": requests_per_minute,
                            "rate": "60s",
                            "bucket_key": ["req.headers['authorization']"],
                        },
                    }
                ],
            },
        ]
    }
    return json.dumps(policy, separators=(",", ":"))


class NgrokConnectivity:
    """Bind one assigned Internal Endpoint to the AthenaSS usage proxy."""

    def __init__(
        self,
        *,
        agent_authtoken: str,
        internal_endpoint: str,
        proxy_port: int,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
    ) -> None:
        if not agent_authtoken:
            raise ValueError("agent_authtoken must not be empty")
        if isinstance(proxy_port, bool) or not 1 <= proxy_port <= 65535:
            raise ValueError("proxy_port must be between 1 and 65535")
        if isinstance(max_request_bytes, bool) or max_request_bytes < 1:
            raise ValueError("max_request_bytes must be positive")
        if isinstance(requests_per_minute, bool) or requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")

        parsed_endpoint = urlparse(internal_endpoint)
        if parsed_endpoint.scheme != "https" or not parsed_endpoint.hostname:
            raise ValueError("internal_endpoint must be an HTTPS URL")
        if not parsed_endpoint.hostname.endswith(".internal"):
            raise ValueError("internal_endpoint must use the .internal suffix")
        if (
            parsed_endpoint.username
            or parsed_endpoint.password
            or parsed_endpoint.port
            or parsed_endpoint.path not in ("", "/")
            or parsed_endpoint.params
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise ValueError("internal_endpoint must contain only its HTTPS hostname")

        self._agent_authtoken = agent_authtoken
        self._internal_domain = parsed_endpoint.hostname
        self._upstream_url = f"http://127.0.0.1:{proxy_port}"
        self._traffic_policy = _build_traffic_policy(
            max_request_bytes,
            requests_per_minute,
        )
        self._listener: ngrok.Listener | None = None

    @property
    def connected(self) -> bool:
        """Whether this instance successfully started a listener."""
        return self._listener is not None

    @property
    def endpoint(self) -> str:
        return f"https://{self._internal_domain}"

    def start(self) -> None:
        if self._listener is not None:
            raise RuntimeError("ngrok connectivity is already started")

        self._listener = ngrok.forward(
            self._upstream_url,
            authtoken=self._agent_authtoken,
            domain=self._internal_domain,
            traffic_policy=self._traffic_policy,
        )

    def stop(self) -> None:
        listener = self._listener
        if listener is None:
            return

        try:
            asyncio.run(self._close(listener))
        finally:
            self._listener = None

    @staticmethod
    async def _close(listener: ngrok.Listener) -> None:
        await listener.close()