"""M0 acceptance: the asyncio runtime starts, bridges work in, and stops cleanly.

This proves the threading model — the riskiest, least-reference-backed part of
the plugin — in isolation, before anything depends on it. No Indigo, no
matter-server.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from async_runtime import AsyncRuntime


@pytest.fixture
def runtime(mock_logger):
    rt = AsyncRuntime(mock_logger)
    rt.start()
    yield rt
    rt.stop()


def test_start_brings_loop_up(runtime):
    assert runtime.is_running
    assert runtime.loop is not None
    assert runtime.loop.is_running()


def test_submit_round_trips_a_coroutine_result(runtime):
    async def add(a, b):
        await asyncio.sleep(0)
        return a + b

    future = runtime.submit(add(2, 3))
    assert future.result(timeout=5) == 5


def test_submit_runs_on_the_loop_thread_not_the_caller(runtime):
    caller_thread = threading.current_thread().name

    async def whoami():
        return threading.current_thread().name

    loop_thread = runtime.submit(whoami()).result(timeout=5)
    assert loop_thread == "matter-asyncio"
    assert loop_thread != caller_thread


def test_submit_propagates_exceptions_to_the_future(runtime):
    async def boom():
        raise ValueError("kaboom")

    future = runtime.submit(boom())
    with pytest.raises(ValueError, match="kaboom"):
        future.result(timeout=5)


def test_call_soon_fire_and_forget(runtime):
    flag = threading.Event()
    runtime.call_soon(flag.set)
    assert flag.wait(timeout=5)


def test_stop_is_idempotent_and_clean(mock_logger):
    rt = AsyncRuntime(mock_logger)
    rt.start()
    assert rt.is_running
    rt.stop()
    assert not rt.is_running
    # second stop must not raise
    rt.stop()
    assert not rt.is_running


def test_submit_after_stop_raises(mock_logger):
    rt = AsyncRuntime(mock_logger)
    rt.start()
    rt.stop()

    async def noop():
        return 1

    coro = noop()
    with pytest.raises(RuntimeError, match="not running"):
        rt.submit(coro)
    coro.close()  # avoid "coroutine was never awaited" warning


def test_double_start_raises(runtime):
    with pytest.raises(RuntimeError, match="already started"):
        runtime.start()
