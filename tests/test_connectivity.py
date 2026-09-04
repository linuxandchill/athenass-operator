import asyncio
import json
import unittest
from unittest.mock import patch

from ass_node.connectivity import NgrokConnectivity


class FakeListener:
    def __init__(self) -> None:
        self.closed = False

    def close(self):
        asyncio.get_running_loop()

        async def mark_closed() -> None:
            self.closed = True

        return mark_closed()


class NgrokConnectivityTests(unittest.TestCase):
    def test_start_and_stop_manage_one_restricted_listener(self) -> None:
        listener = FakeListener()
        connectivity = NgrokConnectivity(
            agent_authtoken="secret",
            internal_endpoint="https://node-test.internal",
            proxy_port=8932,
        )

        with patch("ass_node.connectivity.ngrok.forward", return_value=listener) as forward:
            self.assertFalse(connectivity.connected)
            connectivity.start()
            self.assertTrue(connectivity.connected)
            forward.assert_called_once()
            args, kwargs = forward.call_args
            self.assertEqual(args, ("http://127.0.0.1:8932",))
            self.assertEqual(kwargs["authtoken"], "secret")
            self.assertEqual(kwargs["domain"], "node-test.internal")
            policy = json.loads(kwargs["traffic_policy"])
            request_rules = policy["on_http_request"]
            self.assertEqual(
                request_rules[0]["expressions"],
                ["req.content_length > 4194304"],
            )
            self.assertIn("/v1/chat/completions", request_rules[1]["expressions"][0])
            self.assertEqual(
                request_rules[3]["actions"][0]["config"]["capacity"],
                60,
            )

            with self.assertRaisesRegex(RuntimeError, "already started"):
                connectivity.start()

            connectivity.stop()
            self.assertTrue(listener.closed)
            self.assertFalse(connectivity.connected)
            connectivity.stop()

    def test_failed_start_does_not_report_connected(self) -> None:
        connectivity = NgrokConnectivity(
            agent_authtoken="secret",
            internal_endpoint="https://node-test.internal",
            proxy_port=8931,
        )

        with patch(
            "ass_node.connectivity.ngrok.forward",
            side_effect=ValueError("ACL denied"),
        ):
            with self.assertRaisesRegex(ValueError, "ACL denied"):
                connectivity.start()

        self.assertFalse(connectivity.connected)

    def test_rejects_non_internal_or_decorated_endpoints(self) -> None:
        invalid_endpoints = (
            "http://node-test.internal",
            "https://node-test.example.com",
            "https://node-test.internal:443",
            "https://node-test.internal/path",
            "https://node-test.internal?query=value",
        )

        for endpoint in invalid_endpoints:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    NgrokConnectivity(
                        agent_authtoken="secret",
                        internal_endpoint=endpoint,
                        proxy_port=8931,
                    )

    def test_rejects_invalid_proxy_ports(self) -> None:
        for proxy_port in (True, 0, 65536):
            with self.subTest(proxy_port=proxy_port):
                with self.assertRaisesRegex(ValueError, "proxy_port"):
                    NgrokConnectivity(
                        agent_authtoken="secret",
                        internal_endpoint="https://node-test.internal",
                        proxy_port=proxy_port,
                    )

    def test_rejects_invalid_security_limits(self) -> None:
        for field, value in (
            ("max_request_bytes", True),
            ("max_request_bytes", 0),
            ("requests_per_minute", True),
            ("requests_per_minute", 0),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, field):
                    NgrokConnectivity(
                        agent_authtoken="secret",
                        internal_endpoint="https://node-test.internal",
                        proxy_port=8931,
                        **{field: value},
                    )


if __name__ == "__main__":
    unittest.main()
