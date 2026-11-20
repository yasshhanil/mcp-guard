"""Stdio proxy routing: malformed input, allow/block, cancellation. No real child or daemon."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from mcp_guard.core import daemon_client
from mcp_guard.core.models import ApprovalDecision, ApprovalRequest
from mcp_guard.proxy.stdio import StdioProxy


class FakeProxy(StdioProxy):
    """Captures output instead of writing to stdout / a child process."""

    def __init__(self) -> None:
        super().__init__("demo", ["unused"])
        self.http = object()  # type: ignore[assignment]  # never used: daemon is faked
        self.to_claude_log: list[dict[str, Any]] = []
        self.to_child_log: list[dict[str, Any]] = []

    def to_claude(self, data: bytes) -> None:  # type: ignore[override]
        self.to_claude_log.append(json.loads(data))

    async def to_child(self, data: bytes) -> None:
        self.to_child_log.append(json.loads(data))

    async def cancel_on_daemon(self, daemon_id: str) -> None:
        self.cancelled = daemon_id


def fake_daemon(monkeypatch: pytest.MonkeyPatch, decide: Any) -> None:
    async def request_decision(_http: Any, req: ApprovalRequest) -> ApprovalDecision:
        return await decide(req)

    monkeypatch.setattr(daemon_client, "request_decision", request_decision)


def call(id_: Any, name: str = "delete_file", params: Any = None) -> dict[str, Any]:
    if params is None:
        params = {"name": name, "arguments": {"name": "x.txt"}}
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": params}


async def route(proxy: FakeProxy, message: Any) -> None:
    await proxy.route(message, (json.dumps(message) + "\n").encode())


@pytest.mark.parametrize(
    "params",
    [["oops"], {"name": 5}, {"name": "t", "arguments": "nope"}],
    ids=["params-list", "name-not-str", "arguments-not-object"],
)
async def test_malformed_call_is_answered_not_forwarded(params: Any) -> None:
    proxy = FakeProxy()
    await route(proxy, call(7, params=params))
    assert proxy.to_child_log == []
    assert proxy.to_claude_log[0]["id"] == 7
    assert proxy.to_claude_log[0]["error"]["code"] == -32602


async def test_invalid_id_does_not_crash() -> None:
    proxy = FakeProxy()
    await route(proxy, call({"weird": "id"}))
    await route(proxy, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": ["x"]}})
    assert proxy.to_claude_log[0]["error"]["code"] == -32602
    assert proxy.to_claude_log[0]["id"] is None


async def test_allow_forwards_and_block_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def decide(req: ApprovalRequest) -> ApprovalDecision:
        verdict = "allow" if req.tool_name == "read_file" else "block"
        return ApprovalDecision(request_id=req.request_id, decision=verdict, reason="user")

    fake_daemon(monkeypatch, decide)
    proxy = FakeProxy()
    await route(proxy, call(1, "read_file"))
    await route(proxy, call(2, "delete_file"))
    await asyncio.gather(*(task for task, _ in proxy.held.values()))
    assert [m["id"] for m in proxy.to_child_log] == [1]
    assert proxy.to_claude_log[0]["id"] == 2
    assert proxy.to_claude_log[0]["result"]["isError"] is True
    assert proxy.held == {}


async def test_non_intercepted_passes_through() -> None:
    proxy = FakeProxy()
    await route(proxy, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert proxy.to_child_log[0]["method"] == "tools/list"


async def test_cancel_drops_held_request(monkeypatch: pytest.MonkeyPatch) -> None:
    never = asyncio.Event()

    async def decide(req: ApprovalRequest) -> ApprovalDecision:
        await never.wait()
        raise AssertionError("unreachable")

    fake_daemon(monkeypatch, decide)
    proxy = FakeProxy()
    await route(proxy, call(9))
    await asyncio.sleep(0)
    _, daemon_id = proxy.held[9]
    await route(proxy, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 9}})
    await asyncio.sleep(0)
    assert proxy.held == {}
    assert proxy.cancelled == daemon_id
    assert proxy.to_child_log == [] and proxy.to_claude_log == []
