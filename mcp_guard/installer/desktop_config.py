"""Wrap / unwrap the MCP servers in claude_desktop_config.json with the MCP Guard proxy."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

PROXY_MODULE = "mcp_guard.proxy.stdio"
DEMO_NAME = "mcp-guard-demo"


def find_config_path() -> Path:
    """Locate claude_desktop_config.json (Microsoft Store build first, then standard)."""
    if override := os.environ.get("CLAUDE_DESKTOP_CONFIG"):
        return Path(override)
    if sys.platform == "win32":
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
        for pkg in sorted(packages.glob("Claude_*")) if packages.exists() else []:
            candidate = pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json"
            if candidate.parent.exists():
                return candidate
        return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on the same volume


def is_wrapped(entry: dict[str, Any]) -> bool:
    return PROXY_MODULE in entry.get("args", [])


def wrap(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Return `entry` rewritten to launch through the proxy (other keys such as env kept)."""
    if is_wrapped(entry) or "command" not in entry:
        return entry  # already wrapped, or a remote/URL entry we can't proxy over stdio
    wrapped = dict(entry)
    wrapped["command"] = sys.executable
    wrapped["args"] = ["-m", PROXY_MODULE, "--name", name, "--", entry["command"], *entry.get("args", [])]
    return wrapped


def unwrap(entry: dict[str, Any]) -> dict[str, Any]:
    """Reverse `wrap`, restoring the original command and args."""
    if not is_wrapped(entry):
        return entry
    args = entry["args"]
    original = args[args.index("--") + 1 :]
    restored = dict(entry)
    restored["command"], restored["args"] = original[0], original[1:]
    if not restored["args"]:
        del restored["args"]
    return restored


def install(add_demo: bool = False) -> list[str]:
    """Wrap every stdio MCP server. Returns the names of wrapped servers."""
    path = find_config_path()
    data = _load(path)
    if path.exists():
        backup = path.with_name(path.name + ".mcpguard.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    servers: dict[str, Any] = data.setdefault("mcpServers", {})
    if add_demo:
        demo = Path(__file__).resolve().parents[2] / "examples" / "demo_server.py"
        servers[DEMO_NAME] = {"command": sys.executable, "args": [str(demo)]}
    for name, entry in servers.items():
        servers[name] = wrap(name, entry)
    _save(path, data)
    return [n for n, e in servers.items() if is_wrapped(e)]


def uninstall(remove_demo: bool = True) -> list[str]:
    """Unwrap every proxied server (and drop the demo server). Returns unwrapped names."""
    path = find_config_path()
    data = _load(path)
    servers: dict[str, Any] = data.get("mcpServers", {})
    changed = [n for n, e in servers.items() if is_wrapped(e)]
    for name in changed:
        servers[name] = unwrap(servers[name])
    if remove_demo:
        servers.pop(DEMO_NAME, None)
    if path.exists():
        _save(path, data)
    return changed
