"""Tests del Action Executor: avance de pasos, respuesta, fallo, timeout, schedule."""

from __future__ import annotations

import asyncio

from alice.brain.executor import ActionExecutor
from alice.brain.plan import Action, ActionKind, Plan
from alice.core.event_bus import EventBus
from alice.core.events import (
    PLAN_COMPLETED,
    PLAN_CREATED,
    PLAN_FAILED,
    RESPONSE_READY,
    TOOL_REQUESTED,
    Event,
)
from alice.core.payloads import (
    PlanFailedPayload,
    ResponseReadyPayload,
)
from alice.core.scheduler import Scheduler
from alice.tools.builtin.datetime_tool import DateTimeTool
from alice.tools.manager import ToolManager


class _Sink:
    def __init__(self, bus: EventBus) -> None:
        self.responses: list[Event] = []
        self.completed: list[Event] = []
        self.failed: list[Event] = []
        bus.subscribe(RESPONSE_READY, self._on(self.responses))
        bus.subscribe(PLAN_COMPLETED, self._on(self.completed))
        bus.subscribe(PLAN_FAILED, self._on(self.failed))

    def _on(self, sink: list[Event]):
        async def handler(event: Event) -> None:
            sink.append(event)

        return handler


async def _run(bus: EventBus, plan: Plan) -> None:
    task = bus.start_consumer()
    await bus.publish(Event(type=PLAN_CREATED, source="test", payload=plan.model_dump()))
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_datetime_plan_completes_with_observation() -> None:
    bus = EventBus(max_queue_size=100)
    sched = Scheduler()
    sched.start()
    tools = ToolManager(bus=bus, granted_permissions={"read_system"})
    tools.register(DateTimeTool())
    executor = ActionExecutor(bus=bus, scheduler=sched)
    await tools.start()
    await executor.start()
    sink = _Sink(bus)

    plan = Plan(
        goal="hora",
        rule="datetime",
        user_text="¿qué hora es?",
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="datetime"),
            Action(kind=ActionKind.RESPOND),
        ],
    )
    await _run(bus, plan)
    await executor.stop()
    await tools.stop()
    await sched.stop()

    assert len(sink.completed) == 1
    assert len(sink.responses) == 1
    resp = ResponseReadyPayload.model_validate(sink.responses[0].payload)
    # Sin LLM en v1.1: text es None y los datos van estructurados en observations.
    assert resp.text is None
    assert len(resp.observations) == 1
    assert resp.observations[0].kind == "datetime"
    assert "iso" in resp.observations[0].data


async def test_unknown_tool_fails_plan_and_still_responds() -> None:
    bus = EventBus(max_queue_size=100)
    sched = Scheduler()
    sched.start()
    tools = ToolManager(bus=bus, granted_permissions={"read_system"})  # sin "internet"
    executor = ActionExecutor(bus=bus, scheduler=sched)
    await tools.start()
    await executor.start()
    sink = _Sink(bus)

    plan = Plan(
        goal="buscar",
        rule="internet_search",
        user_text="busca X",
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="internet", params={"query": "X"}),
            Action(kind=ActionKind.CALL_LLM, target="llm"),
            Action(kind=ActionKind.RESPOND),
        ],
    )
    await _run(bus, plan)
    await executor.stop()
    await tools.stop()
    await sched.stop()

    # El plan aborta en el primer paso, pero el usuario recibe el error.
    assert len(sink.failed) == 1
    failed = PlanFailedPayload.model_validate(sink.failed[0].payload)
    assert failed.failed_action == "use_tool"
    assert len(sink.responses) == 1
    resp = ResponseReadyPayload.model_validate(sink.responses[0].payload)
    assert resp.observations[0].kind == "error"


async def test_step_output_becomes_tool_request() -> None:
    # Verifica que el executor traduce USE_TOOL a un tool.requested con el target correcto.
    bus = EventBus(max_queue_size=100)
    sched = Scheduler()
    sched.start()
    executor = ActionExecutor(bus=bus, scheduler=sched)
    await executor.start()

    requests: list[Event] = []
    bus.subscribe(TOOL_REQUESTED, lambda e: _collect(requests, e))

    plan = Plan(
        goal="x",
        rule="datetime",
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="datetime"),
            Action(kind=ActionKind.RESPOND),
        ],
    )
    task = bus.start_consumer()
    await bus.publish(Event(type=PLAN_CREATED, source="test", payload=plan.model_dump()))
    await asyncio.sleep(0.05)  # deja que se emita tool.requested (nadie responde)
    await bus.stop()
    await asyncio.wait_for(task, timeout=2.0)
    await executor.stop()
    await sched.stop()

    assert len(requests) == 1
    assert requests[0].payload["tool_name"] == "datetime"


async def test_plan_timeout_fails() -> None:
    bus = EventBus(max_queue_size=100)
    sched = Scheduler()
    sched.start()
    # Executor con timeout muy corto; nadie responde al tool.requested -> timeout.
    executor = ActionExecutor(bus=bus, scheduler=sched, plan_timeout_seconds=0.1)
    await executor.start()
    sink = _Sink(bus)

    plan = Plan(
        goal="cuelga",
        rule="datetime",
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="datetime"),
            Action(kind=ActionKind.RESPOND),
        ],
    )
    task = bus.start_consumer()
    await bus.publish(Event(type=PLAN_CREATED, source="test", payload=plan.model_dump()))
    await asyncio.sleep(0.3)  # supera el timeout del plan
    await bus.stop()
    await asyncio.wait_for(task, timeout=2.0)
    await executor.stop()
    await sched.stop()

    assert len(sink.failed) == 1
    failed = PlanFailedPayload.model_validate(sink.failed[0].payload)
    assert failed.failed_action == "timeout"


async def test_schedule_action_fires_reminder() -> None:
    bus = EventBus(max_queue_size=100)
    sched = Scheduler()
    sched.start()
    executor = ActionExecutor(bus=bus, scheduler=sched)
    await executor.start()
    sink = _Sink(bus)

    plan = Plan(
        goal="recordar",
        rule="reminder",
        actions=[
            Action(
                kind=ActionKind.SCHEDULE,
                target="scheduler",
                params={"seconds": 0.05, "message": "estirar"},
            ),
            Action(kind=ActionKind.RESPOND),
        ],
    )
    task = bus.start_consumer()
    await bus.publish(Event(type=PLAN_CREATED, source="test", payload=plan.model_dump()))
    await asyncio.sleep(0.2)  # deja disparar el recordatorio
    await bus.stop()
    await asyncio.wait_for(task, timeout=2.0)
    await executor.stop()
    await sched.stop()

    # Dos respuestas: la confirmación inmediata + el recordatorio al dispararse.
    kinds = [
        ResponseReadyPayload.model_validate(e.payload).observations[0].kind for e in sink.responses
    ]
    assert "reminder_set" in kinds
    assert "reminder" in kinds


async def _collect(sink: list[Event], event: Event) -> None:
    sink.append(event)
