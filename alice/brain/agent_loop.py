"""Estrategia de planificación con bucle agente (multi-vuelta).

A diferencia de ``ToolCallingStrategy`` (selección de una sola vuelta), aquí el
plan no fija las tools de antemano: produce un único paso ``AGENT_STEP`` y deja
que el bucle del Executor + AgentReasoner itere (percibir→razonar→actuar→repetir)
hasta que el modelo tenga con qué responder. Añadir una tool la hace disponible
sin tocar nada: el razonador ve el catálogo real.

Las reglas regex siguen primero (gratis, deterministas) para lo trivial
(reminder/remember/vision/recall/datetime); solo si nada aplica entra el bucle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alice.brain.plan import Action, ActionKind, Plan
from alice.brain.planner import RuleBasedStrategy, build_chat_plan
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.core.payloads import CommandReceivedPayload

_logger = get_logger("alice.brain.agent_loop")


class AgentLoopStrategy:
    """Estrategia de planificación que delega en el bucle agente. Cumple ``PlanningStrategy``."""

    def __init__(self, *, narrate: bool = True) -> None:
        # Las reglas resuelven casos triviales con extracción estructurada (gratis).
        self._rules = RuleBasedStrategy(narrate=narrate)

    def plan(self, command: CommandReceivedPayload) -> Plan:
        if command.internal:
            # Reacción sintética (visión, etc.): no necesita tools, va directa a chat.
            return build_chat_plan(command.text, rule="reaction")
        base = self._rules.plan(command)
        if base.rule != "fallback_llm":
            # Una regla explícita ya resolvió el comando (p.ej. reminder): sin bucle.
            return base
        # Nada trivial aplicó: entra el bucle agente, que decide tools por su cuenta.
        _logger.info("agent_loop.enter", extra={"command": command.text})
        return Plan(
            goal="Responder al usuario razonando y usando herramientas si hace falta",
            rule="agent",
            user_text=command.text,
            actions=[Action(kind=ActionKind.AGENT_STEP)],
        )
