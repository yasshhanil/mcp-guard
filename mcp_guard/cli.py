"""`mcp-guard` command-line entry point."""

from __future__ import annotations

import argparse
import sys

import httpx

from mcp_guard.core import config


def _status() -> int:
    token = config.read_token()
    try:
        resp = httpx.get(
            f"{config.DAEMON_URL}/v1/health", headers={"X-Guard-Token": token or ""}, timeout=2
        )
        print(f"daemon: {resp.status_code} {resp.text}")
        return 0 if resp.is_success else 1
    except httpx.HTTPError:
        print(f"daemon: not running at {config.DAEMON_URL}")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-guard", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="run the background approval service")
    p_install = sub.add_parser("install", help="route Claude Desktop MCP servers through MCP Guard")
    p_install.add_argument("--demo", action="store_true", help="also add the demo MCP server")
    sub.add_parser("uninstall", help="restore the original Claude Desktop config")
    sub.add_parser("status", help="check whether the daemon is running")
    p_gw = sub.add_parser("gateway", help="run the HTTP gateway for Claude Web connectors")
    p_gw.add_argument("--upstream", default="http://127.0.0.1:9000/mcp", help="real MCP server URL")
    p_gw.add_argument("--port", type=int, default=8080)
    p_gw.add_argument("--name", default="remote", help="server name shown in the popup")
    args = parser.parse_args(argv)

    if args.cmd == "gateway":
        from mcp_guard.gateway.app import run_gateway

        run_gateway(args.upstream, args.port, args.name)
        return 0

    if args.cmd == "daemon":
        from mcp_guard.daemon.service import run_daemon

        run_daemon()
        return 0

    from mcp_guard.installer import desktop_config

    if args.cmd == "install":
        names = desktop_config.install(add_demo=args.demo)
        print(f"config: {desktop_config.find_config_path()}")
        print(f"guarded servers: {', '.join(names) or '(none)'}")
        print("Restart Claude Desktop (quit from the tray) to apply.")
        return 0
    if args.cmd == "uninstall":
        names = desktop_config.uninstall()
        print(f"restored: {', '.join(names) or '(nothing to restore)'}")
        print("Restart Claude Desktop to apply.")
        return 0
    return _status()


if __name__ == "__main__":
    sys.exit(main())
