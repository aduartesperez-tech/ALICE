"""Interfaz abstracta de LLM y módulo que la consume vía eventos.

El resto del sistema NUNCA ve tipos de un proveedor concreto (OpenAI, Ollama,
Claude...): solo ``LLMRequest`` / ``LLMResponse``, modelos propios de Alice.
v1 incluye únicamente ``NullLLMProvider`` (respuesta fija) para tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from alice.config.settings import DEFAULT_SYSTEM_PROMPT
from alice.core.events import LLM_FINISHED, LLM_REQUESTED, Event
from alice.core.payloads import LLMFinishedPayload, LLMRequestedPayload
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.brain.memory import ShortTermMemory
    from alice.core.event_bus import EventBus, Subscription

_logger = get_logger("alice.brain.llm")


class LLMMessage(BaseModel):
    """Un turno de conversación para el LLM."""

    role: str
    content: str


class LLMRequest(BaseModel):
    """Petición al proveedor de LLM (tipo propio de Alice).

    ``max_tokens``/``temperature`` a ``None`` significa "usa el default del
    proveedor" (el configurado en settings).
    """

    messages: list[LLMMessage]
    system: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None


class LLMUsage(BaseModel):
    """Contabilidad de tokens (opcional según proveedor)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMResponse(BaseModel):
    """Respuesta del proveedor de LLM (tipo propio de Alice)."""

    text: str
    model: str
    usage: LLMUsage = Field(default_factory=LLMUsage)


class LLMToolCall(BaseModel):
    """Una tool que el LLM decidió invocar (function calling)."""

    name: str
    arguments: dict[str, object] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    """Descripción de una tool para el catálogo de function calling."""

    name: str
    description: str
    parameters: dict[str, object]  # JSON Schema de los parámetros


class LLMProvider(ABC):
    """Contrato que cualquier backend de LLM debe implementar."""

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Genera una respuesta a partir de la petición."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Indica si el proveedor está disponible."""

    async def select_tools(
        self, request: LLMRequest, tools: list[ToolSpec]
    ) -> list[LLMToolCall]:
        """Dado el catálogo de tools, devuelve cuáles invocar (function calling).

        Por defecto (proveedores sin soporte) no elige ninguna: el sistema
        degrada a conversación normal.
        """
        return []

    async def aclose(self) -> None:
        """Libera recursos (conexiones HTTP...). Por defecto no hace nada."""
        return None


class NullLLMProvider(LLMProvider):
    """Proveedor de relleno: responde un texto fijo. No hay IA real en v1."""

    model_name = "null"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="[respuesta simulada: no hay proveedor de LLM configurado]",
            model=self.model_name,
        )

    async def health_check(self) -> bool:
        return True


class LLMModule:
    """Escucha ``llm.requested``, invoca el proveedor y emite ``llm.finished``.

    Es un ``CoreModule``. Añade al request el system prompt de Alice y el
    historial de conversación reciente (desde la short-term) como mensajes con
    rol, para que el modelo tenga contexto. Si el proveedor falla, degrada:
    emite ``llm.finished`` con ``success=False`` en vez de lanzar.
    """

    name = "llm"

    def __init__(
        self,
        *,
        bus: EventBus,
        provider: LLMProvider,
        short_term: ShortTermMemory | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        history_turns: int = 6,
    ) -> None:
        self._bus = bus
        self._provider = provider
        self._short_term = short_term
        self._system_prompt = system_prompt
        self._history_turns = history_turns
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = self._bus.subscribe(LLM_REQUESTED, self._on_request)
        healthy = await self._provider.health_check()
        _logger.info(
            "llm_module.started",
            extra={"provider": type(self._provider).__name__, "healthy": healthy},
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
        await self._provider.aclose()

    async def _on_request(self, event: Event) -> None:
        req = LLMRequestedPayload.model_validate(event.payload)
        request = LLMRequest(
            messages=[*self._history(), LLMMessage(role="user", content=req.prompt)],
            system=req.system or self._system_prompt,
        )
        correlation_id = str(event.correlation_id) if event.correlation_id else None
        try:
            response = await self._provider.generate(request)
            payload = LLMFinishedPayload(text=response.text, model=response.model, success=True)
            _logger.info(
                "llm.finished", extra={"correlation_id": correlation_id, "model": response.model}
            )
        except Exception as exc:  # noqa: BLE001 - degradación controlada, no tumba el módulo
            payload = LLMFinishedPayload(
                text="", model=type(self._provider).__name__, success=False, error=str(exc)
            )
            _logger.warning(
                "llm.provider_error", extra={"correlation_id": correlation_id, "error": str(exc)}
            )
        await self._bus.publish(
            Event(
                type=LLM_FINISHED,
                source="brain.llm",
                correlation_id=event.correlation_id,
                payload=payload.model_dump(),
            )
        )

    def _history(self) -> list[LLMMessage]:
        """Últimos turnos como mensajes con rol, excluyendo el turno actual."""
        if self._short_term is None or self._history_turns <= 0:
            return []
        items = self._short_term.recent(self._history_turns + 1)
        # El último item es el user_turn actual (ya grabado): lo excluimos.
        if items and items[-1].kind == "user_turn":
            items = items[:-1]
        items = items[-self._history_turns :]
        role = {"user_turn": "user", "alice_turn": "assistant"}
        return [
            LLMMessage(role=role.get(it.kind, "user"), content=str(it.content.get("text", "")))
            for it in items
        ]
