"""Tests del MemoryModule: grabación de turnos, escrituras explícitas e historial."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from alice.brain.memory import InMemoryShortTermMemory, MemoryItem
from alice.brain.memory_module import MemoryModule
from alice.brain.memory_sqlite import (
    SqliteEpisodicMemory,
    SqliteLongTermMemory,
    open_database,
)
from alice.core.event_bus import EventBus
from alice.core.events import (
    COMMAND_RECEIVED,
    MEMORY_STORE_REQUESTED,
    RESPONSE_READY,
    Event,
)


class _Kit(NamedTuple):
    bus: EventBus
    module: MemoryModule
    episodic: SqliteEpisodicMemory
    long_term: SqliteLongTermMemory
    short_term: InMemoryShortTermMemory


def _make_module(tmp_path: Path) -> _Kit:
    conn = open_database(tmp_path / "alice.db")
    episodic = SqliteEpisodicMemory(conn)
    long_term = SqliteLongTermMemory(conn)
    short_term = InMemoryShortTermMemory(max_items=50)
    bus = EventBus(max_queue_size=100)
    module = MemoryModule(bus=bus, episodic=episodic, long_term=long_term, short_term=short_term)
    return _Kit(bus, module, episodic, long_term, short_term)


async def test_records_user_and_alice_turns(tmp_path: Path) -> None:
    bus, module, episodic, _lt, short_term = _make_module(tmp_path)
    await module.start()
    task = bus.start_consumer()

    await bus.publish(
        Event(type=COMMAND_RECEIVED, source="test", payload={"text": "hola alice"})
    )
    await bus.publish(
        Event(type=RESPONSE_READY, source="test", payload={"text": "hola, ¿en qué ayudo?"})
    )
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await module.stop()

    episodes = episodic.query_by_date(datetime.now(UTC).date())
    kinds = [e.kind for e in episodes]
    assert "user_turn" in kinds
    assert "alice_turn" in kinds
    # Los mismos turnos alimentan la short-term.
    assert len(short_term.recent(10)) == 2


async def test_store_requested_writes_long_term(tmp_path: Path) -> None:
    bus, module, _ep, long_term, _st = _make_module(tmp_path)
    await module.start()
    task = bus.start_consumer()

    await bus.publish(
        Event(
            type=MEMORY_STORE_REQUESTED,
            source="test",
            payload={"store": "long_term", "key": "user.nombre", "value": "Adrián"},
        )
    )
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await module.stop()

    assert long_term.get("user.nombre") == "Adrián"


async def test_store_requested_writes_episodic(tmp_path: Path) -> None:
    bus, module, episodic, _lt, _st = _make_module(tmp_path)
    await module.start()
    task = bus.start_consumer()

    await bus.publish(
        Event(
            type=MEMORY_STORE_REQUESTED,
            source="test",
            payload={
                "store": "episodic",
                "kind": "user_fact",
                "content": {"text": "le gusta el té"},
            },
        )
    )
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await module.stop()

    episodes = episodic.query_by_date(datetime.now(UTC).date())
    assert any(e.kind == "user_fact" for e in episodes)


def test_short_term_recent_excludes_nothing_by_default() -> None:
    # Sanity: la short-term devuelve en orden y acotada.
    st = InMemoryShortTermMemory(max_items=3)
    for i in range(5):
        st.add(MemoryItem(kind="user_turn", content={"text": str(i)}))
    recent = st.recent(10)
    assert [r.content["text"] for r in recent] == ["2", "3", "4"]
