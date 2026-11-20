"""Minimal JSON-RPC 2.0 helpers for MCP messages."""

from __future__ import annotations

import json
from typing import Any

BLOCKED_TEXT = "Blocked by user via MCP Guard."
BLOCKED_ERROR_CODE = -32001
INVALID_PARAMS = -32602


def parse_line(line: bytes) -> Any | None:
    """Parse one newline-delimited JSON-RPC message; return None if it is not valid JSON."""
    try:
        return json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def encode(message: dict[str, Any]) -> bytes:
    """Serialize a message as a single newline-terminated line."""
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def is_request(message: Any) -> bool:
    """True for a JSON-RPC request (has `method` and `id`), as opposed to a notification."""
    return isinstance(message, dict) and "method" in message and "id" in message


def is_valid_id(value: Any) -> bool:
    """True for an id MCP allows: a string or integer (never null, bool, list or object)."""
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def tool_call_fields(message: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Return `(tool name, arguments)` from an intercepted request.

    Raises `ValueError` when the id or params are malformed, so callers can reply with an
    `INVALID_PARAMS` error instead of crashing.
    """
    if not is_valid_id(message.get("id")):
        raise ValueError("id must be a string or integer")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    name = params.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("params.name must be a string")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("params.arguments must be an object")
    return name, arguments


def invalid_params_response(message: dict[str, Any], detail: str) -> dict[str, Any]:
    """Build the `INVALID_PARAMS` reply for a malformed intercepted request."""
    request_id = message.get("id") if is_valid_id(message.get("id")) else None
    return error_response(request_id, INVALID_PARAMS, f"Invalid params: {detail}")


def blocked_response(message: dict[str, Any], detail: str = BLOCKED_TEXT) -> dict[str, Any]:
    """Build the reply sent to the client when a request is blocked.

    `tools/call` gets a tool result flagged `isError`, so Claude explains the denial to the
    user; any other method gets a JSON-RPC error.
    """
    if message.get("method") == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": detail}], "isError": True},
        }
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "error": {"code": BLOCKED_ERROR_CODE, "message": detail},
    }


def error_response(request_id: Any, code: int, text: str) -> dict[str, Any]:
    """Build a generic JSON-RPC error response."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}}
