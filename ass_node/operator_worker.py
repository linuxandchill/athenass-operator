"""Command bridge used by the localhost AthenaSS Operator process manager."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ass_node import cli
from ass_node.operator_service import (
    OperatorError,
    begin_login,
    get_state,
    list_nodes,
    poll_login,
    register_node,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="athenass-operator-worker")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("state")
    commands.add_parser("login-start")

    login_poll = commands.add_parser("login-poll")
    login_poll.add_argument("device_code")

    register = commands.add_parser("register")
    register.add_argument("name")
    register.add_argument("model_id")
    register.add_argument("command")
    register.add_argument("port", type=int)

    commands.add_parser("list")
    commands.add_parser("serve")
    commands.add_parser("stop")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.action == "state":
            _print_json(get_state())
        elif args.action == "login-start":
            _print_json(begin_login())
        elif args.action == "login-poll":
            _print_json(poll_login(args.device_code))
        elif args.action == "register":
            _print_json(
                register_node(args.name, args.model_id, args.command, args.port)
            )
        elif args.action == "list":
            _print_json({"nodes": list_nodes()})
        elif args.action == "serve":
            cli.serve(local=False, timeout=300, usage_proxy_port=cli._USAGE_PROXY_PORT)
        elif args.action == "stop":
            _print_json({"stopped": cli._stop_running_serve()})
    except OperatorError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except (KeyError, TypeError, ValueError) as exc:
        print(
            f"Invalid AthenaSS (A77) response or configuration: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
