# MCP Guard

MCP Guard is a Python background app that intercepts **Model Context Protocol (MCP)** tool calls
made by Claude. It holds each call until you click **Allow** or **Block** in a popup that stays on
top of your other windows.

> **Status: MVP.** Claude Desktop (local stdio MCP servers) and Claude Web (remote connectors via
> the HTTP gateway) are supported. A tray icon and audit log are planned. See
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## How it works

```
Claude Desktop ──stdio──► MCP Guard proxy ──stdio──► real MCP server
                               │
                         asks the daemon (127.0.0.1:8765, token-authenticated)
                               ▼
                     MCP Guard daemon ──► Allow / Block popup
```

- `mcp-guard install` rewrites `claude_desktop_config.json` so that Claude Desktop starts each
  MCP server through the proxy. Your original config is backed up first.
- The proxy forwards everything unchanged **except `tools/call`**. A `tools/call` is held while the
  proxy asks the daemon what to do.
- **Allow**: the call is forwarded to the real server.
- **Block**: Claude gets a tool error that says *"Blocked by user via MCP Guard."*
- Calls are blocked by default whenever something goes wrong (*fail closed*). This covers the
  daemon not running, no decision within 120 seconds, the popup being closed, and any unexpected
  error.
- Simultaneous calls each get their own popup and decision.

## Requirements

- Windows 10/11 (macOS/Linux should work but are untested)
- Python 3.11+
- Claude Desktop

## Setup

```powershell
git clone https://github.com/yasshhanil/mcp-guard.git
cd mcp-guard
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
```

## Run

**1. Start the daemon.** Leave this window running:

```powershell
mcp-guard daemon
```

**2. Route Claude Desktop through MCP Guard.** This is a one-time step. **Quit Claude Desktop
first** (from the system tray), because it rewrites its own config while running and could undo
the change. `--demo` also adds a sample server whose file tools only work inside
`~/mcp_guard_demo`:

```powershell
mcp-guard install --demo
```

**3. Start Claude Desktop again.**

**4. Try it.** Ask Claude to *"write a file hello.txt with 'hi' using the demo tools"*. A popup
appears. Click **Allow** and the file is written. Then ask Claude to delete it and click
**Block**. Claude reports that the call was blocked.

## Run with Claude Web

Claude Web calls MCP servers from Anthropic's cloud, not from your browser. MCP Guard therefore
runs a **gateway** in front of the MCP server and exposes it with ngrok:

```
claude.ai ─► Anthropic cloud ─► ngrok ─► MCP Guard gateway :8080 ─► MCP server :9000
                                                │
                                        daemon ─► popup on your PC
```

Use four terminals, and keep the daemon from step 1 of the Claude Desktop setup running:

```powershell
# 1. the real MCP server, served over HTTP
python examples\demo_server.py --http 9000

# 2. the gateway (prints the secret connector path)
mcp-guard gateway --upstream http://127.0.0.1:9000/mcp --port 8080 --name demo-web

# 3. the public tunnel
ngrok http 8080
```

In claude.ai, go to **Settings → Connectors → Add custom connector**. For the URL, enter
`https://<your-ngrok-domain>/mcp/<secret>`, using the secret the gateway printed. Enable the
connector in a chat and ask Claude to use the demo tools. The same Allow/Block popup appears.

The secret path segment means only someone who knows the full URL can reach the gateway. Requests
with a wrong path get a 404.

### Other commands

| Command | What it does |
|---|---|
| `mcp-guard status` | Checks whether the daemon is running |
| `mcp-guard uninstall` | Restores the original server commands and removes the demo server |

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MCP_GUARD_PORT` | `8765` | Daemon port (always bound to 127.0.0.1) |
| `MCP_GUARD_TIMEOUT` | `120` | Seconds before an unanswered request is auto-blocked |
| `MCP_GUARD_MAX_PENDING` | `10` | Most popups open at once; further requests are auto-blocked |
| `MCP_GUARD_HOME` | `~/.mcp_guard` | Where the auth token is stored |
| `CLAUDE_DESKTOP_CONFIG` | auto-detected | Path to `claude_desktop_config.json` |

The config is auto-detected for both the Microsoft Store build
(`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\`) and the standard installer
(`%APPDATA%\Claude\`).

## Tests

```powershell
pytest
```

## Project layout

```
mcp_guard/
├── core/       config, shared pydantic models, JSON-RPC helpers, daemon client
├── daemon/     FastAPI app, approval broker, service (thread wiring)
├── ui/         Tkinter popup
├── proxy/      stdio proxy launched by Claude Desktop
├── gateway/    HTTP gateway for Claude Web connectors
├── installer/  claude_desktop_config.json wrap / unwrap
└── cli.py      mcp-guard command
examples/       demo MCP server
tests/
```

## Known limitations

**What it can't see**
- **Claude Web: only servers routed through the gateway.** Built-in and third-party connectors
  (Google Drive, Slack, …) are called cloud-to-cloud from Anthropic and never reach this machine.
- **Claude Desktop Extensions** (`.mcpb`/`.dxt`, installed from the Extensions UI) and URL-based
  remote entries are not in `mcpServers` as stdio commands, so `install` does not wrap them.
- **Only `tools/call` is gated.** `resources/read`, `prompts/get`, and server-to-client requests
  (sampling, elicitation) pass through. See `INTERCEPTED_METHODS` in `core/config.py`.
- **Servers added after `install`** stay unguarded until you run `install` again.

**Behaviour gaps**
- **Client timeouts are shorter than ours.** We hold a call for up to 120 s; Claude (especially
  claude.ai) may give up sooner. A late Allow can then run a call Claude no longer waits for.
- **If the MCP server crashes,** calls that were already allowed and forwarded get no error
  reply; Claude waits until its own timeout. (Calls still awaiting approval do get an error.)
- **Long arguments are cut off** in the popup after 4,000 characters, with no way to expand them.
- **Not built yet:** always-allow/deny rules, an audit log, a tray icon, and `--fail-open`.

## Design trade-offs

- **Fail closed over availability.** If the daemon isn't running, every tool call is blocked.
  Safer, but disruptive.
- **A popup per call.** The most precise control, at the cost of approval fatigue — people start
  clicking Allow on autopilot. Policy rules (planned) are the mitigation.
- **Single-process, in-memory broker.** Right for a one-user desktop tool: the bottleneck is the
  person clicking, not memory. Pending requests don't survive a daemon restart (they're blocked,
  not lost). A team deployment with shared approvers would need a shared broker (e.g. Redis
  pub/sub), approver authentication, and a persistent audit store.
- **Tkinter UI.** No extra installs, but basic, and Windows may not raise it over full-screen apps.
- **Secret URL path instead of OAuth for the gateway.** Simple, and claude.ai accepts it with no
  setup, but anyone holding the full URL has full access.

## Security considerations

**Protections in place**
- The daemon listens only on `127.0.0.1`, and every request needs a random per-install token.
- The daemon API has **no approve endpoint**. Approvals only come from a click in the popup; the
  token lets a caller create, cancel and list requests, never approve them.
- At most 10 popups at once (`MCP_GUARD_MAX_PENDING`); extra and duplicate requests are blocked,
  so no caller can flood the screen.
- Malformed requests are rejected (`-32602`), never forwarded, and can't crash the proxy.
- If a caller disconnects while waiting, its request is cancelled and the popup closes.
- The popup shows arguments as plain text only, and its buttons stay disabled for 600 ms so a
  stray keystroke can't decide.
- Logs never contain tokens, argument values, or the gateway secret (uvicorn's access log is off).
- The installer backs up the config and writes atomically.

**Threat model and residual risks**
- **Protects against Claude and its tools, not against malware on your account.** Software
  running as you could edit the Claude config to unwrap servers, or bind port 8765 before the
  daemon starts and answer "allow" — interceptors don't yet verify the daemon's identity. On
  Windows the token file has no restricted permissions (`chmod` is a no-op).
- **It gates the request, not what the tool does.** The popup trusts the server's own tool names;
  a malicious server could call a delete function `read_file`. Once allowed, a tool can do
  anything it is capable of.
- **Arguments can mislead.** Invisible or look-alike Unicode characters display as-is.
- **The gateway secret** is printed once at startup and visible in ngrok's request inspector.
  Don't show it in screen recordings.
- **Absolute paths.** The installer writes the venv's Python path into the Claude config; moving
  or deleting `.venv` breaks the guarded servers until you `uninstall` and `install` again.
