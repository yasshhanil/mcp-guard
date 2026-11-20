"""Approval Broker: the single place where Allow/Block decisions are made.

Each pending request owns an `asyncio.Future`. `submit` awaits it (suspending only that
request); the UI thread resolves it through `resolve_threadsafe`. Anything that goes
wrong resolves as **block** (fail closed).
"""

from __future__ import annotations

import asyncio
import logging
import queue
from dataclasses import dataclass

from mcp_guard.core.models import ApprovalDecision, ApprovalRequest, DecisionReason, DecisionValue

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UIEvent:
    """Message from the broker to the UI thread."""

    kind: str  # "show" | "close"
    request: ApprovalRequest | None = None
    request_id: str | None = None


class Broker:
    """Tracks pending requests and their decisions."""

    def __init__(
        self, ui_queue: "queue.Queue[UIEvent]", timeout_s: float, max_pending: int = 10
    ) -> None:
        self._ui_queue = ui_queue
        self._timeout_s = timeout_s
        self._max_pending = max_pending
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def pending_ids(self) -> list[str]:
        """Ids of requests still awaiting a decision."""
        return list(self._pending)

    async def submit(self, request: ApprovalRequest) -> ApprovalDecision:
        """Show a popup for `request` and wait for the user's decision (or timeout).

        Requests beyond `max_pending`, and duplicates of an id already pending, are blocked
        immediately without a popup, so no client can flood the screen.
        """
        self._loop = asyncio.get_running_loop()
        if request.request_id in self._pending:
            log.warning("duplicate request id %s, blocking", request.request_id)
            return self._make(request.request_id, "block", "error")
        if len(self._pending) >= self._max_pending:
            log.warning("%d requests already pending, blocking", len(self._pending))
            return self._make(request.request_id, "block", "overloaded")
        future: asyncio.Future[ApprovalDecision] = self._loop.create_future()
        self._pending[request.request_id] = future
        self._ui_queue.put(UIEvent("show", request=request))
        log.info("pending %s %s/%s", request.request_id, request.server_name, request.tool_name)
        try:
            decision = await asyncio.wait_for(asyncio.shield(future), self._timeout_s)
        except asyncio.TimeoutError:
            decision = self._make(request.request_id, "block", "timeout")
        except asyncio.CancelledError:
            # The awaiting task itself was cancelled (e.g. server shutdown); close the popup.
            self._finish(request.request_id, self._make(request.request_id, "block", "cancelled"))
            raise
        self._finish(request.request_id, decision)
        log.info("decided %s -> %s (%s)", request.request_id, decision.decision, decision.reason)
        return decision

    def resolve(self, request_id: str, decision: DecisionValue, reason: DecisionReason) -> bool:
        """Resolve a pending request. Must run on the event loop thread."""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(self._make(request_id, decision, reason))
        return True

    def resolve_threadsafe(
        self, request_id: str, decision: DecisionValue, reason: DecisionReason = "user"
    ) -> None:
        """Resolve a pending request from any thread (used by the UI)."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.resolve, request_id, decision, reason)

    def cancel(self, request_id: str) -> bool:
        """Cancel a pending request (client gave up). The request resolves as block."""
        return self.resolve(request_id, "block", "cancelled")

    def shutdown(self) -> None:
        """Block every pending request. Must run on the event loop thread."""
        for request_id in list(self._pending):
            self.resolve(request_id, "block", "daemon_shutdown")

    def _finish(self, request_id: str, decision: ApprovalDecision) -> None:
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(decision)
        self._ui_queue.put(UIEvent("close", request_id=request_id))

    @staticmethod
    def _make(request_id: str, decision: DecisionValue, reason: DecisionReason) -> ApprovalDecision:
        return ApprovalDecision(request_id=request_id, decision=decision, reason=reason)
