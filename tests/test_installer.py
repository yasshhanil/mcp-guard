"""wrap / unwrap round-trip and config preservation."""

import json
from pathlib import Path

import pytest

from mcp_guard.installer import desktop_config as dc


def test_wrap_unwrap_roundtrip() -> None:
    entry = {"command": "npx", "args": ["-y", "pkg", "C:/x"], "env": {"A": "1"}}
    wrapped = dc.wrap("fs", entry)
    assert dc.is_wrapped(wrapped)
    assert wrapped["env"] == {"A": "1"}
    assert dc.wrap("fs", wrapped) == wrapped  # idempotent
    assert dc.unwrap(wrapped) == entry


def test_install_preserves_other_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"preferences": {"x": 1}, "mcpServers": {"fs": {"command": "npx"}}}))
    monkeypatch.setenv("CLAUDE_DESKTOP_CONFIG", str(cfg))
    assert dc.install() == ["fs"]
    assert json.loads(cfg.read_text())["preferences"] == {"x": 1}
    assert (tmp_path / "claude_desktop_config.json.mcpguard.bak").exists()
    dc.uninstall()
    assert json.loads(cfg.read_text())["mcpServers"] == {"fs": {"command": "npx"}}
