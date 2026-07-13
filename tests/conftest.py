"""Fixtures compartidas para los tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from alice.core.event_bus import EventBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def running_bus() -> AsyncIterator[EventBus]:
    """EventBus con su consumidor corriendo; drena y se detiene al terminar el test."""
    bus = EventBus(max_queue_size=100)
    task = bus.start_consumer()
    try:
        yield bus
    finally:
        await bus.stop()
        await asyncio.wait_for(task, timeout=1.0)
