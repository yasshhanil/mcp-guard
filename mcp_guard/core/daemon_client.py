"""Client used by interceptors (stdio proxy, HTTP gateway) to ask the daemon for decisions.

Every failure path returns a **block** decision (fail closed).
"""

from __future__ import annotations

import logging

import httpx

from mcp_guard.core import config, jsonrpc
from mcp_guard.core.models import ApprovalDecision, ApprovalRequest, DecisionReason

log = logging.getLogger(__name__)


async def request_decision(http: httpx.AsyncClient, req: ApprovalRequest) -> ApprovalDecision:
    """Submit `req` and wait for the user's decision."""
    token = config.read_token()
    if token is None:
        return _blocked(req, "daemon_unreachable")
    try:
        resp = await http.post(
            f"{config.DAEMON_URL}/v1/approvals",
            content=req.model_dump_json(),
            headers={"X-Guard-Token": token, "Content-Type": "application/json"},
            timeout=httpx.Timeout(config.DECISION_TIMEOUT_S + 15, connect=3),
        )
        resp.raise_for_status()
        decision = ApprovalDecision.model_validate_json(resp.content)
    except httpx.HTTPStatusError as exc:
        log.warning("daemon rejected request, blocking: %s", exc)
        return _blocked(req, "error")
    except httpx.HTTPError as exc:
        log.warning("daemon unavailable, blocking: %s", exc)
        return _blocked(req, "daemon_unreachable")
    except ValueError as exc:
        log.warning("invalid daemon response, blocking: %s", exc)
        return _blocked(req, "error")
    if decision.request_id != req.request_id:
        log.warning("daemon answered a different request id, blocking")
        return _blocked(req, "error")
    return decision


async def cancel_request(http: httpx.AsyncClient, request_id: str) -> None:
    """Tell the daemon a pending request was cancelled by the client (best effort)."""
    token = config.read_token()
    if token is None:
        return
    try:
        await http.post(
            f"{config.DAEMON_URL}/v1/approvals/{request_id}/cancel",
            headers={"X-Guard-Token": token},
            timeout=3,
        )
    except httpx.HTTPError:
        pass


def block_detail(decision: ApprovalDecision) -> str:
    """User-facing text explaining why a request was blocked."""
    if decision.reason == "daemon_unreachable":
        return "Blocked: MCP Guard daemon is not running."
    if decision.reason == "timeout":
        return "Blocked: no decision was made in time (MCP Guard)."
    if decision.reason == "overloaded":
        return "Blocked: too many requests are already awaiting approval (MCP Guard)."
    if decision.reason == "error":
        return "Blocked: MCP Guard could not process this request."
    return jsonrpc.BLOCKED_TEXT


def _blocked(req: ApprovalRequest, reason: DecisionReason) -> ApprovalDecision:
    return ApprovalDecision(request_id=req.request_id, decision="block", reason=reason)
