"""Test de integración end-to-end SIN IA real (escenario de aceptación v1.1).

Monta el pipeline completo (Planner + Executor + ToolManager + LLMModule) sobre
el bus y verifica los flujos clave desde ``command.received`` hasta
``response.ready``, comprobando el encadenado por ``correlation_id``.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from alice.brain.executor import ActionExecutor
from alice.brain.llm import LLMModule, LLMProvider, LLMRequest, LLMResponse, NullLLMProvider
from alice.brain.plan import Action, ActionKind, Plan
from alice.brain.planner import Planner
from alice.core.event_bus import EventBus
from alice.core.events import (
    COMMAND_RECEIVED,
    PLAN_COMPLETED,
    PLAN_CREATED,
    PLAN_FAILED,
    RESPONSE_READY,
    Event,
)
from alice.core.payloads import ResponseReadyPayload
from alice.core.scheduler import Scheduler
from alice.tools.builtin.datetime_tool import DateTimeTool
from alice.tools.manager import ToolManager


class _Harness:
    """Sistema mínimo cableado (equivale a lo que compone main.py)."""

    def __init__(self, *, narrate: bool = False, provider: LLMProvider | None = None) -> None:
        self.bus = EventBus(max_queue_size=200)
        self.sched = Scheduler()
        self.tools = ToolManager(bus=self.bus, granted_permissions={"read_system"})
        self.tools.register(DateTimeTool())  # NO se registra "internet"
        self.executor = ActionExecutor(bus=self.bus, scheduler=self.sched)
        self.planner = Planner(bus=self.bus, narrate=narrate)
        self.llm = LLMModule(bus=self.bus, provider=provider or NullLLMProvider())
        self.responses: list[Event] = []
        self.completed: list[Event] = []
        self.failed: list[Event] = []
        self.bus.subscribe(RESPONSE_READY, self._on(self.responses))
        self.bus.subscribe(PLAN_COMPLETED, self._on(self.completed))
        self.bus.subscribe(PLAN_FAILED, self._on(self.failed))

    def _on(self, sink: list[Event]):
        async def handler(event: Event) -> None:
            sink.append(event)

        return handler

    async def start(self) -> None:
        self.sched.start()
        await self.tools.start()
        await self.llm.start()
        await self.executor.start()
        await self.planner.start()
        self._task = self.bus.start_consumer()

    async def send(self, text: str) -> UUID:
        event = Event(type=COMMAND_RECEIVED, source="test", payload={"text": text})
        await self.bus.publish(event)
        return event.id

    async def settle(self) -> None:
        await self.bus._queue.join()  # noqa: SLF001

    async def stop(self) -> None:
        await self.bus.stop()
        await asyncio.wait_for(self._task, timeout=2.0)
        await self.planner.stop()
        await self.executor.stop()
        await self.llm.stop()
        await self.tools.stop()
        await self.sched.stop()


async def test_what_time_is_it_end_to_end() -> None:
    h = _Harness()
    await h.start()
    command_id = await h.send("¿qué hora es?")
    await h.settle()
    await h.stop()

    # El plan datetime se ejecuta sin LLM y produce una respuesta estructurada.
    assert len(h.completed) == 1
    assert len(h.responses) == 1
    resp = ResponseReadyPayload.model_validate(h.responses[0].payload)
    assert resp.text is None  # sin LLM en v1.1
    assert resp.observations[0].kind == "datetime"
    assert "iso" in resp.observations[0].data

    # Todo el flujo queda encadenado por correlation_id == id del comando.
    assert h.responses[0].correlation_id == command_id
    assert h.completed[0].correlation_id == command_id


async def test_fallback_goes_through_llm_end_to_end() -> None:
    h = _Harness()
    await h.start()
    await h.send("cuéntame un chiste")
    await h.settle()
    await h.stop()

    # Sin regla -> call_llm -> respond. Con NullProvider, text es el fijo simulado.
    assert len(h.completed) == 1
    resp = ResponseReadyPayload.model_validate(h.responses[0].payload)
    assert resp.text is not None
    assert "simulada" in resp.text


class _ProseProvider(LLMProvider):
    """Provider que narra: devuelve prosa a partir del prompt estructurado."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="Son las tres de la tarde.", model="prose")

    async def health_check(self) -> bool:
        return True


class _DownProvider(LLMProvider):
    """Provider caído: siempre falla."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise ConnectionError("caído")

    async def health_check(self) -> bool:
        return False


async def test_datetime_narrated_end_to_end() -> None:
    h = _Harness(narrate=True, provider=_ProseProvider())
    await h.start()
    await h.send("¿qué hora es?")
    await h.settle()
    await h.stop()

    # Con narración, la respuesta lleva la prosa del LLM (no solo datos crudos).
    assert len(h.completed) == 1
    resp = ResponseReadyPayload.model_validate(h.responses[0].payload)
    assert resp.text == "Son las tres de la tarde."
    # Y las observaciones estructuradas siguen ahí (fuente de verdad).
    assert any(o.kind == "datetime" for o in resp.observations)


async def test_datetime_degrades_when_llm_down() -> None:
    h = _Harness(narrate=True, provider=_DownProvider())
    await h.start()
    await h.send("¿qué hora es?")
    await h.settle()
    await h.stop()

    # LLM caído: el plan NO falla; responde en crudo (sin text) con los datos.
    assert len(h.completed) == 1
    assert len(h.failed) == 0
    resp = ResponseReadyPayload.model_validate(h.responses[0].payload)
    assert resp.text is None
    assert any(o.kind == "datetime" for o in resp.observations)


async def test_missing_tool_fails_gracefully_end_to_end() -> None:
    # Un plan que referencia una tool inexistente debe degradar con gracia:
    # el ToolManager falla, el Executor emite plan.failed y el usuario recibe
    # el error (no un silencio). Se inyecta el plan directamente porque ya no
    # hay una regla que enrute a una tool que no existe.
    h = _Harness()
    await h.start()
    correlation_id = uuid4()
    plan = Plan(
        goal="Ejecutar una tool inexistente",
        rule="test_missing_tool",
        user_text="da igual",
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="no_existe", params={}),
            Action(kind=ActionKind.RESPOND),
        ],
    )
    await h.bus.publish(
        Event(
            type=PLAN_CREATED,
            source="test",
            correlation_id=correlation_id,
            payload=plan.model_dump(),
        )
    )
    await h.settle()
    await h.stop()

    assert len(h.failed) == 1
    assert len(h.responses) == 1
    resp = ResponseReadyPayload.model_validate(h.responses[0].payload)
    assert resp.observations[0].kind == "error"
    assert h.failed[0].correlation_id == correlation_id
