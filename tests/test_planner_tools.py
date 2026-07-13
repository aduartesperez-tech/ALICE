"""Tests de ToolCallingStrategy: selección de tools por function calling."""

from __future__ import annotations

from alice.brain.llm import LLMProvider, LLMRequest, LLMResponse, LLMToolCall, ToolSpec
from alice.brain.planner_tools import ToolCallingStrategy
from alice.core.payloads import CommandReceivedPayload

_CATALOG = [
    ToolSpec(name="datetime", description="hora y fecha", parameters={"type": "object"}),
    ToolSpec(name="recall_memory", description="lo que sé de ti", parameters={"type": "object"}),
    ToolSpec(name="camera", description="qué veo", parameters={"type": "object"}),
]


class FakeProvider(LLMProvider):
    """Devuelve las tool calls que se le configuren; registra las peticiones."""

    def __init__(self, calls: list[LLMToolCall] | Exception) -> None:
        self._calls = calls
        self.select_requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="narración", model="fake")

    async def health_check(self) -> bool:
        return True

    async def select_tools(self, request: LLMRequest, tools: list[ToolSpec]) -> list[LLMToolCall]:
        self.select_requests.append(request)
        if isinstance(self._calls, Exception):
            raise self._calls
        return self._calls


def _cmd(text: str) -> CommandReceivedPayload:
    return CommandReceivedPayload(text=text)


def _strategy(calls: list[LLMToolCall] | Exception) -> ToolCallingStrategy:
    return ToolCallingStrategy(provider=FakeProvider(calls), tools=_CATALOG)


async def test_internal_command_skips_tool_selection() -> None:
    # Una reacción de visión (internal=True) va directa a chat, sin consultar al LLM.
    provider = FakeProvider([LLMToolCall(name="datetime")])
    strategy = ToolCallingStrategy(provider=provider, tools=_CATALOG)
    cmd = CommandReceivedPayload(text="(Acabas de ver a Adrián. Salúdalo.)", internal=True)
    plan = await strategy.plan(cmd)
    assert plan.rule == "reaction"
    assert plan.requires_llm  # narra el saludo
    assert provider.select_requests == []  # no se pagó la selección de tools


async def test_regex_rule_skips_tool_selection() -> None:
    provider = FakeProvider([LLMToolCall(name="datetime")])
    strategy = ToolCallingStrategy(provider=provider, tools=_CATALOG)
    plan = await strategy.plan(_cmd("recuérdame estirar en 10 segundos"))
    assert plan.rule == "reminder"
    assert provider.select_requests == []  # no se consultó al LLM


# Nota: las frases evitan las palabras que dispararían el regex (hora, fecha,
# etc.); así el comando cae en fallback y se ejercita la selección por LLM.
async def test_single_tool_selected() -> None:
    strategy = _strategy([LLMToolCall(name="datetime")])
    plan = await strategy.plan(_cmd("necesito ubicarme en el tiempo"))
    assert plan.rule == "tool_call:datetime"
    assert plan.action_kinds == ["use_tool", "call_llm", "respond"]
    assert plan.actions[0].target == "datetime"


async def test_multiple_tools_in_one_turn() -> None:
    strategy = _strategy([LLMToolCall(name="datetime"), LLMToolCall(name="recall_memory")])
    plan = await strategy.plan(_cmd("hazme un resumen de mi situación"))
    assert plan.rule == "tool_call:datetime,recall_memory"
    targets = [a.target for a in plan.actions if a.kind.value == "use_tool"]
    assert targets == ["datetime", "recall_memory"]


async def test_tool_arguments_passed_through() -> None:
    strategy = _strategy([LLMToolCall(name="recall_memory", arguments={"limit": 5})])
    plan = await strategy.plan(_cmd("recuérdame mis últimas cosas"))
    assert plan.actions[0].params == {"limit": 5}


async def test_no_tools_selected_goes_to_chat() -> None:
    strategy = _strategy([])
    plan = await strategy.plan(_cmd("cuéntame un chiste"))
    assert plan.rule == "tool_call:none"
    assert plan.requires_llm


async def test_unknown_tool_filtered_to_chat() -> None:
    strategy = _strategy([LLMToolCall(name="hackear_nasa")])
    plan = await strategy.plan(_cmd("haz algo raro"))
    assert plan.rule == "tool_call:none"


async def test_provider_error_falls_back_to_chat() -> None:
    strategy = _strategy(RuntimeError("modelo sin function calling"))
    plan = await strategy.plan(_cmd("hazme un resumen general"))
    assert plan.rule == "fallback_llm"
    assert plan.requires_llm


async def test_empty_catalog_skips_selection() -> None:
    provider = FakeProvider([LLMToolCall(name="datetime")])
    strategy = ToolCallingStrategy(provider=provider, tools=[])
    plan = await strategy.plan(_cmd("hazme un resumen general"))
    assert plan.rule == "fallback_llm"
    assert provider.select_requests == []
