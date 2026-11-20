"""FastAPI reverse proxy for the MCP Streamable HTTP transport.

Claude Web's connectors are called from Anthropic's cloud, so the gateway sits in front of
the MCP server (exposed via a tunnel such as ngrok):

    Anthropic cloud -> ngrok -> gateway /mcp/{secret} -> upstream MCP server

`tools/call` requests are held until the daemon decides. Everything else (initialize,
tools/list, notifications, the GET SSE stream, DELETE) is proxied through unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from mcp_guard.core import config, daemon_client, jsonrpc
from mcp_guard.core.models import ApprovalDecision, ApprovalRequest

log = logging.getLogger(__name__)

# Headers that must not be copied between hops.
_HOP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "te", "upgrade", "proxy-authorization", "proxy-connection", "accept-encoding",
}


def load_or_create_secret() -> str:
    """Secret path segment that protects the public gateway URL."""
    path = config.data_dir() / "gateway_secret"
    if path.exists() and (value := path.read_text(encoding="utf-8").strip()):
        return value
    value = secrets.token_urlsafe(18)
    path.write_text(value, encoding="utf-8")
    return value


def _forward_headers(headers: Any) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_HEADERS}


def _answer_all(
    message: Any, reply_for: Callable[[dict[str, Any]], dict[str, Any]]
) -> JSONResponse:
    """Reply to every request in `message` (single or batch) without contacting the server.

    Guarantees no request id in the message is left unanswered.
    """
    if isinstance(message, list):
        return JSONResponse([reply_for(m) for m in message if jsonrpc.is_request(m)])
    return JSONResponse(reply_for(message))


async def _decide_unless_disconnected(
    request: Request, http: httpx.AsyncClient, approvals: list[ApprovalRequest]
) -> list[ApprovalDecision] | None:
    """Ask the daemon about every request at once; return None if the client disconnects.

    Cancelling the pending tasks closes their connections to the daemon, which notices the
    disconnect and closes the popups.
    """
    tasks = [
        asyncio.create_task(daemon_client.request_decision(http, a)) for a in approvals
    ]
    pending: set[asyncio.Task[ApprovalDecision]] = set(tasks)
    while pending:
        _, pending = await asyncio.wait(pending, timeout=config.DISCONNECT_POLL_S)
        if pending and await request.is_disconnected():
            for task in pending:
                task.cancel()
            return None
    return [task.result() for task in tasks]


def create_app(
    upstream_url: str,
    secret: str,
    server_name: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the gateway app proxying `/mcp/{secret}` to `upstream_url`.

    `transport` lets tests substitute a fake upstream.
    """
    http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10), transport=transport)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await http.aclose()

    app = FastAPI(title="MCP Guard Gateway", lifespan=lifespan)

    def check(given: str) -> None:
        if not secrets.compare_digest(given, secret):
            raise HTTPException(status_code=404)

    async def forward(request: Request, body: bytes | None) -> Response:
        upstream = http.build_request(
            request.method,
            upstream_url,
            params=request.query_params,
            headers=_forward_headers(request.headers),
            content=body,
        )
        try:
            resp = await http.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            log.error("upstream unreachable: %s", exc)
            return JSONResponse(
                jsonrpc.error_response(None, -32603, "MCP server unreachable"), status_code=502
            )
        return StreamingResponse(
            resp.aiter_raw(),  # works for both application/json and text/event-stream
            status_code=resp.status_code,
            headers=_forward_headers(resp.headers),
            background=BackgroundTask(resp.aclose),
        )

    @app.post("/mcp/{given_secret}")
    async def post(given_secret: str, request: Request) -> Response:
        check(given_secret)
        body = await request.body()
        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return await forward(request, body)  # let the server reject it

        items = message if isinstance(message, list) else [message]
        gated = [
            m for m in items
            if jsonrpc.is_request(m) and m["method"] in config.INTERCEPTED_METHODS
        ]
        if not gated:
            return await forward(request, body)

        requests: list[ApprovalRequest] = []
        for item in gated:
            try:
                tool_name, arguments = jsonrpc.tool_call_fields(item)
            except ValueError as exc:
                reason = str(exc)
                return _answer_all(message, lambda m: jsonrpc.invalid_params_response(m, reason))
            requests.append(
                ApprovalRequest(
                    client="remote-connector",
                    server_name=server_name,
                    method=item["method"],
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )

        # Ask about every call in parallel so each gets its popup at once.
        decisions = await _decide_unless_disconnected(request, http, requests)
        if decisions is None:
            log.info("client disconnected while awaiting approval")
            return _answer_all(message, lambda m: jsonrpc.blocked_response(m, "Client gone."))

        blocked = [d for d in decisions if d.decision == "block"]
        if not blocked:
            return await forward(request, body)
        if isinstance(message, list):
            # Never forward part of a batch: answer every request in it as blocked.
            detail = "Blocked: this batch contained a request the user blocked (MCP Guard)."
        else:
            detail = daemon_client.block_detail(blocked[0])
        return _answer_all(message, lambda m: jsonrpc.blocked_response(m, detail))

    @app.get("/mcp/{given_secret}")
    async def get(given_secret: str, request: Request) -> Response:
        check(given_secret)
        return await forward(request, None)

    @app.delete("/mcp/{given_secret}")
    async def delete(given_secret: str, request: Request) -> Response:
        check(given_secret)
        return await forward(request, None)

    return app


def run_gateway(upstream_url: str, port: int, server_name: str) -> None:
    """Run the gateway on 127.0.0.1:`port` (expose it with ngrok)."""
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    secret = load_or_create_secret()
    print(f"MCP Guard gateway -> {upstream_url}")
    print(f"Local URL : http://127.0.0.1:{port}/mcp/{secret}")
    print(f"Expose it : ngrok http {port}")
    print(f"Connector : https://<your-ngrok-domain>/mcp/{secret}")
    # access_log=False: uvicorn's access log would write the secret path on every request.
    uvicorn.run(
        create_app(upstream_url, secret, server_name),
        host="127.0.0.1",
        port=port,
        access_log=False,
    )
