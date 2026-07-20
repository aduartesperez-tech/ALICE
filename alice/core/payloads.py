"""Modelos Pydantic de payload para cada tipo de evento con datos.

Permiten a los consumidores validar el contenido del evento en lugar de leer
``dict`` a ciegas: ``CommandReceivedPayload.model_validate(event.payload)``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from alice.core.observation import Observation


class CommandReceivedPayload(BaseModel):
    """Un comando en lenguaje natural recibido por el sistema."""

    text: str
    user: str | None = None
    # Comando sintético generado por el propio sistema (p.ej. una reacción de
    # visión), no tecleado/hablado por el usuario. Salta el enrutado caro
    # (selección de tools / clasificación): una reacción nunca necesita tools.
    internal: bool = False


class ToolRequestedPayload(BaseModel):
    """Solicitud de ejecución de una herramienta."""

    tool_name: str
    params: dict[str, Any] = {}


class ToolFinishedPayload(BaseModel):
    """Resultado de la ejecución de una herramienta."""

    tool_name: str
    success: bool
    output: dict[str, Any] | None = None
    error: str | None = None


class LLMRequestedPayload(BaseModel):
    """Solicitud de generación al módulo LLM."""

    prompt: str
    system: str | None = None


class LLMFinishedPayload(BaseModel):
    """Respuesta del módulo LLM.

    ``success=False`` indica degradación (proveedor caído/timeout): el Executor
    responde igualmente con las observaciones en crudo, sin narración.
    """

    text: str
    model: str
    success: bool = True
    error: str | None = None


class AgentToolCall(BaseModel):
    """Una tool que el LLM pidió invocar en un paso del bucle agente."""

    name: str
    arguments: dict[str, Any] = {}


class AgentStepRequestedPayload(BaseModel):
    """Petición de un paso de razonamiento del bucle agente.

    Lleva el texto del usuario y todo lo observado hasta ahora (resultados de
    tools de vueltas previas). El AgentReasoner decide: más tools o respuesta.
    """

    user_text: str
    goal: str = ""
    observations: list[Observation] = []


class AgentStepFinishedPayload(BaseModel):
    """Resultado de un paso del bucle agente.

    Si ``tool_calls`` no está vacío, el Executor las ejecuta y vuelve a pedir
    otro paso. Si está vacío, ``text`` es la respuesta final (o cadena vacía si
    el proveedor degradó: el Executor narrará las observaciones en crudo).
    """

    tool_calls: list[AgentToolCall] = []
    text: str = ""
    success: bool = True
    error: str | None = None


class MemoryStoreRequestedPayload(BaseModel):
    """Petición de escritura en memoria (fire-and-forget)."""

    store: str  # "episodic" | "long_term"
    kind: str = "event"  # para episodic: user_turn | alice_turn | event | thought | user_fact
    content: dict[str, Any] = {}  # para episodic
    key: str | None = None  # para long_term
    value: Any = None  # para long_term


class ConfirmationRequestedPayload(BaseModel):
    """Alice pide permiso al usuario antes de una acción que muta el sistema."""

    request_id: str
    question: str  # lo que se le dice al usuario ("¿Ejecuto el script 'backup'?")
    detail: str = ""  # qué hará exactamente (para logs y para el canal de salida)


class ConfirmationResolvedPayload(BaseModel):
    """Respuesta a una petición de confirmación. Sin respuesta a tiempo = denegada."""

    request_id: str
    granted: bool
    reason: str = ""  # "usuario" | "timeout" | "sin_canal"


class MemoryStoredPayload(BaseModel):
    """Confirmación de escritura (para logs/tests; nadie la espera para avanzar)."""

    store: str
    ok: bool


class PlanCompletedPayload(BaseModel):
    """Señal de que el Executor terminó todos los pasos de un plan."""

    goal: str
    rule: str


class PlanFailedPayload(BaseModel):
    """Señal de que un plan abortó por el fallo de un paso."""

    goal: str
    rule: str
    failed_action: str
    error: str


class ResponseReadyPayload(BaseModel):
    """Respuesta para el usuario. La consume el canal de salida (consola, voz...).

    ``text`` es la prosa narrada por el LLM; en v1.1 (sin LLM real) va ``None`` y
    el canal renderiza ``observations`` en crudo. ``observations`` SIEMPRE lleva
    los datos estructurados: son la fuente de verdad, el texto es su presentación.
    """

    text: str | None = None
    observations: list[Observation] = []
    goal: str = ""


class SpeechDetectedPayload(BaseModel):
    """Texto transcrito por un módulo de voz futuro."""

    text: str
    confidence: float | None = None


class PersonDetectedPayload(BaseModel):
    """Cambio de presencia frente a la cámara (aparece/desaparece)."""

    present: bool  # True = hay al menos una persona; False = no hay nadie
    count: int = 0  # número de caras/personas visibles


class FaceRecognizedPayload(BaseModel):
    """Identidad reconocida por el módulo de visión."""

    name: str | None = None  # None si es un rostro desconocido
    known: bool = False
    confidence: float | None = None


class GestureDetectedPayload(BaseModel):
    """Gesto de mano reconocido (saludo, pulgar arriba, palma...)."""

    gesture: str  # "wave" | "thumbs_up" | "open_palm" | "victory" | ...
    hand: str | None = None  # "left" | "right"


class ConversationPayload(BaseModel):
    """Marca de inicio/fin de conversación."""

    conversation_id: UUID
    user: str | None = None
