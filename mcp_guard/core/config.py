"""Runtime configuration: daemon address, data directory and the shared auth token."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = int(os.environ.get("MCP_GUARD_PORT", "8765"))
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"

# How long a request may wait for a decision before it is auto-blocked.
DECISION_TIMEOUT_S = float(os.environ.get("MCP_GUARD_TIMEOUT", "120"))

# Maximum popups open at once; further requests are auto-blocked until some are decided.
MAX_PENDING = int(os.environ.get("MCP_GUARD_MAX_PENDING", "10"))

# How often a held HTTP request checks whether its client has disconnected.
DISCONNECT_POLL_S = 0.5

# Methods that require user approval. Everything else passes through.
INTERCEPTED_METHODS = frozenset({"tools/call"})


def data_dir() -> Path:
    """Return the per-user data directory, creating it if needed."""
    path = Path(os.environ.get("MCP_GUARD_HOME", Path.home() / ".mcp_guard"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path() -> Path:
    """Path of the file holding the daemon auth token."""
    return data_dir() / "token"


def load_or_create_token() -> str:
    """Return the auth token, generating and storing one on first use (daemon side)."""
    path = token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)  # no-op on Windows; the file lives in the user's profile
    except OSError:
        pass
    return token


def read_token() -> str | None:
    """Return the auth token if the daemon has created one (proxy side)."""
    path = token_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None
