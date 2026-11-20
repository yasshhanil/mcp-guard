"""FastAPI app exposing the broker to interceptors on 127.0.0.1."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from mcp_guard.core import config
from mcp_guard.core.models import ApprovalDecision, ApprovalRequest
from mcp_guard.daemon.broker import Broker


def create_app(broker: Broker, token: str) -> FastAPI:
    """Build the daemon API. Every endpoint requires the `X-Guard-Token` header."""

    def require_token(x_guard_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_guard_token, token):
            raise HTTPException(status_code=401, detail="invalid token")

    app = FastAPI(title="MCP Guard Daemon", dependencies=[Depends(require_token)])

    @app.get("/v1/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "pending": len(broker.pending_ids)}

    @app.get("/v1/pending")
    async def pending() -> list[str]:
        return broker.pending_ids

    @app.post("/v1/approvals")
    async def approve(approval: ApprovalRequest, http_request: Request) -> ApprovalDecision:
        """Hold the connection open until the user decides, then return the decision.

        uvicorn does not cancel a handler when its client disconnects, so we poll for it:
        if the interceptor goes away (process killed, gateway client gone), the request is
        cancelled and its popup closes instead of lingering until the timeout.
        """
        task = asyncio.create_task(broker.submit(approval))
        while True:
            done, _ = await asyncio.wait({task}, timeout=config.DISCONNECT_POLL_S)
            if done:
                return task.result()
            if await http_request.is_disconnected():
                broker.cancel(approval.request_id)
                return await task

    @app.post("/v1/approvals/{request_id}/cancel")
    async def cancel(request_id: str) -> dict[str, bool]:
        return {"cancelled": broker.cancel(request_id)}

    return app
