"""Demo MCP server with file tools, sandboxed to ~/mcp_guard_demo.

Used to demonstrate Allow / Block in Claude Desktop.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

SANDBOX = Path.home() / "mcp_guard_demo"
SANDBOX.mkdir(exist_ok=True)

mcp = FastMCP("mcp-guard-demo")


def _resolve(name: str) -> Path:
    path = (SANDBOX / name).resolve()
    if SANDBOX.resolve() not in path.parents and path != SANDBOX.resolve():
        raise ValueError("path escapes the demo sandbox")
    return path


@mcp.tool()
def list_files() -> list[str]:
    """List files in the demo sandbox."""
    return sorted(p.name for p in SANDBOX.iterdir())


@mcp.tool()
def read_file(name: str) -> str:
    """Read a text file from the demo sandbox."""
    return _resolve(name).read_text(encoding="utf-8")


@mcp.tool()
def write_file(name: str, content: str) -> str:
    """Write a text file in the demo sandbox."""
    _resolve(name).write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {name}"


@mcp.tool()
def delete_file(name: str) -> str:
    """Delete a file from the demo sandbox."""
    _resolve(name).unlink()
    return f"deleted {name}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--http", type=int, metavar="PORT", help="serve Streamable HTTP on PORT")
    args = parser.parse_args()
    if args.http:
        mcp.settings.port = args.http
        mcp.run(transport="streamable-http")  # http://127.0.0.1:PORT/mcp
    else:
        mcp.run()
