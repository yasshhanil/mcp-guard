"""Stdio proxy launched by Claude Desktop in place of a real MCP server.

Usage (written into claude_desktop_config.json by `mcp-guard install`):

    python -m mcp_guard.proxy.stdio --name <server> -- <real command> [args...]

Claude -> proxy -> child: intercepted requests are held until the daemon decides;
everything else is forwarded immediately. child -> proxy -> Claude: copied unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import threading
from typing import Any

import httpx

from mcp_guard.core import config, daemon_client, jsonrpc
from mcp_guard.core.models import ApprovalRequest

log = logging.getLogger("mcp_guard.proxy")


class StdioProxy:
    """Bridges Claude's stdio to a child MCP server, gating intercepted requests."""

    def __init__(self, server_name: str, command: list[str]) -> None:
        self.server_name = server_name
        self.command = command
        self.child: asyncio.subprocess.Process | None = None
        self.http: httpx.AsyncClient | None = None
        # JSON-RPC id -> (task waiting for a decision, daemon request id)
        self.held: dict[Any, tuple[asyncio.Task[None], str]] = {}

    # ---------- output helpers ----------

    @staticmethod
    def to_claude(data: bytes) -> None:
        """Write raw bytes to Claude (our stdout). Only called from the event loop thread."""
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def to_child(self, data: bytes) -> None:
        assert self.child is not None and self.child.stdin is not None
        self.child.stdin.write(data)
        await self.child.stdin.drain()

    # ---------- approval ----------

    async def cancel_on_daemon(self, daemon_id: str) -> None:
        if self.http is not None:
            await daemon_client.cancel_request(self.http, daemon_id)

    async def gate(self, message: dict[str, Any], raw: bytes, req: ApprovalRequest) -> None:
        """Hold one request until decided, then forward it or answer with a block."""
        try:
            assert self.http is not None
            decision = await daemon_client.request_decision(self.http, req)
            if decision.decision == "allow":
                await self.to_child(raw)
            else:
                detail = daemon_client.block_detail(decision)
                self.to_claude(jsonrpc.encode(jsonrpc.blocked_response(message, detail)))
        except asyncio.CancelledError:
            pass  # client cancelled; MCP says no response is required
        except Exception:  # last-resort: never leave the id unanswered
            log.exception("error while gating request")
            self.to_claude(jsonrpc.encode(jsonrpc.blocked_response(message)))
        finally:
            self.held.pop(message.get("id"), None)

    # ---------- message routing ----------

    async def handle_from_claude(self, raw: bytes) -> None:
        message = jsonrpc.parse_line(raw)
        items = message if isinstance(message, list) else [message]
        for item in items:
            item_raw = raw if not isinstance(message, list) else jsonrpc.encode(item)
            await self.route(item, item_raw)

    async def route(self, message: Any, raw: bytes) -> None:
        if jsonrpc.is_request(message) and message["method"] in config.INTERCEPTED_METHODS:
            try:
                tool_name, arguments = jsonrpc.tool_call_fields(message)
            except ValueError as exc:
                # Malformed: answer it ourselves; never forward it, never crash.
                self.to_claude(jsonrpc.encode(jsonrpc.invalid_params_response(message, str(exc))))
                return
            req = ApprovalRequest(
                server_name=self.server_name,
                method=message["method"],
                tool_name=tool_name,
                arguments=arguments,
            )
            task = asyncio.create_task(self.gate(message, raw, req))
            self.held[message["id"]] = (task, req.request_id)
            return

        if isinstance(message, dict) and message.get("method") == "notifications/cancelled":
            params = message.get("params")
            target = params.get("requestId") if isinstance(params, dict) else None
            if jsonrpc.is_valid_id(target) and target in self.held:
                task, daemon_id = self.held.pop(target)
                task.cancel()
                await self.cancel_on_daemon(daemon_id)
                return  # the server never saw the request, so don't forward the cancel

        await self.to_child(raw)

    async def pump_child_stdout(self) -> None:
        assert self.child is not None and self.child.stdout is not None
        while True:
            line = await self.child.stdout.readline()
            if not line:
                break
            self.to_claude(line)

    # ---------- lifecycle ----------

    async def run(self) -> int:
        executable = shutil.which(self.command[0]) or self.command[0]
        self.child = await asyncio.create_subprocess_exec(
            executable,
            *self.command[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
        )
        self.http = httpx.AsyncClient()
        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue[bytes | None] = asyncio.Queue()

        def read_stdin() -> None:
            # Blocking reads in a thread: portable across Windows pipe types.
            for line in iter(sys.stdin.buffer.readline, b""):
                loop.call_soon_threadsafe(inbox.put_nowait, line)
            loop.call_soon_threadsafe(inbox.put_nowait, None)

        threading.Thread(target=read_stdin, name="stdin-reader", daemon=True).start()
        child_pump = asyncio.create_task(self.pump_child_stdout())

        async def consume() -> None:
            while (line := await inbox.get()) is not None:
                if not line.strip():
                    continue
                try:
                    await self.handle_from_claude(line)
                except Exception:  # one bad message must not take the server down
                    log.exception("failed to handle message from Claude; dropped it")

        consumer = asyncio.create_task(consume())
        done, _ = await asyncio.wait({consumer, child_pump}, return_when=asyncio.FIRST_COMPLETED)

        if child_pump in done:
            log.error("MCP server '%s' exited", self.server_name)
            for request_id, (task, _daemon_id) in list(self.held.items()):
                task.cancel()
                self.to_claude(
                    jsonrpc.encode(
                        jsonrpc.error_response(request_id, -32603, "MCP server exited")
                    )
                )
        await self.shutdown()
        return 0 if consumer in done else 1

    async def shutdown(self) -> None:
        for task, daemon_id in list(self.held.values()):
            task.cancel()
            await self.cancel_on_daemon(daemon_id)
        if self.child is not None and self.child.returncode is None:
            if self.child.stdin is not None:
                self.child.stdin.close()
            try:
                await asyncio.wait_for(self.child.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.child.kill()
        if self.http is not None:
            await self.http.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_guard.proxy.stdio")
    parser.add_argument("--name", required=True, help="MCP server name shown in the popup")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> [args...]")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing server command after --")
    # Logs go to stderr; Claude Desktop shows them in its MCP log files.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="[mcp-guard] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(StdioProxy(args.name, command).run())


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Hard exit: the stdin reader thread may still be blocked in readline(), which makes
    # normal interpreter shutdown abort on Windows.
    os._exit(code)
