"""Tests de LLMModule (NullProvider, historial, degradación) y short-term."""

from __future__ import annotations

import asyncio

from alice.brain.llm import LLMModule, LLMProvider, LLMRequest, LLMResponse, NullLLMProvider
from alice.brain.memory import InMemoryShortTermMemory, MemoryItem
from alice.core.event_bus import EventBus
from alice.core.events import LLM_FINISHED, LLM_REQUESTED, Event
from alice.core.payloads import LLMFinishedPayload


class _CapturingProvider(LLMProvider):
    """Captura el último request para inspeccionar system prompt e historial."""

    def __init__(self) -> None:
        self.last_request: LLMRequest | None = None

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.last_request = request
        return LLMResponse(text="ok", model="capturing")

    async def health_check(self) -> bool:
        return True


class _FailingProvider(LLMProvider):
    """Simula un proveedor caído (conexión rechazada)."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise ConnectionError("servidor no disponible")

    async def health_check(self) -> bool:
        return False


async def test_llm_module_emits_finished_with_null_provider() -> None:
    bus = EventBus(max_queue_size=50)
    module = LLMModule(bus=bus, provider=NullLLMProvider())
    await module.start()

    finished: list[Event] = []
    bus.subscribe(LLM_FINISHED, lambda e: _collect(finished, e))

    task = bus.start_consumer()
    await bus.publish(Event(type=LLM_REQUESTED, source="test", payload={"prompt": "hola"}))
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await module.stop()

    assert len(finished) == 1
    payload = LLMFinishedPayload.model_validate(finished[0].payload)
    assert payload.model == "null"
    assert payload.text


async def test_llm_module_injects_history_and_system_prompt() -> None:
    bus = EventBus(max_queue_size=50)
    short_term = InMemoryShortTermMemory(max_items=50)
    short_term.add(MemoryItem(kind="user_turn", content={"text": "hola"}))
    short_term.add(MemoryItem(kind="alice_turn", content={"text": "¿en qué ayudo?"}))
    short_term.add(MemoryItem(kind="user_turn", content={"text": "turno actual"}))  # se excluye
    provider = _CapturingProvider()
    module = LLMModule(
        bus=bus,
        provider=provider,
        short_term=short_term,
        system_prompt="SOY ALICE",
        history_turns=6,
    )
    await module.start()

    task = bus.start_consumer()
    await bus.publish(Event(type=LLM_REQUESTED, source="test", payload={"prompt": "pregunta"}))
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await module.stop()

    req = provider.last_request
    assert req is not None
    assert req.system == "SOY ALICE"
    roles = [(m.role, m.content) for m in req.messages]
    # Historial (sin el turno actual) + el prompt como último mensaje user.
    assert ("user", "hola") in roles
    assert ("assistant", "¿en qué ayudo?") in roles
    assert roles[-1] == ("user", "pregunta")
    assert ("user", "turno actual") not in roles  # el turno actual no se duplica


async def test_llm_module_degrades_on_provider_error() -> None:
    bus = EventBus(max_queue_size=50)
    module = LLMModule(bus=bus, provider=_FailingProvider())
    await module.start()

    finished: list[Event] = []
    bus.subscribe(LLM_FINISHED, lambda e: _collect(finished, e))

    task = bus.start_consumer()
    await bus.publish(Event(type=LLM_REQUESTED, source="test", payload={"prompt": "hola"}))
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await module.stop()

    assert len(finished) == 1
    payload = LLMFinishedPayload.model_validate(finished[0].payload)
    assert payload.success is False
    assert payload.error is not None


def test_short_term_memory_is_bounded() -> None:
    mem = InMemoryShortTermMemory(max_items=3)
    for i in range(5):
        mem.add(MemoryItem(kind="turn", content={"i": i}))
    recent = mem.recent(limit=10)
    assert len(recent) == 3  # buffer acotado descarta los más viejos
    assert recent[-1].content["i"] == 4


async def _collect(sink: list[Event], event: Event) -> None:
    sink.append(event)
