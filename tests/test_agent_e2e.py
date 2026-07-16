"""Test E2E del bucle agente: Executor + AgentReasoner + ToolManager.

Con un proveedor falso guionizado (una secuencia de AgentStep por vuelta) se
verifica el bucle completo percibir→razonar→actuar→repetir: encadenar tools,
tolerar fallos de tool sin abortar, y respetar el presupuesto de vueltas.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from alice.brain.agent_reasoner import AgentReasoner
from alice.brain.executor import ActionExecutor
from alice.brain.llm import (
    AgentStep,
    LLMModule,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    ToolSpec,
)
from alice.brain.plan import Action, ActionKind, Plan
from alice.brain.tool_catalog import build_tool_specs
from alice.core.event_bus import EventBus
from alice.core.events import (
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
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult


class _ScriptProvider(LLMProvider):
    """Reproduce una lista de AgentStep, uno por vuelta; luego repite el último.

    ``generate`` (usado por CALL_LLM al agotar el presupuesto) devuelve un texto fijo.
    """

    def __init__(self, steps: list[AgentStep], *, narration: str = "respuesta narrada") -> None:
        self._steps = steps
        self._i = 0
        self._narration = narration

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self._narration, model="fake")

    async def health_check(self) -> bool:
        return True

    async def run_agent_step(self, request: LLMRequest, tools: list[ToolSpec]) -> AgentStep:
        step = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        return step


class _FailParams(BaseModel):
    pass


class _FailingTool(Tool):
    """Tool que siempre falla: para probar la tolerancia del bucle."""

    definition = ToolDefinition(
        name="rompe",
        description="siempre falla",
        parameters=_FailParams,
        permissions={Permission.READ_SYSTEM},
    )

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(success=False, error="boom")


class _Harness:
    def __init__(self, provider: LLMProvider, *, max_agent_iters: int = 6) -> None:
        self.bus = EventBus(max_queue_size=300)
        self.sched = Scheduler()
        self.tools = ToolManager(bus=self.bus, granted_permissions={"read_system"})
        self.tools.register(DateTimeTool())
        self.tools.register(_FailingTool())
        self.reasoner = AgentReasoner(
            bus=self.bus, provider=provider, tools=build_tool_specs(self.tools.definitions())
        )
        self.llm = LLMModule(bus=self.bus, provider=provider)
        self.executor = ActionExecutor(
            bus=self.bus, scheduler=self.sched, max_agent_iters=max_agent_iters
        )
        self.responses: list[Event] = []
        self.completed: list[Event] = []
        self.failed: list[Event] = []
        self.bus.subscribe(RESPONSE_READY, self._on(self.responses))
        self.bus.subscribe(PLAN_COMPLETED, self._on(self.completed))
        self.bus.subscribe(PLAN_FAILED, self._on(self.failed))

    def _on(self, sink: list[Event]):  # noqa: ANN202 - helper de test
        async def handler(event: Event) -> None:
            sink.append(event)

        return handler

    async def start(self) -> None:
        self.sched.start()
        await self.tools.start()
        await self.llm.start()
        await self.reasoner.start()
        await self.executor.start()
        self._task = self.bus.start_consumer()

    async def run_agent(self, user_text: str) -> None:
        plan = Plan(
            goal="Responder razonando",
            rule="agent",
            user_text=user_text,
            actions=[Action(kind=ActionKind.AGENT_STEP)],
        )
        await self.bus.publish(Event(type=PLAN_CREATED, source="test", payload=plan.model_dump()))
        await self.bus._queue.join()  # noqa: SLF001

    async def stop(self) -> None:
        await self.bus.stop()
        await asyncio.wait_for(self._task, timeout=2.0)
        await self.executor.stop()
        await self.reasoner.stop()
        await self.llm.stop()
        await self.tools.stop()
        await self.sched.stop()

    def response(self) -> ResponseReadyPayload:
        return ResponseReadyPayload.model_validate(self.responses[0].payload)


async def test_loop_uses_tool_then_answers() -> None:
    # Vuelta 1: pide datetime. Vuelta 2: con el resultado, responde.
    provider = _ScriptProvider(
        [AgentStep(tool_calls=[LLMToolCall(name="datetime")]), AgentStep(text="Son las tres.")]
    )
    h = _Harness(provider)
    await h.start()
    await h.run_agent("¿qué hora es exactamente?")
    await h.stop()

    assert len(h.completed) == 1
    assert len(h.failed) == 0
    resp = h.response()
    assert resp.text == "Son las tres."  # el texto final viene del razonador
    assert any(o.kind == "datetime" for o in resp.observations)  # la tool se ejecutó


async def test_loop_tolerates_tool_failure() -> None:
    # La tool falla en la vuelta 1; el bucle NO aborta: sigue y responde.
    provider = _ScriptProvider(
        [
            AgentStep(tool_calls=[LLMToolCall(name="rompe")]),
            AgentStep(text="lo intenté, sin suerte"),
        ]
    )
    h = _Harness(provider)
    await h.start()
    await h.run_agent("haz la cosa que falla")
    await h.stop()

    assert len(h.failed) == 0  # un fallo de tool no rompe el plan en modo agente
    assert len(h.completed) == 1
    resp = h.response()
    assert resp.text == "lo intenté, sin suerte"
    assert any(o.kind == "error" for o in resp.observations)  # el error quedó registrado


async def test_loop_respects_iteration_budget() -> None:
    # El modelo pide tools sin parar: al agotar el presupuesto (2), el bucle
    # corta y narra lo que tenga, sin colgarse ni fallar.
    provider = _ScriptProvider(
        [AgentStep(tool_calls=[LLMToolCall(name="datetime")])], narration="lo que averigüé"
    )
    h = _Harness(provider, max_agent_iters=2)
    await h.start()
    await h.run_agent("pregunta que provoca un bucle")
    await h.stop()

    assert len(h.completed) == 1
    assert len(h.failed) == 0
    resp = h.response()
    # Se ejecutó datetime exactamente max_agent_iters veces (no más).
    assert sum(1 for o in resp.observations if o.kind == "datetime") == 2
    # Al agotar vueltas, se narró con el LLM (CALL_LLM -> generate).
    assert resp.text == "lo que averigüé"
