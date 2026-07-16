"""Modelos de plan que produce el Planner y ejecuta el Action Executor.

Un ``Plan`` es una secuencia ordenada de ``Action``. El Planner solo lo produce
(decide); el Executor lo ejecuta (actúa). No contiene lógica: es un dato.

``Action`` generaliza el antiguo ``StepKind``: una acción puede ir a una tool,
al LLM, a la memoria, al scheduler o al estado cognitivo. Así "recuérdame X" o
"apréndete Y" son acciones, no herramientas.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ActionKind(StrEnum):
    """Tipo de acción de un plan. Cada uno tiene un destino distinto."""

    USE_TOOL = "use_tool"  # -> ToolManager
    CALL_LLM = "call_llm"  # -> LLMModule
    AGENT_STEP = "agent_step"  # -> AgentReasoner (bucle: decide tools o respuesta)
    REMEMBER = "remember"  # -> Memory (stub en v1.1)
    SCHEDULE = "schedule"  # -> Scheduler
    SET_STATE = "set_state"  # -> CognitiveState (stub en v1.1)
    RESPOND = "respond"  # -> canal de salida (response.ready)


class Action(BaseModel):
    """Una acción individual del plan."""

    kind: ActionKind
    target: str = ""  # p.ej. "datetime", "llm", "episodic", "scheduler"
    params: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    """Plan completo para un comando. Lo produce el Planner, lo ejecuta el Executor."""

    goal: str  # qué se quiere lograr (para logs, contexto del LLM y thoughts)
    rule: str  # regla que lo generó (trazabilidad)
    user_text: str = ""  # texto original del usuario (contexto para el LLM)
    actions: list[Action] = Field(default_factory=list)

    @property
    def action_kinds(self) -> list[str]:
        return [a.kind.value for a in self.actions]

    @property
    def requires_llm(self) -> bool:
        return any(a.kind is ActionKind.CALL_LLM for a in self.actions)
