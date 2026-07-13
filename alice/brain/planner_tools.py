"""Estrategia de planificación por function calling (agente).

A diferencia de ``LLMIntentStrategy`` (catálogo cerrado de 5 intenciones), aquí
el LLM ve el catálogo REAL de tools registradas y decide él cuáles invocar. Al
añadir una tool nueva, queda disponible automáticamente: no hay que tocar el
planner ni el prompt.

Flujo: reglas regex primero (resuelven ``reminder``/``remember`` con extracción
estructurada, gratis); si ninguna aplica, se pide al modelo que seleccione tools
(function calling). Las tools elegidas forman un plan ``use_tool... + call_llm +
respond``: el executor las ejecuta y el LLM narra el resultado, como siempre.

Selección de una sola vuelta (no multi-turno): suficiente para encadenar varias
tools en un turno; el bucle agente iterativo queda para una fase posterior.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alice.brain.llm import LLMMessage, LLMRequest
from alice.brain.plan import Action, ActionKind, Plan
from alice.brain.planner import RuleBasedStrategy, build_chat_plan
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.brain.llm import LLMProvider, LLMToolCall, ToolSpec
    from alice.core.payloads import CommandReceivedPayload

_logger = get_logger("alice.brain.planner_tools")

_SELECT_SYSTEM = (
    "Eres Alice, una asistente local. Tienes herramientas para obtener datos que NO "
    "conoces por ti misma (la hora real, la memoria del usuario, lo que ve la cámara).\n"
    "Usa una herramienta SOLO si el mensaje pide justo ese dato. Para charla, "
    "opiniones, saludos, chistes, preguntas de conocimiento general o cualquier cosa "
    "que puedas responder tú misma, NO uses ninguna herramienta.\n"
    "Ante la duda, no uses herramientas."
)


class ToolCallingStrategy:
    """Estrategia de planificación con function calling. Cumple ``PlanningStrategy``."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        tools: list[ToolSpec],
        narrate: bool = True,
        timeout_seconds: float = 25.0,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._tool_names = {t.name for t in tools}
        self._rules = RuleBasedStrategy(narrate=narrate)
        self._timeout = timeout_seconds

    async def plan(self, command: CommandReceivedPayload) -> Plan:
        if command.internal:
            # Reacción sintética (visión, etc.): no necesita tools, va directa a chat.
            return build_chat_plan(command.text, rule="reaction")
        base = self._rules.plan(command)
        if base.rule != "fallback_llm":
            # Una regla explícita ya resolvió el comando (p.ej. reminder): sin LLM.
            return base
        if not self._tools:
            return base

        try:
            calls = await asyncio.wait_for(self._select(command.text), timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - la selección nunca rompe el flujo
            _logger.warning(
                "planner_tools.select_failed",
                extra={
                    "command": command.text,
                    "error": str(exc) or type(exc).__name__,  # TimeoutError tiene str vacío
                },
            )
            return base

        valid = [c for c in calls if c.name in self._tool_names]
        if not valid:
            return build_chat_plan(command.text, rule="tool_call:none")

        names = [c.name for c in valid]
        _logger.info("planner_tools.selected", extra={"command": command.text, "tools": names})
        actions = [
            Action(kind=ActionKind.USE_TOOL, target=c.name, params=dict(c.arguments))
            for c in valid
        ]
        # Tras las tools, el LLM narra el resultado en lenguaje natural.
        actions.append(Action(kind=ActionKind.CALL_LLM, target="llm"))
        actions.append(Action(kind=ActionKind.RESPOND))
        return Plan(
            goal="Responder al usuario usando herramientas",
            rule=f"tool_call:{','.join(names)}",
            user_text=command.text,
            actions=actions,
        )

    async def _select(self, text: str) -> list[LLMToolCall]:
        request = LLMRequest(
            messages=[LLMMessage(role="user", content=text)],
            system=_SELECT_SYSTEM,
        )
        return await self._provider.select_tools(request, self._tools)
