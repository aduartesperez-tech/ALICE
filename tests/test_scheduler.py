"""Tests del Scheduler: once, interval, cancel y resiliencia a excepciones."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from alice.core.scheduler import Scheduler


async def test_schedule_once_runs() -> None:
    sched = Scheduler()
    sched.start()
    fired = asyncio.Event()

    async def cb() -> None:
        fired.set()

    sched.schedule_once(timedelta(seconds=0.02), cb)
    await asyncio.wait_for(fired.wait(), timeout=1.0)
    await sched.stop()


async def test_schedule_interval_runs_multiple_times() -> None:
    sched = Scheduler()
    sched.start()
    count = 0
    reached = asyncio.Event()

    async def cb() -> None:
        nonlocal count
        count += 1
        if count >= 3:
            reached.set()

    handle = sched.schedule_interval(timedelta(seconds=0.02), cb)
    await asyncio.wait_for(reached.wait(), timeout=1.0)
    sched.cancel(handle)
    await sched.stop()
    assert count >= 3


async def test_cancel_prevents_execution() -> None:
    sched = Scheduler()
    sched.start()
    fired = False

    async def cb() -> None:
        nonlocal fired
        fired = True

    handle = sched.schedule_once(timedelta(seconds=0.2), cb)
    sched.cancel(handle)
    await asyncio.sleep(0.3)
    await sched.stop()
    assert fired is False


async def test_interval_survives_exception() -> None:
    sched = Scheduler()
    sched.start()
    count = 0
    reached = asyncio.Event()

    async def cb() -> None:
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("boom")  # el job debe seguir programado
        if count >= 3:
            reached.set()

    handle = sched.schedule_interval(timedelta(seconds=0.02), cb)
    await asyncio.wait_for(reached.wait(), timeout=1.0)
    sched.cancel(handle)
    await sched.stop()
    assert count >= 3
