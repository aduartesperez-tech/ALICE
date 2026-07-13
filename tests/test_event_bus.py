"""Tests del EventBus: prioridad, comodín, unsubscribe, handler que falla, cola llena."""

from __future__ import annotations

import asyncio

from alice.core.event_bus import EventBus
from alice.core.events import Event, EventPriority


def _event(type_: str, priority: EventPriority = EventPriority.NORMAL) -> Event:
    return Event(type=type_, source="test", priority=priority)


async def _drain(bus: EventBus) -> None:
    """Espera a que la cola quede vacía sin detener el bus."""
    await bus._queue.join()  # noqa: SLF001 - acceso controlado en test


async def test_publish_and_receive(running_bus: EventBus) -> None:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    running_bus.subscribe("a.b", handler)
    await running_bus.publish(_event("a.b"))
    await _drain(running_bus)

    assert len(received) == 1
    assert received[0].type == "a.b"


async def test_wildcard_receives_all(running_bus: EventBus) -> None:
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event.type)

    running_bus.subscribe("*", handler)
    await running_bus.publish(_event("x.one"))
    await running_bus.publish(_event("y.two"))
    await _drain(running_bus)

    assert set(seen) == {"x.one", "y.two"}


async def test_priority_ordering() -> None:
    # Bus sin consumidor: publicamos varias prioridades y luego consumimos a mano.
    bus = EventBus(max_queue_size=10)
    order: list[str] = []

    async def handler(event: Event) -> None:
        order.append(event.type)

    bus.subscribe("*", handler)
    await bus.publish(_event("low", EventPriority.LOW))
    await bus.publish(_event("critical", EventPriority.CRITICAL))
    await bus.publish(_event("normal", EventPriority.NORMAL))
    await bus.publish(_event("high", EventPriority.HIGH))

    task = bus.start_consumer()
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)

    assert order == ["critical", "high", "normal", "low"]


async def test_fifo_within_same_priority() -> None:
    bus = EventBus(max_queue_size=10)
    order: list[str] = []

    async def handler(event: Event) -> None:
        order.append(event.type)

    bus.subscribe("*", handler)
    for i in range(5):
        await bus.publish(_event(f"n{i}", EventPriority.NORMAL))

    task = bus.start_consumer()
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)

    assert order == ["n0", "n1", "n2", "n3", "n4"]


async def test_unsubscribe(running_bus: EventBus) -> None:
    count = 0

    async def handler(event: Event) -> None:
        nonlocal count
        count += 1

    sub = running_bus.subscribe("a.b", handler)
    await running_bus.publish(_event("a.b"))
    await _drain(running_bus)
    running_bus.unsubscribe(sub)
    await running_bus.publish(_event("a.b"))
    await _drain(running_bus)

    assert count == 1


async def test_failing_handler_does_not_kill_bus(running_bus: EventBus) -> None:
    good_calls = 0

    async def bad(event: Event) -> None:
        raise RuntimeError("boom")

    async def good(event: Event) -> None:
        nonlocal good_calls
        good_calls += 1

    running_bus.subscribe("a.b", bad)
    running_bus.subscribe("a.b", good)
    await running_bus.publish(_event("a.b"))
    await running_bus.publish(_event("a.b"))
    await _drain(running_bus)

    assert good_calls == 2  # el bus sigue vivo pese a la excepción del otro handler


async def test_full_queue_does_not_block() -> None:
    bus = EventBus(max_queue_size=2)  # sin consumidor: la cola se llena
    await bus.publish(_event("a", EventPriority.LOW))
    await bus.publish(_event("b", EventPriority.LOW))
    # La tercera excede el tamaño; publish no debe bloquear ni lanzar.
    await asyncio.wait_for(bus.publish(_event("c", EventPriority.LOW)), timeout=0.5)
