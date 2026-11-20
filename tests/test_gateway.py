"""HTTP gateway: secret check, allow/block, batches, malformed input. Upstream and daemon faked."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mcp_guard.core import daemon_client
from mcp_guard.core.models import ApprovalDecision, ApprovalRequest
from mcp_guard.gateway.app import create_app

SECRET = "s3cret"


class Upstream:
    """Fake MCP server that records what reached it."""

    def __init__(self) -> None:
        self.received: list[Any] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.received.append(body)
        reply = {"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                 "result": {"ok": True}}
        # A real stream (not pre-read content), as a real upstream would send.
        return httpx.Response(
            200,
            stream=httpx.ByteStream(json.dumps(reply).encode()),
            headers={"Mcp-Session-Id": "abc", "Content-Type": "application/json"},
        )


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def client(upstream: Upstream, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    async def request_decision(_http: Any, req: ApprovalRequest) -> ApprovalDecision:
        verdict = "block" if req.tool_name == "delete_file" else "allow"
        return ApprovalDecision(request_id=req.request_id, decision=verdict, reason="user")

    monkeypatch.setattr(daemon_client, "request_decision", request_decision)
    app = create_app("http://upstream/mcp", SECRET, "demo-web",
                     transport=httpx.MockTransport(upstream.handler))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


def call(id_: int, name: str, params: Any = None) -> dict[str, Any]:
    params = params if params is not None else {"name": name, "arguments": {}}
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": params}


async def test_wrong_secret_is_404(client: httpx.AsyncClient, upstream: Upstream) -> None:
    resp = await client.post("/mcp/wrong", json=call(1, "read_file"))
    assert resp.status_code == 404
    assert upstream.received == []


async def test_allowed_call_is_forwarded(client: httpx.AsyncClient, upstream: Upstream) -> None:
    resp = await client.post(f"/mcp/{SECRET}", json=call(1, "read_file"))
    assert resp.json()["result"] == {"ok": True}
    assert resp.headers["mcp-session-id"] == "abc"
    assert upstream.received[0]["id"] == 1


async def test_blocked_call_never_reaches_server(
    client: httpx.AsyncClient, upstream: Upstream
) -> None:
    resp = await client.post(f"/mcp/{SECRET}", json=call(2, "delete_file"))
    assert resp.json()["result"]["isError"] is True
    assert upstream.received == []


async def test_batch_with_a_block_answers_every_request(
    client: httpx.AsyncClient, upstream: Upstream
) -> None:
    batch = [call(1, "read_file"), call(2, "delete_file"),
             {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
             {"jsonrpc": "2.0", "method": "notifications/progress"}]
    replies = (await client.post(f"/mcp/{SECRET}", json=batch)).json()
    assert sorted(r["id"] for r in replies) == [1, 2, 3]  # notification needs no reply
    assert upstream.received == []


async def test_malformed_call_gets_invalid_params(
    client: httpx.AsyncClient, upstream: Upstream
) -> None:
    resp = await client.post(f"/mcp/{SECRET}", json=call(4, "x", params=["oops"]))
    assert resp.json()["error"]["code"] == -32602
    assert upstream.received == []
