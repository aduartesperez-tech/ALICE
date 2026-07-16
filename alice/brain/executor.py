"""Action Executor: ejecuta los planes que produce el Planner, paso a paso.

El Planner decide (produce un ``Plan``); el Executor actúa (lo ejecuta). El
Executor NO decide nada: solo traduce cada ``Action`` a su destino y avanza
cuando llega el resultado, encadenando todo por ``correlation_id``.

Acciones síncronas (schedule, remember, set_state, respond) se ejecutan en el
acto; las asíncronas (use_tool, call_llm) publican una petición y esperan su
evento de resultado antes de avanzar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from alice.brain.context import render_for_llm
from alice.brain.plan import Action, ActionKind, Plan
from alice.core.events import (
    AGENT_STEP_FINISHED,
    AGENT_STEP_REQUESTED,
    LLM_FINISHED,
    LLM_REQUESTED,
    MEMORY_STORE_REQUESTED,
    PLAN_COMPLETED,
    PLAN_CREATED,
    PLAN_FAILED,
    RESPONSE_READY,
    TOOL_FINISHED,
    TOOL_REQUESTED,
    Event,
)
from alice.core.observation import Observation
from alice.core.payloads import (
    AgentStepFinishedPayload,
    AgentStepRequestedPayload,
    LLMFinishedPayload,
    LLMRequestedPayload,
    MemoryStoreRequestedPayload,
    PlanCompletedPayload,
    PlanFailedPayload,
    ResponseReadyPayload,
    ToolFinishedPayload,
    ToolRequestedPayload,
)
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.core.event_bus import EventBus, Subscription
    from alice.core.scheduler import AsyncCallback, JobHandle, Scheduler

_logger = get_logger("alice.brain.executor")


@dataclass
class _Execution:
    """Estado de un plan en curso, indexado por ``correlation_id``."""

    plan: Plan
    correlation_id: UUID
    index: int = 0
    observations: list[Observation] = field(default_factory=list)
    last_llm_text: str | None = None
    timeout_handle: JobHandle | None = None
    # Modo agente: el plan contiene pasos AGENT_STEP; en él los fallos de tool no
    # abortan (se convierten en observación y el bucle sigue) y se cuentan vueltas.
    agent_mode: bool = False
    agent_iters: int = 0


class ActionExecutor:
    """Módulo interno que ejecuta planes. Es un ``CoreModule``."""

    name = "executor"

    def __init__(
        self,
        *,
        bus: EventBus,
        scheduler: Scheduler,
        plan_timeout_seconds: float = 30.0,
        max_agent_iters: int = 6,
    ) -> None:
        self._bus = bus
        self._scheduler = scheduler
        self._timeout = plan_timeout_seconds
        self._max_agent_iters = max_agent_iters
        self._executions: dict[UUID, _Execution] = {}
        self._subs: list[Subscription] = []

    async def start(self) -> None:
        self._subs = [
            self._bus.subscribe(PLAN_CREATED, self._on_plan_created),
            self._bus.subscribe(TOOL_FINISHED, self._on_tool_finished),
            self._bus.subscribe(LLM_FINISHED, self._on_llm_finished),
            self._bus.subscribe(AGENT_STEP_FINISHED, self._on_agent_step_finished),
        ]
        _logger.info("executor.started")

    async def stop(self) -> None:
        for sub in self._subs:
            self._bus.unsubscribe(sub)
        self._subs.clear()

    # --- Entrada: nuevo plan --------------------------------------------------

    async def _on_plan_created(self, event: Event) -> None:
        plan = Plan.model_validate(event.payload)
        correlation_id = event.correlation_id or event.id
        execution = _Execution(plan=plan, correlation_id=correlation_id)
        execution.agent_mode = any(a.kind is ActionKind.AGENT_STEP for a in plan.actions)
        execution.timeout_handle = self._scheduler.schedule_once(
            timedelta(seconds=self._timeout), self._make_timeout(correlation_id)
        )
        self._executions[correlation_id] = execution
        _logger.info(
            "executor.plan_started",
            extra={
                "goal": plan.goal,
                "rule": plan.rule,
                "actions": plan.action_kinds,
                "correlation_id": str(correlation_id),
            },
        )
        await self._advance(execution)

    # --- Motor de avance ------------------------------------------------------

    async def _advance(self, execution: _Execution) -> None:
        """Ejecuta acciones en orden hasta encontrar una que requiera esperar."""
        while execution.index < len(execution.plan.actions):
            action = execution.plan.actions[execution.index]
            execution.index += 1

            if action.kind is ActionKind.USE_TOOL:
                await self._request_tool(execution, action)
                return  # espera tool.finished
            if action.kind is ActionKind.CALL_LLM:
                await self._request_llm(execution, action)
                return  # espera llm.finished
            if action.kind is ActionKind.AGENT_STEP:
                await self._request_agent_step(execution)
                return  # espera agent.step_finished
            if action.kind is ActionKind.SCHEDULE:
                self._do_schedule(execution, action)
            elif action.kind is ActionKind.REMEMBER:
                await self._do_remember(execution, action)  # fire-and-forget, no espera
            elif action.kind is ActionKind.RESPOND:
                await self._respond(execution)
                await self._complete(execution)
                return
            elif action.kind is ActionKind.SET_STATE:
                # Stub documentado: CognitiveState llega en v1.3.
                _logger.info(
                    "executor.action_stub",
                    extra={"kind": action.kind.value, "params": action.params},
                )

        # El plan terminó sin una acción RESPOND explícita.
        await self._complete(execution)

    async def _request_tool(self, execution: _Execution, action: Action) -> None:
        await self._bus.publish(
            Event(
                type=TOOL_REQUESTED,
                source="brain.executor",
                correlation_id=execution.correlation_id,
                payload=ToolRequestedPayload(
                    tool_name=action.target, params=action.params
                ).model_dump(),
            )
        )

    async def _request_llm(self, execution: _Execution, action: Action) -> None:
        prompt = render_for_llm(
            user_text=execution.plan.user_text,
            goal=execution.plan.goal,
            observations=execution.observations,
        )
        await self._bus.publish(
            Event(
                type=LLM_REQUESTED,
                source="brain.executor",
                correlation_id=execution.correlation_id,
                payload=LLMRequestedPayload(prompt=prompt).model_dump(),
            )
        )

    async def _request_agent_step(self, execution: _Execution) -> None:
        """Pide al AgentReasoner el siguiente paso, dándole lo observado hasta ahora."""
        await self._bus.publish(
            Event(
                type=AGENT_STEP_REQUESTED,
                source="brain.executor",
                correlation_id=execution.correlation_id,
                payload=AgentStepRequestedPayload(
                    user_text=execution.plan.user_text,
                    goal=execution.plan.goal,
                    observations=execution.observations,
                ).model_dump(),
            )
        )

    def _do_schedule(self, execution: _Execution, action: Action) -> None:
        """Programa un recordatorio: al dispararse, publica una respuesta al usuario."""
        seconds = float(action.params.get("seconds", 0))
        message = str(action.params.get("message", "recordatorio"))
        self._scheduler.schedule_once(
            timedelta(seconds=seconds), self._make_reminder(message)
        )
        execution.observations.append(
            Observation(
                source="scheduler",
                kind="reminder_set",
                data={"seconds": seconds, "message": message},
            )
        )

    async def _do_remember(self, execution: _Execution, action: Action) -> None:
        """Publica una petición de escritura en memoria (fire-and-forget)."""
        await self._bus.publish(
            Event(
                type=MEMORY_STORE_REQUESTED,
                source="brain.executor",
                correlation_id=execution.correlation_id,
                payload=MemoryStoreRequestedPayload(
                    store=str(action.params.get("store", "episodic")),
                    kind=str(action.params.get("kind", "event")),
                    content=action.params.get("content", {}),
                    key=action.params.get("key"),
                    value=action.params.get("value"),
                ).model_dump(),
            )
        )
        execution.observations.append(
            Observation(
                source="memory",
                kind="remembered",
                data={"store": action.params.get("store", "episodic")},
            )
        )

    # --- Resultados asíncronos ------------------------------------------------

    async def _on_tool_finished(self, event: Event) -> None:
        execution = self._executions.get(event.correlation_id) if event.correlation_id else None
        if execution is None:
            return
        payload = ToolFinishedPayload.model_validate(event.payload)
        if payload.success:
            execution.observations.append(
                Observation(
                    source=f"tool:{payload.tool_name}",
                    kind=payload.tool_name,
                    data=payload.output or {},
                )
            )
            await self._advance(execution)
        else:
            execution.observations.append(
                Observation(
                    source=f"tool:{payload.tool_name}",
                    kind="error",
                    data={"error": payload.error or "error desconocido"},
                )
            )
            if execution.agent_mode:
                # En el bucle agente, un fallo de tool no aborta: queda como
                # observación y el razonador decide qué hacer en el siguiente paso.
                await self._advance(execution)
            else:
                await self._fail(execution, "use_tool", payload.error or "error de herramienta")

    async def _on_llm_finished(self, event: Event) -> None:
        execution = self._executions.get(event.correlation_id) if event.correlation_id else None
        if execution is None:
            return
        payload = LLMFinishedPayload.model_validate(event.payload)
        # Degradación: si el LLM falló, seguimos sin narración (respuesta en crudo).
        if payload.success:
            execution.last_llm_text = payload.text
        else:
            _logger.warning(
                "executor.llm_degraded",
                extra={"error": payload.error, "correlation_id": str(execution.correlation_id)},
            )
        await self._advance(execution)

    async def _on_agent_step_finished(self, event: Event) -> None:
        """Un paso del bucle: o encola las tools pedidas y sigue, o cierra el turno."""
        execution = self._executions.get(event.correlation_id) if event.correlation_id else None
        if execution is None:
            return
        payload = AgentStepFinishedPayload.model_validate(event.payload)
        calls = payload.tool_calls

        if calls and execution.agent_iters < self._max_agent_iters:
            # El modelo pide más tools: se ejecutan y luego se le vuelve a preguntar.
            execution.agent_iters += 1
            for call in calls:
                execution.plan.actions.append(
                    Action(kind=ActionKind.USE_TOOL, target=call.name, params=dict(call.arguments))
                )
            execution.plan.actions.append(Action(kind=ActionKind.AGENT_STEP))
            _logger.info(
                "executor.agent_step",
                extra={
                    "correlation_id": str(execution.correlation_id),
                    "iter": execution.agent_iters,
                    "tools": [c.name for c in calls],
                },
            )
            await self._advance(execution)
            return

        # Fin del bucle: respuesta final directa, presupuesto agotado, o degradación.
        if payload.text:
            execution.last_llm_text = payload.text
            execution.plan.actions.append(Action(kind=ActionKind.RESPOND))
        else:
            # Sin texto (tope de vueltas con tools pendientes, o proveedor caído):
            # narramos lo observado con el LLM; si también falla, sale en crudo.
            if calls:
                _logger.info(
                    "executor.agent_budget_exhausted",
                    extra={
                        "correlation_id": str(execution.correlation_id),
                        "iters": execution.agent_iters,
                    },
                )
            execution.plan.actions.extend(
                [Action(kind=ActionKind.CALL_LLM, target="llm"), Action(kind=ActionKind.RESPOND)]
            )
        await self._advance(execution)

    # --- Salida y cierre ------------------------------------------------------

    async def _respond(self, execution: _Execution) -> None:
        await self._bus.publish(
            Event(
                type=RESPONSE_READY,
                source="brain.executor",
                correlation_id=execution.correlation_id,
                payload=ResponseReadyPayload(
                    text=execution.last_llm_text,
                    observations=execution.observations,
                    goal=execution.plan.goal,
                ).model_dump(),
            )
        )

    async def _complete(self, execution: _Execution) -> None:
        self._cancel_timeout(execution)
        self._executions.pop(execution.correlation_id, None)
        await self._bus.publish(
            Event(
                type=PLAN_COMPLETED,
                source="brain.executor",
                correlation_id=execution.correlation_id,
                payload=PlanCompletedPayload(
                    goal=execution.plan.goal, rule=execution.plan.rule
                ).model_dump(),
            )
        )
        _logger.info(
            "executor.plan_completed",
            extra={"goal": execution.plan.goal, "correlation_id": str(execution.correlation_id)},
        )

    async def _fail(self, execution: _Execution, failed_action: str, error: str) -> None:
        self._cancel_timeout(execution)
        self._executions.pop(execution.correlation_id, None)
        # El usuario recibe el error (en crudo); no se lo tragamos en silencio.
        await self._respond(execution)
        await self._bus.publish(
            Event(
                type=PLAN_FAILED,
                source="brain.executor",
                correlation_id=execution.correlation_id,
                payload=PlanFailedPayload(
                    goal=execution.plan.goal,
                    rule=execution.plan.rule,
                    failed_action=failed_action,
                    error=error,
                ).model_dump(),
            )
        )
        _logger.warning(
            "executor.plan_failed",
            extra={
                "goal": execution.plan.goal,
                "failed_action": failed_action,
                "error": error,
                "correlation_id": str(execution.correlation_id),
            },
        )

    def _cancel_timeout(self, execution: _Execution) -> None:
        if execution.timeout_handle is not None:
            self._scheduler.cancel(execution.timeout_handle)
            execution.timeout_handle = None

    # --- Callbacks de scheduler ----------------------------------------------

    def _make_timeout(self, correlation_id: UUID) -> AsyncCallback:
        async def _timeout() -> None:
            execution = self._executions.get(correlation_id)
            if execution is not None:
                execution.timeout_handle = None  # ya disparó; no cancelar
                await self._fail(execution, "timeout", "timeout de plan")

        return _timeout

    def _make_reminder(self, message: str) -> AsyncCallback:
        reminder_obs = Observation(source="scheduler", kind="reminder", data={"message": message})

        async def _reminder() -> None:
            await self._bus.publish(
                Event(
                    type=RESPONSE_READY,
                    source="brain.executor",
                    correlation_id=uuid4(),
                    payload=ResponseReadyPayload(
                        observations=[reminder_obs], goal="recordatorio"
                    ).model_dump(),
                )
            )

        return _reminder
