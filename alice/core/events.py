"""Modelo de eventos y catálogo de tipos.

Un ``Event`` es inmutable y serializable a JSON. Todos los módulos se comunican
EXCLUSIVAMENTE mediante eventos publicados en el EventBus; ningún módulo importa
otro módulo de negocio.

Cada tipo de evento define además un modelo Pydantic de payload para que los
consumidores validen en lugar de leer ``dict`` a ciegas (ver ``payloads.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventPriority(IntEnum):
    """Prioridad del evento. Menor número = mayor prioridad en la cola."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


class Event(BaseModel):
    """Evento inmutable que circula por el bus."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: str
    source: str
    priority: EventPriority = EventPriority.NORMAL
    correlation_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# --- Catálogo de tipos de evento (nomenclatura dominio.acción) ---------------

WILDCARD = "*"

# Sistema
SYSTEM_STARTED = "system.started"
SYSTEM_STOPPED = "system.stopped"

# Percepción (voz/visión)
SPEECH_DETECTED = "speech.detected"
PERSON_DETECTED = "person.detected"  # cambia la presencia (aparece/desaparece)
FACE_RECOGNIZED = "face.recognized"  # identidad reconocida (quién)
HAND_DETECTED = "hand.detected"  # gesto de mano detectado

# Comandos y conversación
COMMAND_RECEIVED = "command.received"
CONVERSATION_STARTED = "conversation.started"
CONVERSATION_ENDED = "conversation.ended"

# Planificación y ejecución
PLAN_CREATED = "plan.created"
PLAN_COMPLETED = "plan.completed"
PLAN_FAILED = "plan.failed"

# Salida hacia el usuario (la consume el canal de salida: consola, voz futura...)
RESPONSE_READY = "response.ready"

# Internet
INTERNET_SEARCH_REQUESTED = "internet.search_requested"
INTERNET_SEARCH_COMPLETED = "internet.search_completed"

# Herramientas
TOOL_REQUESTED = "tool.requested"
TOOL_FINISHED = "tool.finished"

# LLM
LLM_REQUESTED = "llm.requested"
LLM_FINISHED = "llm.finished"

# Bucle agente: un "paso" de razonamiento donde el LLM decide, dado lo observado
# hasta ahora, si invocar más tools o dar la respuesta final. Lo pide el Executor
# y lo resuelve el AgentReasoner; así el bucle percibir→razonar→actuar itera.
AGENT_STEP_REQUESTED = "agent.step_requested"
AGENT_STEP_FINISHED = "agent.step_finished"

# Memoria
MEMORY_STORE_REQUESTED = "memory.store_requested"
MEMORY_STORED = "memory.stored"

# Confirmación humana: barandilla para todo lo que muta el sistema (ejecutar
# scripts, comandos, código). Alice pregunta y ESPERA; sin respuesta = no.
CONFIRMATION_REQUESTED = "confirmation.requested"
CONFIRMATION_RESOLVED = "confirmation.resolved"
