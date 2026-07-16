"""AgentReasoner: resuelve un paso del bucle agente.

El Executor conduce el bucle (ejecuta tools, acumula observaciones); el
razonamiento —"¿ya tengo lo necesario para responder, o pido otra tool?"— vive
aquí. Escucha ``agent.step_requested`` con el texto del usuario y lo observado
hasta ahora, pregunta al proveedor (function calling) y emite
``agent.step_finished`` con las tools a invocar o con la respuesta final.

Es análogo al ``LLMModule`` (encapsula el proveedor tras el bus), pero para el
paso de decisión del agente en vez de la narración simple.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alice.brain.context import render_for_llm
from alice.brain.llm import LLMMessage, LLMRequest
from alice.core.events import AGENT_STEP_FINISHED, AGENT_STEP_REQUESTED, Event
from alice.core.payloads import (
    AgentStepFinishedPayload,
    AgentStepRequestedPayload,
    AgentToolCall,
)
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.brain.llm import LLMProvider, ToolSpec
    from alice.core.event_bus import EventBus, Subscription

_logger = get_logger("alice.brain.agent_reasoner")

_SYSTEM = (
    "Eres Alice, una asistente local con herramientas. Tienes tools para obtener "
    "datos que NO conoces por ti misma (la hora real, la memoria del usuario, lo "
    "que ve la cámara) y para actuar sobre el sistema.\n"
    "En cada paso: si necesitas un dato o una acción que solo una tool puede dar, "
    "invócala. Si con lo que ya sabes (incluido lo de OBSERVATIONS) puedes "
    "responder, responde directamente sin invocar más tools. No repitas una tool "
    "cuyo resultado ya tienes."
)

_STEP_INSTRUCTION = (
    "Si te falta algún dato, invoca la herramienta adecuada. Si ya tienes lo "
    "necesario en OBSERVATIONS, responde al usuario en su idioma, breve y natural, "
    "usando solo esos datos."
)


class AgentReasoner:
    """Módulo interno que resuelve pasos del bucle agente. Es un ``CoreModule``."""

    name = "agent_reasoner"

    def __init__(
        self,
        *,
        bus: EventBus,
        provider: LLMProvider,
        tools: list[ToolSpec],
        system_prompt: str = _SYSTEM,
        timeout_seconds: float = 25.0,
    ) -> None:
        self._bus = bus
        self._provider = provider
        self._tools = tools
        self._tool_names = {t.name for t in tools}
        self._system = system_prompt
        self._timeout = timeout_seconds
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = self._bus.subscribe(AGENT_STEP_REQUESTED, self._on_step)
        _logger.info("agent_reasoner.started", extra={"tools": sorted(self._tool_names)})

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)

    async def _on_step(self, event: Event) -> None:
        req = AgentStepRequestedPayload.model_validate(event.payload)
        content = render_for_llm(
            user_text=req.user_text,
            goal=req.goal,
            observations=req.observations,
            instruction=_STEP_INSTRUCTION,
        )
        request = LLMRequest(
            messages=[LLMMessage(role="user", content=content)], system=self._system
        )
        try:
            step = await asyncio.wait_for(
                self._provider.run_agent_step(request, self._tools), timeout=self._timeout
            )
            # Descarta tools alucinadas: solo las del catálogo real llegan al bucle.
            valid = [c for c in step.tool_calls if c.name in self._tool_names]
            payload = AgentStepFinishedPayload(
                tool_calls=[
                    AgentToolCall(name=c.name, arguments=dict(c.arguments)) for c in valid
                ],
                text=step.text,
                success=True,
            )
            _logger.info(
                "agent_reasoner.step",
                extra={
                    "correlation_id": str(event.correlation_id),
                    "tools": [c.name for c in valid],
                    "final": not valid,
                },
            )
        except Exception as exc:  # noqa: BLE001 - degradación controlada, no rompe el bucle
            payload = AgentStepFinishedPayload(
                success=False, error=str(exc) or type(exc).__name__
            )
            _logger.warning(
                "agent_reasoner.failed",
                extra={"correlation_id": str(event.correlation_id), "error": str(exc)},
            )
        await self._bus.publish(
            Event(
                type=AGENT_STEP_FINISHED,
                source="brain.agent_reasoner",
                correlation_id=event.correlation_id,
                payload=payload.model_dump(),
            )
        )
