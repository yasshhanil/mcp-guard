# MCP Guard Development Guidelines

> **Audience**: LLM-driven engineering agents and human developers
>
> Adapted from the [FastMCP AGENTS.md](https://github.com/PrefectHQ/fastmcp/blob/main/AGENTS.md).

MCP Guard is a Python (≥3.11) background application that intercepts MCP requests from
Claude Desktop and Claude Web and asks the user to Allow or Block each one.
**Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before making structural changes** — it is
the source of truth for components, endpoints, threading, and failure behaviour.

## Required Development Workflow

Install dependencies before running local checks:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"   # Install dependencies
```

Run the tests relevant to the behavior you changed, including neighboring paths that share the
implementation. For example:

```powershell
.\.venv\Scripts\python -m pytest tests/test_broker.py
```

Broaden the selection when a concrete dependency or failure warrants it. Run the full suite before
committing changes that touch `core/` or the broker.

You must also pass static checks:

```powershell
.\.venv\Scripts\python -m ruff check .      # Lint
.\.venv\Scripts\python -m ruff format .     # Format
.\.venv\Scripts\python -m mypy mcp_guard    # Type check
```

Note: `mcp` is pinned `<2` (v2 renamed FastMCP → MCPServer).

**Relevant tests and static checks must pass before committing.**

## Repository Structure

| Path                  | Purpose                                              |
| --------------------- | ---------------------------------------------------- |
| `mcp_guard/`          | Application source code                              |
| `├─core/`             | Shared models, JSON-RPC framing, policy, audit, config |
| `├─daemon/`           | FastAPI approval service, broker, thread wiring, tray |
| `├─ui/`               | Tkinter confirmation popup                           |
| `├─proxy/`            | Stdio proxy launched by Claude Desktop               |
| `├─gateway/`          | FastAPI HTTP gateway for Claude Web                  |
| `├─installer/`        | `claude_desktop_config.json` wrap / restore          |
| `└─cli.py`            | `mcp-guard` command-line entry point                 |
| `examples/`           | Demo MCP server used for testing and the demo video  |
| `tests/`              | Pytest suite                                         |
| `docs/`               | Architecture and supporting documentation            |

## Core Invariants

These define correct behaviour. Changes must preserve them; tests should cover them.

- **One decision point.** All Allow/Block decisions are made by the daemon's Approval Broker.
  The stdio proxy and HTTP gateway only intercept and ask — never decide locally (except
  fail-closed when the daemon is unreachable).
- **Fail closed.** Daemon unreachable, timeout, shutdown, overload, or unexpected error →
  **block**. (A planned `--fail-open` flag would be opt-in only.)
- **Hold, don't drop.** An intercepted request is held until a decision arrives, then either
  forwarded unchanged or answered with a synthetic block response. Never forward before a
  decision; never leave a request id unanswered.
- **Requests are independent.** Each pending request has its own Future, task and popup, keyed
  by request id. One slow decision must never stall other traffic or other requests.
- **Pass-through is transparent.** Non-intercepted messages, headers (`Mcp-Session-Id`) and
  response streams (JSON and SSE) are forwarded byte-for-byte where possible.
- **Threading.** Tkinter owns the main thread. The FastAPI/uvicorn event loop runs on its own
  thread. Cross over only via `queue.Queue` (broker → UI) and `loop.call_soon_threadsafe`
  (UI → broker). Never touch Tk widgets from the event loop, or Futures from the Tk thread.
- **Shared models.** `core/models.py` (`ApprovalRequest`, `ApprovalDecision`) is the single
  source of truth for data exchanged between components. Don't define parallel dicts or schemas.

## Security Rules

- The daemon binds to `127.0.0.1` only. Every daemon endpoint requires the `X-Guard-Token` header.
- The gateway must require its secret path segment or bearer token.
- Never log tokens, the gateway secret, or tool argument values. Logs carry ids, server and
  tool names only. (The gateway prints its connector URL once at startup, by design, and runs
  uvicorn with `access_log=False` so the secret path is never logged per request.)
- The daemon API must never gain an "approve" endpoint: approvals come only from the popup.
- The popup renders request data as plain text only.
- The installer must back up `claude_desktop_config.json` before writing, and `uninstall` must
  reverse the wrapping exactly.

## Development Rules

### Git

- **NEVER** commit, push, or open a PR unless specifically instructed to do so.
- Never amend commits to fix lint or test failures — make a new commit.
- Keep commit messages brief — ideally just headlines, not detailed messages.
- Focus on what changed, not how or why.
- Agents must identify themselves in commits and PRs (co-author trailer / "Generated with Claude
  Code").

### PR Messages

- 1–2 paragraphs: problem/tension + solution (PRs are documentation!)
- Focused code example showing the key capability
- **Avoid:** bullet summaries, exhaustive change lists, marketing language
- Minor fixes: keep body short and concise

### Code Review Guidelines

- **Fix causes, not symptoms.** A side-channel that compensates for a missing step adds permanent
  complexity. If the fix doesn't change the code path where the bug actually happens, ask why not.
- Focus on API design and naming clarity.
- Identify confusing patterns (parameter values that contradict defaults) or non-idiomatic code
  (mutable defaults, etc.).
- Suggest specific improvements, not generic "add more tests" comments.
- Report an edge case only when it is reachable under supported usage or a credible threat model
  and has meaningful impact.

### Code Standards

- Python ≥ 3.11 with full type annotations
- Follow existing patterns and maintain consistency
- **Prioritize readable, understandable code** — clarity over cleverness
- Avoid obfuscated or confusing patterns even if they're shorter
- Each feature needs corresponding tests
- Every module, public class and public function has a docstring (markdown, not RST)

### Module Exports

- **Do not create overeager `__init__.py` files.** Package initializers should not import heavy
  submodules (FastAPI apps, Tkinter, pystray) or modules that point back into the package.
  Overeager re-exports create circular imports and slow proxy start-up.
- Prefer importing from the specific submodule over re-exporting to parent namespaces.

### Documentation

- **Core principle:** a feature doesn't exist unless it is documented.
- When you change components, endpoints, threading, or failure behaviour, update
  `docs/ARCHITECTURE.md` to match.
- When you add or change a setting in `core/config.py` or a CLI flag, update the README.
- **Code examples:** explain before showing code; make blocks fully runnable (include imports).
- **Structure:** headers form a navigation guide; logical H2/H3 hierarchy.
- **Content:** motivate features (why) before mechanics (how).

## Critical Patterns

- Never use bare `except` — be specific with exception types. Top-level handlers that must not
  crash (proxy pumps, broker tasks) may catch `Exception`, but must log it and resolve affected
  requests as **block**.
- Never block the event loop: no synchronous I/O or `time.sleep` in async code.
- Always clean up child processes and pending Futures on exit (EOF, signals, crashes).
- Default test timeout is 5s. Tests must not open real Tk windows or reach the network — inject
  a fake UI / fake upstream instead.
- Always reinstall (`pip install -e ".[dev]"`) first when debugging environment issues.
