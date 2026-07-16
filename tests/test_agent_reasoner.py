"""Tests del AgentReasoner: resuelve un paso del bucle agente vía eventos."""

from __future__ import annotations

import asyncio

from alice.brain.agent_reasoner import AgentReasoner
from alice.brain.llm import (
    AgentStep,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    ToolSpec,
)
from alice.core.event_bus import EventBus
from alice.core.events import AGENT_STEP_FINISHED, AGENT_STEP_REQUESTED, Event
from alice.core.payloads import AgentStepFinishedPayload, AgentStepRequestedPayload

_CATALOG = [
    ToolSpec(name="datetime", description="hora", parameters={"type": "object"}),
    ToolSpec(name="camera", description="qué veo", parameters={"type": "object"}),
]


class FakeAgentProvider(LLMProvider):
    """Devuelve el AgentStep configurado (o lanza); registra las peticiones."""

    def __init__(self, step: AgentStep | Exception) -> None:
        self._step = step
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="narración", model="fake")

    async def health_check(self) -> bool:
        return True

    async def run_agent_step(self, request: LLMRequest, tools: list[ToolSpec]) -> AgentStep:
        self.requests.append(request)
        if isinstance(self._step, Exception):
            raise self._step
        return self._step


async def _run_step(
    provider: LLMProvider, payload: AgentStepRequestedPayload
) -> AgentStepFinishedPayload:
    bus = EventBus(max_queue_size=50)
    reasoner = AgentReasoner(bus=bus, provider=provider, tools=_CATALOG)
    await reasoner.start()
    out: list[Event] = []
    bus.subscribe(AGENT_STEP_FINISHED, lambda e: _collect(out, e))
    task = bus.start_consumer()
    await bus.publish(
        Event(type=AGENT_STEP_REQUESTED, source="test", payload=payload.model_dump())
    )
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await reasoner.stop()
    assert len(out) == 1
    return AgentStepFinishedPayload.model_validate(out[0].payload)


async def _collect(sink: list[Event], event: Event) -> None:
    sink.append(event)


async def test_reasoner_returns_tool_calls() -> None:
    provider = FakeAgentProvider(AgentStep(tool_calls=[LLMToolCall(name="datetime")]))
    result = await _run_step(provider, AgentStepRequestedPayload(user_text="ubícame en el tiempo"))
    assert [c.name for c in result.tool_calls] == ["datetime"]
    assert result.success


async def test_reasoner_returns_final_text() -> None:
    provider = FakeAgentProvider(AgentStep(text="Son las tres."))
    result = await _run_step(provider, AgentStepRequestedPayload(user_text="hola"))
    assert result.tool_calls == []
    assert result.text == "Son las tres."


async def test_reasoner_filters_hallucinated_tools() -> None:
    # El modelo pide una tool que no existe: se descarta, solo pasa la real.
    provider = FakeAgentProvider(
        AgentStep(tool_calls=[LLMToolCall(name="hackear_nasa"), LLMToolCall(name="camera")])
    )
    result = await _run_step(provider, AgentStepRequestedPayload(user_text="haz algo raro"))
    assert [c.name for c in result.tool_calls] == ["camera"]


async def test_reasoner_degrades_on_provider_error() -> None:
    provider = FakeAgentProvider(RuntimeError("sin function calling"))
    result = await _run_step(provider, AgentStepRequestedPayload(user_text="hola"))
    assert result.success is False
    assert result.tool_calls == []


async def test_reasoner_passes_observations_in_prompt() -> None:
    # Las observaciones de vueltas previas llegan al prompt del razonador.
    from alice.core.observation import Observation

    provider = FakeAgentProvider(AgentStep(text="ok"))
    payload = AgentStepRequestedPayload(
        user_text="y ahora qué",
        observations=[Observation(source="tool:datetime", kind="datetime", data={"iso": "2026"})],
    )
    await _run_step(provider, payload)
    assert "datetime" in provider.requests[0].messages[0].content
