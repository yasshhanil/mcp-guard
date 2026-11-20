"""Daemon entry point: wires the FastAPI server thread to the Tkinter main thread.

Main thread  -> Tkinter mainloop (popups)
Thread 2     -> uvicorn + FastAPI + Broker (asyncio)
"""

from __future__ import annotations

import asyncio
import logging
import queue
import signal
import threading
import tkinter as tk

import uvicorn

from mcp_guard.core import config
from mcp_guard.daemon.app import create_app
from mcp_guard.daemon.broker import Broker, UIEvent
from mcp_guard.ui.popup import PopupManager

log = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """uvicorn server that exposes its event loop and blocks pending requests on exit."""

    def __init__(self, cfg: uvicorn.Config, broker: Broker) -> None:
        super().__init__(cfg)
        self.broker = broker
        self.loop: asyncio.AbstractEventLoop | None = None

    async def serve(self, sockets=None) -> None:  # type: ignore[no-untyped-def]
        self.loop = asyncio.get_running_loop()
        await super().serve(sockets)

    def install_signal_handlers(self) -> None:
        """Signals are handled by the main (Tk) thread instead."""


def run_daemon() -> None:
    """Run the approval daemon until the user quits (Ctrl+C)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = config.load_or_create_token()
    ui_queue: queue.Queue[UIEvent] = queue.Queue()
    broker = Broker(
        ui_queue, timeout_s=config.DECISION_TIMEOUT_S, max_pending=config.MAX_PENDING
    )

    app = create_app(broker, token)
    server = _Server(
        uvicorn.Config(app, host=config.DAEMON_HOST, port=config.DAEMON_PORT, log_level="warning"),
        broker,
    )
    server_thread = threading.Thread(target=server.run, name="mcp-guard-api", daemon=True)
    server_thread.start()

    root = tk.Tk()
    root.withdraw()  # no main window; popups only
    PopupManager(root, ui_queue, broker).start()

    def stop(*_: object) -> None:
        log.info("shutting down")
        if server.loop is not None:
            server.loop.call_soon_threadsafe(broker.shutdown)
        server.should_exit = True
        root.after(300, root.quit)

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)  # Ctrl+Break on Windows

    def watchdog() -> None:
        # Keeps Python's signal handling responsive and exits if the API thread dies.
        if not server_thread.is_alive():
            log.error("API server stopped (is port %s in use?)", config.DAEMON_PORT)
            root.quit()
            return
        root.after(200, watchdog)

    root.after(200, watchdog)
    log.info("MCP Guard daemon listening on %s (Ctrl+C to quit)", config.DAEMON_URL)
    root.mainloop()
    server.should_exit = True
    server_thread.join(timeout=3)
