"""Data exchanged between interceptors (proxy, gateway) and the daemon."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

# "claude-desktop": a local stdio server launched by Claude Desktop (via the stdio proxy).
# "remote-connector": a connector called from Anthropic's cloud (via the HTTP gateway); used by
# claude.ai and by Claude Desktop alike, so the gateway cannot tell which app the user is in.
Client = Literal["claude-desktop", "remote-connector"]
DecisionValue = Literal["allow", "block"]
DecisionReason = Literal[
    "user",
    "timeout",
    "cancelled",
    "daemon_shutdown",
    "daemon_unreachable",
    "overloaded",
    "error",
]


class ApprovalRequest(BaseModel):
    """An intercepted MCP request awaiting a user decision."""

    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    client: Client = "claude-desktop"
    server_name: str
    method: str
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ApprovalDecision(BaseModel):
    """The outcome for one `ApprovalRequest`."""

    request_id: str
    decision: DecisionValue
    reason: DecisionReason
