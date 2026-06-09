"""Single asyncio event loop hosted on a dedicated background thread.

Indigo plugins run on a synchronous main thread with lifecycle callbacks. This
plugin, unlike the rest of the workspace, needs a long-lived async I/O subsystem
— a persistent WebSocket client to matter-server — plus short-lived coroutines
bridged in from Indigo threads (commissioning, decommission, diagnostics). All
of these live on the single loop owned here. (The Domio HTTP API is NOT on this
loop: it is served by the Indigo Web Server as hidden-action handlers — see
http_handlers.py and docs/IMPLEMENTATION.md §4.)

The rule from the implementation notes — "never call ``asyncio.run`` from
Indigo's main thread" — is honoured by running ``run_forever()`` on our own
thread and only ever crossing into it through the threadsafe bridge below:

- Indigo -> asyncio: :meth:`AsyncRuntime.submit` (returns a
  ``concurrent.futures.Future`` so the caller can block with a timeout).
- asyncio -> Indigo: nothing special is needed — ``updateStatesOnServer`` is a
  thread-safe IPC call and may be invoked directly from the loop thread.

This module has no Indigo dependency, so it is unit-testable in isolation.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Callable, Coroutine, Optional


class AsyncRuntime:
    """Owns the plugin's single event loop and the thread it runs on."""

    def __init__(self, logger: Any) -> None:
        self.logger = logger
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle (called from Indigo's startup()/shutdown() threads)
    # ------------------------------------------------------------------
    def start(self, timeout: float = 10.0) -> None:
        """Spin up the loop thread and block until the loop is running."""
        if self._thread is not None:
            raise RuntimeError("AsyncRuntime already started")
        self._thread = threading.Thread(
            target=self._thread_main, name="matter-asyncio", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError(f"asyncio loop failed to start within {timeout}s")

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            self._drain_and_close(loop)

    @staticmethod
    def _drain_and_close(loop: asyncio.AbstractEventLoop) -> None:
        """Cancel outstanding tasks and close the loop cleanly."""
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

    def stop(self, timeout: float = 8.0) -> None:
        """Ask the loop to stop and join its thread."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Keep the refs: clearing them would let a later start() spin up a
                # second loop thread alongside this orphaned one.
                self.logger.warning("asyncio thread did not exit within %.1fs", timeout)
                return
        self._thread = None
        self._loop = None
        self._ready.clear()

    # ------------------------------------------------------------------
    # The Indigo -> asyncio bridge
    # ------------------------------------------------------------------
    def submit(self, coro: Coroutine) -> "Future[Any]":
        """Schedule *coro* on the loop from any thread.

        Returns a ``concurrent.futures.Future``; call ``.result(timeout=...)``
        to block the calling (Indigo) thread until the coroutine completes.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            raise RuntimeError("asyncio runtime is not running")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        """Fire-and-forget a plain callable on the loop thread."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(fn, *args)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    @property
    def is_running(self) -> bool:
        loop = self._loop
        return bool(loop is not None and loop.is_running())
