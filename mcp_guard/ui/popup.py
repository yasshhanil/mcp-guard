"""Tkinter confirmation popups. Runs on the main thread only.

The broker pushes `UIEvent`s onto a queue; `PopupManager` polls it with `root.after` and
opens one always-on-top window per pending request. Button clicks are sent back to the
broker via `Broker.resolve_threadsafe`.
"""

from __future__ import annotations

import json
import queue
import tkinter as tk
from tkinter import ttk

from mcp_guard.core.models import ApprovalRequest
from mcp_guard.daemon.broker import Broker, UIEvent

POLL_MS = 100
ARM_DELAY_MS = 600  # buttons stay disabled briefly so a stray keystroke can't decide
MAX_ARGS_CHARS = 4000


class PopupManager:
    """Owns all open popups and routes decisions back to the broker."""

    def __init__(self, root: tk.Tk, ui_queue: "queue.Queue[UIEvent]", broker: Broker) -> None:
        self._root = root
        self._queue = ui_queue
        self._broker = broker
        self._windows: dict[str, tk.Toplevel] = {}

    def start(self) -> None:
        """Begin polling the event queue."""
        self._root.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            if event.kind == "show" and event.request is not None:
                self._show(event.request)
            elif event.kind == "close" and event.request_id is not None:
                self._close(event.request_id)
        self._root.after(POLL_MS, self._poll)

    def _show(self, req: ApprovalRequest) -> None:
        win = tk.Toplevel(self._root)
        win.title("MCP Guard — approval required")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        offset = 30 * (len(self._windows) % 8)  # cascade simultaneous popups
        win.geometry(f"+{200 + offset}+{150 + offset}")

        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Claude wants to run a tool", font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        rows = [
            ("Client", req.client),
            ("Server", req.server_name),
            ("Method", req.method),
            ("Tool", req.tool_name or "—"),
            ("Time", req.received_at.astimezone().strftime("%H:%M:%S")),
        ]
        for i, (label, value) in enumerate(rows, start=1):
            ttk.Label(frame, text=f"{label}:", font=("Segoe UI", 9, "bold")).grid(
                row=i, column=0, sticky="nw", padx=(0, 8)
            )
            ttk.Label(frame, text=value).grid(row=i, column=1, sticky="w")

        ttk.Label(frame, text="Arguments:", font=("Segoe UI", 9, "bold")).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(8, 2)
        )
        args_text = json.dumps(req.arguments, indent=2, ensure_ascii=False)
        if len(args_text) > MAX_ARGS_CHARS:
            args_text = args_text[:MAX_ARGS_CHARS] + "\n… (truncated)"
        box = tk.Text(frame, width=60, height=min(12, args_text.count("\n") + 2), wrap="word")
        box.insert("1.0", args_text)  # plain text only, never interpreted
        box.configure(state="disabled")
        box.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="we")

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(rows) + 3, column=0, columnspan=2, pady=(12, 0), sticky="e")
        allow = ttk.Button(
            buttons, text="Allow", state="disabled", command=lambda: self._decide(req, "allow")
        )
        block = ttk.Button(
            buttons, text="Block", state="disabled", command=lambda: self._decide(req, "block")
        )
        block.pack(side="right")
        allow.pack(side="right", padx=(0, 8))

        def arm() -> None:
            if win.winfo_exists():
                allow.configure(state="normal")
                block.configure(state="normal")

        win.after(ARM_DELAY_MS, arm)
        # Closing the window counts as Block.
        win.protocol("WM_DELETE_WINDOW", lambda: self._decide(req, "block"))
        win.lift()
        win.focus_force()
        win.bell()
        self._windows[req.request_id] = win

    def _decide(self, req: ApprovalRequest, decision: str) -> None:
        self._broker.resolve_threadsafe(req.request_id, "allow" if decision == "allow" else "block")
        self._close(req.request_id)

    def _close(self, request_id: str) -> None:
        win = self._windows.pop(request_id, None)
        if win is not None and win.winfo_exists():
            win.destroy()
