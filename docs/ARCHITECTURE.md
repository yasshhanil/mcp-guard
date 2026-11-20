# MCP Guard — Architecture

MCP Guard is a Python background application that intercepts Model Context Protocol (MCP)
requests made by **Claude Desktop** and **Claude Web**, shows the user a confirmation popup for
each tool invocation, and lets the request proceed only if the user clicks **Allow**.

### Implementation status

| Built | Planned |
|---|---|
| Daemon (FastAPI + broker + Tkinter popups), stdio proxy, HTTP gateway, installer, CLI, demo server, tests | Policy rules (`core/policy.py`), audit log (`core/audit.py`), tray icon (`daemon/tray.py`), `--fail-open`, daemon authenticity check |

Planned items are marked *(planned)* below.

---

## 1. Where MCP traffic actually flows

The design is driven by one fact: the two Claude clients reach MCP servers through different
paths, so they need different interception points.

| Client | Transport | Who sends the request | Interception point |
|---|---|---|---|
| Claude Desktop — local servers | JSON-RPC over **stdio** | Claude Desktop, on the user's machine | **Stdio Proxy** wrapping each server command |
| Claude Web / Desktop custom connectors | JSON-RPC over **Streamable HTTP** | **Anthropic's cloud** (not the browser) | **HTTP Gateway** in front of the MCP server, exposed via a tunnel |

### Approaches considered and rejected

- **TLS interception (mitmproxy) or a browser extension on claude.ai** — remote tool calls are
  executed server-side by Anthropic's infrastructure. By the time a `tool_use` block appears in
  the browser stream, the call has already run. This can *observe* but never *block*.
- **Process hooking / DLL injection into Claude Desktop** — fragile across app updates,
  platform-specific, and a security liability in itself.

Wrapping the transport (stdio) and fronting the server (HTTP) are the only points where a
request can be **held before execution** using supported, documented mechanisms.

---

## 2. System overview

```
┌──────────────────┐   stdio (JSON-RPC)    ┌──────────────────┐   stdio    ┌──────────────┐
│  Claude Desktop  │ ────────────────────► │  Stdio Proxy     │ ─────────► │ Local MCP    │
└──────────────────┘ ◄──────────────────── │ (1 per server)   │ ◄───────── │ server       │
                                           └────────┬─────────┘            └──────────────┘
                                                    │ HTTP (httpx)
                                                    ▼
                                  ┌────────────────────────────────────┐
                                  │        GUARD DAEMON (FastAPI)      │
                                  │        127.0.0.1:8765              │
                                  │                                    │
                                  │  API layer ─► Approval Broker      │
                                  │                 │       │          │
                                  │            Policy   Audit Log      │
                                  │                 │                  │
                                  │          Popup UI (Tkinter)        │
                                  │          Tray icon (pystray)       │
                                  └────────────────────────────────────┘
                                                    ▲ HTTP (httpx)
                                                    │
┌──────────────────┐  HTTPS  ┌────────┐  ┌─────────┴────────┐   HTTP    ┌──────────────┐
│ Claude Web       │ ──────► │ Tunnel │─►│ HTTP Gateway     │ ────────► │ Remote MCP   │
│ (Anthropic cloud)│ ◄────── │        │◄─│ (FastAPI) :8080  │ ◄──────── │ server       │
└──────────────────┘         └────────┘  └──────────────────┘           └──────────────┘
```

**Principle:** decisions are made in exactly one place — the Guard Daemon. The Stdio Proxy and
HTTP Gateway are thin interceptors that hold a request and ask the daemon what to do.

### How each part is started

| Component | Started by | Lifetime |
|---|---|---|
| Guard Daemon | User (`mcp-guard daemon`) or Windows startup | Always on, tray icon only |
| Stdio Proxy | **Claude Desktop, automatically** (after `mcp-guard install`) | Lives and dies with Claude Desktop |
| HTTP Gateway | User (`mcp-guard gateway`) + tunnel (e.g. cloudflared) | Only needed for Claude Web |

---

## 3. Components

### 3.1 Guard Daemon (FastAPI, `127.0.0.1:8765`)

The background service. Bound to loopback only — unreachable from the network.

| Endpoint | Purpose |
|---|---|
| `POST /v1/approvals` | Submit a pending request. The HTTP call **stays open until the user decides**, then returns `ApprovalDecision`. If the caller disconnects while waiting, the request is cancelled and its popup closes. |
| `POST /v1/approvals/{id}/cancel` | Client cancelled the request; close the popup, nothing is forwarded. |
| `GET /v1/pending` | List requests awaiting a decision. |
| `GET /v1/audit` | Query past decisions. |
| `GET /v1/health` | Liveness check used by interceptors. |

All endpoints require an `X-Guard-Token` header. The token is generated at install time and
stored in a file readable only by the current user.

**Internal modules**

- **Approval Broker** (`daemon/broker.py`) — holds `{request_id: asyncio.Future}`. Each
  incoming request creates a Future and `await`s it, suspending only that request. A button click
  resolves the Future and the held HTTP response returns. Also owns the per-request timeout
  (default 120 s → auto-block). At most `MCP_GUARD_MAX_PENDING` (default 10) requests may wait
  at once; extra requests and duplicate request ids are blocked immediately, without a popup, so
  no client can flood the screen.
- **Intercepted methods** (`core/config.py`) — only `tools/call` by default
  (`INTERCEPTED_METHODS`).
- **Popup UI** (`ui/popup.py`) — Tkinter window with `-topmost`, showing server, tool, arguments
  (pretty-printed, plain text, truncated at 4,000 characters), source client and timestamp.
  Buttons are enabled after 600 ms to prevent accidental keystroke approval. Closing the window
  counts as Block.
- *(planned)* **Policy** (`core/policy.py`) — always-allow / always-deny rules per server and tool.
- *(planned)* **Audit Log** (`core/audit.py`) — SQLite record of every decision, with sensitive
  keys redacted.
- *(planned)* **Tray** (`daemon/tray.py`) — pystray icon: Pause, View Log, Quit.

**Threading model**

Tkinter must own the main thread; uvicorn needs its own asyncio event loop.

```
Main thread ─────────► Tkinter mainloop (popups)
                          ▲                │
              queue.Queue │                │ loop.call_soon_threadsafe(
          (new requests)  │                │   future.set_result(decision))
                          │                ▼
Thread 2  ───────────► uvicorn + FastAPI + Broker (asyncio)

Thread 3  ───────────► pystray tray icon
```

- Broker → UI: thread-safe `queue.Queue`, polled by Tkinter via `root.after(100, …)`.
- UI → Broker: `loop.call_soon_threadsafe` resolves the Future on the event loop's thread.

### 3.2 Stdio Proxy (`proxy/stdio.py`) — Claude Desktop

`mcp-guard install` backs up and rewrites `claude_desktop_config.json` so each server is launched
through the proxy:

```jsonc
// before
"filesystem": { "command": "npx", "args": ["@mcp/server-filesystem", "C:/notes"] }

// after
"filesystem": { "command": "python",
                "args": ["-m", "mcp_guard.proxy.stdio", "--name", "filesystem",
                         "--", "npx", "@mcp/server-filesystem", "C:/notes"] }
```

`mcp-guard uninstall` reverses the wrapping exactly (the original command is recovered from after
`--`), which keeps any preferences Claude Desktop saved since install. A one-time backup,
`claude_desktop_config.json.mcpguard.bak`, is also kept next to the config.

The proxy spawns the real server as a child process and runs two async pumps:

- **Client → Server:** parse each newline-delimited JSON-RPC message.
  - Malformed intercepted request (bad id, `params`, `name` or `arguments`) → answered with
    JSON-RPC `-32602 Invalid params`; never forwarded. A per-message safety net logs any other
    unexpected error and keeps the proxy running.
  - Intercepted method (`tools/call`) → spawn a task that calls `POST /v1/approvals` and awaits.
    - **allow** → forward the original message to the child.
    - **block** → write a synthetic response to Claude (see §5).
  - Anything else (initialize, `tools/list`, notifications) → forward immediately.
  - `notifications/cancelled` for a held id → call `/cancel`, drop the held message.
- **Server → Client:** copy responses back unchanged.

Because each held request waits in its own task, parallel tool calls are independent and
non-intercepted traffic keeps flowing.

### 3.3 HTTP Gateway (`gateway/app.py`, FastAPI, `:8080`) — Claude Web

A reverse proxy for the MCP Streamable HTTP transport. Exposed via a tunnel and registered in
claude.ai as a custom connector.

| Endpoint | Purpose |
|---|---|
| `POST /mcp/{secret}` | Inspect JSON-RPC body; intercepted methods go through the daemon first, everything else is forwarded. |
| `GET /mcp/{secret}` | Pass through the server→client SSE stream. |
| `DELETE /mcp/{secret}` | Session termination pass-through. |

- Upstream calls use `httpx` streaming; responses are returned via `StreamingResponse`, so both
  `application/json` and `text/event-stream` work.
- `Mcp-Session-Id` and related headers are preserved.
- The `{secret}` path segment prevents use by anyone who discovers the tunnel URL.
- All intercepted calls in one POST are sent to the daemon **in parallel**, so each popup appears
  at once.
- **Batches are all-or-nothing.** If any call in a batch is blocked, nothing is forwarded and
  every request in the batch is answered as blocked. Forwarding part of a batch would leave ids
  unanswered. (Batching was removed from the current MCP spec, so this is rare.)
- If the client disconnects while waiting, the daemon requests are cancelled and their popups
  close.

---

## 4. Request lifecycle

```
Claude          Proxy/Gateway            Daemon (FastAPI)          User
  │  tools/call      │                         │                    │
  │─────────────────►│  POST /v1/approvals     │                    │
  │                  │────────────────────────►│ create Future      │
  │                  │     (connection held)   │ audit "pending"    │
  │                  │                         │──── popup ────────►│
  │   (waiting...)   │                         │                    │ clicks
  │                  │                         │◄─── Allow/Block ───│
  │                  │ ApprovalDecision        │ resolve Future     │
  │                  │◄────────────────────────│ audit decision     │
  │                  │                         │                    │
  │  allow: forwarded to real server, real response returned        │
  │  block: synthetic "Blocked by user" result returned             │
  │◄─────────────────│                         │                    │
```

---

## 5. Block response

For `tools/call`, a block returns a valid tool result flagged as an error, so Claude reports the
denial to the user instead of treating it as a protocol failure:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {
    "content": [{ "type": "text", "text": "Blocked by user via MCP Guard." }],
    "isError": true
  }
}
```

For other intercepted methods, a JSON-RPC error is returned:
`{"code": -32001, "message": "Blocked by user via MCP Guard"}`.

---

## 6. Shared data model (`core/models.py`, pydantic)

```python
class ApprovalRequest(BaseModel):
    request_id: str                                  # uuid4
    client: Literal["claude-desktop", "remote-connector"]
    # remote-connector: called from Anthropic's cloud via the gateway — from claude.ai or from
    # Claude Desktop (account connectors appear in both), so the app itself can't be known.
    server_name: str                                 # "filesystem"
    method: str                                      # "tools/call"
    tool_name: str | None                            # "delete_file"
    arguments: dict                                  # {"path": "C:/notes/old.txt"}
    received_at: datetime

class ApprovalDecision(BaseModel):
    request_id: str
    decision: Literal["allow", "block"]
    reason: Literal["user", "timeout", "policy", "cancelled", "daemon_shutdown"]
```

Used by the proxy, gateway and daemon alike — one source of truth.

---

## 7. Concurrency and failure handling

| Situation | Behaviour |
|---|---|
| Multiple simultaneous calls | One Future and one popup per request (cascaded); decisions keyed by request id, never crossed. |
| More than `MAX_PENDING` waiting | Extra requests auto-blocked (`reason="overloaded"`), no popup. |
| Daemon not running | Interceptor **fails closed** — returns "MCP Guard daemon is not running". *(planned: opt-in `--fail-open`)* |
| Daemon returns an error / wrong request id | Block (`reason="error"`). |
| No decision within timeout | Auto-block (`reason="timeout"`), popup closes. |
| Client cancels a pending call | `/cancel` → popup closes, message never forwarded. |
| Interceptor disconnects while waiting | Daemon notices within 0.5 s, cancels the request, popup closes. |
| Malformed `tools/call` | `-32602 Invalid params` reply; proxy keeps running. |
| Real MCP server crashes | All in-flight ids receive errors; proxy exits cleanly. |
| Claude Desktop exits | stdin EOF → proxy kills child process tree, cancels its pending requests. |
| Daemon shuts down | All pending requests resolve as block (`reason="daemon_shutdown"`). |
| Non-JSON lines / batch arrays | Non-JSON lines pass through untouched. Proxy: batch items are handled individually. Gateway: batches are all-or-nothing. |
| Windows specifics | UTF-8 binary stdio, separate reader/writer tasks (no pipe deadlock), process-group cleanup. |

---

## 8. Security

- Daemon binds to `127.0.0.1` only; every call requires the per-install token.
- Gateway requires a secret path segment (or bearer token).
- Popup renders arguments as plain text only; buttons are delay-enabled.
- The daemon API has **no approve endpoint**: approvals can only come from a click in the popup.
  The token lets a caller create, cancel and list requests, never approve them.
- Popups are capped (`MAX_PENDING`) so no caller can flood the screen.
- Installer always backs up `claude_desktop_config.json` before modifying it.

**Threat model.** MCP Guard protects the user from unwanted actions by Claude and its tools. It
does not protect against other malicious software running as the same user, which could edit
the Claude config directly, or bind port 8765 before the daemon starts and answer "allow"
(interceptors do not yet verify the daemon's identity — *planned*).

---

## 9. Project layout

```
mcp_guard/
├── core/        config.py, models.py, jsonrpc.py, daemon_client.py (shared "ask the daemon")
├── daemon/      app.py (FastAPI), broker.py, service.py (thread wiring)
├── ui/          popup.py
├── proxy/       stdio.py
├── gateway/     app.py (FastAPI)
├── installer/   desktop_config.py
└── cli.py       mcp-guard install | uninstall | daemon | gateway | status
examples/        demo_server.py   (FastMCP: read_file, write_file, delete_file)
tests/
```

## 10. Technology stack

Python 3.11+, FastAPI + uvicorn, httpx, pydantic, asyncio, Tkinter (stdlib), `mcp` SDK `<2`
(demo server), pytest + pytest-asyncio. Planned: pystray + Pillow (tray), SQLite (audit log).
