"""Confirmación humana: la barandilla de todo lo que muta el sistema.

Antes de ejecutar algo peligroso (un script, un comando, código), el ToolManager
pregunta aquí. El manager publica la pregunta como respuesta al usuario (para que
la oiga por voz o la lea en consola), espera su "sí"/"no" y resuelve.

Reglas duras:
- **Fail-closed**: si nadie contesta a tiempo, se DENIEGA. El silencio nunca
  autoriza.
- Mientras hay una confirmación pendiente, el Planner no planifica el "sí"/"no"
  como si fuera un comando nuevo (ver ``is_pending``): esa respuesta es para
  Alice, no una petición.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from alice.core.events import (
    COMMAND_RECEIVED,
    CONFIRMATION_REQUESTED,
    CONFIRMATION_RESOLVED,
    RESPONSE_READY,
    Event,
)
from alice.core.observation import Observation
from alice.core.payloads import (
    CommandReceivedPayload,
    ConfirmationRequestedPayload,
    ConfirmationResolvedPayload,
    ResponseReadyPayload,
)
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.core.event_bus import EventBus, Subscription

_logger = get_logger("alice.brain.confirm")

# Respuestas afirmativas/negativas típicas por voz o texto.
_YES = re.compile(
    r"^\s*(s[ií]|dale|ok|okey|okay|vale|adelante|hazlo|confirmo|correcto|afirmativo|claro)\b",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\s*(no|nop|cancela|cancelar|para|detente|negativo|olv[ií]dalo|mejor no)\b",
    re.IGNORECASE,
)


class ConfirmationManager:
    """Pide confirmación al usuario y espera su respuesta. Es un ``CoreModule``."""

    name = "confirmation"

    def __init__(self, *, bus: EventBus, timeout_seconds: float = 45.0) -> None:
        self._bus = bus
        self._timeout = timeout_seconds
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._subs: list[Subscription] = []

    async def start(self) -> None:
        self._subs = [
            self._bus.subscribe(COMMAND_RECEIVED, self._on_command),
            self._bus.subscribe(CONFIRMATION_RESOLVED, self._on_resolved),
        ]
        _logger.info("confirmation.started", extra={"timeout": self._timeout})

    async def stop(self) -> None:
        for sub in self._subs:
            self._bus.unsubscribe(sub)
        self._subs.clear()
        # Nadie se queda esperando para siempre: lo pendiente se deniega.
        for future in self._pending.values():
            if not future.done():
                future.set_result(False)
        self._pending.clear()

    def is_pending(self) -> bool:
        """Si hay una confirmación esperando respuesta (lo consulta el Planner)."""
        return any(not f.done() for f in self._pending.values())

    async def confirm(self, question: str, *, detail: str = "") -> bool:
        """Pregunta al usuario y espera. Devuelve False si no contesta a tiempo."""
        request_id = str(uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[request_id] = future

        await self._bus.publish(
            Event(
                type=CONFIRMATION_REQUESTED,
                source="brain.confirm",
                payload=ConfirmationRequestedPayload(
                    request_id=request_id, question=question, detail=detail
                ).model_dump(),
            )
        )
        # La pregunta va al canal de salida para que el usuario la oiga/lea.
        await self._bus.publish(
            Event(
                type=RESPONSE_READY,
                source="brain.confirm",
                payload=ResponseReadyPayload(
                    text=question,
                    observations=[
                        Observation(
                            source="confirmation",
                            kind="confirmation_request",
                            data={"question": question, "detail": detail},
                        )
                    ],
                    goal="Pedir confirmación al usuario",
                ).model_dump(),
            )
        )
        try:
            granted = await asyncio.wait_for(future, timeout=self._timeout)
            reason = "usuario"
        except TimeoutError:
            granted, reason = False, "timeout"
        finally:
            self._pending.pop(request_id, None)
        _logger.info(
            "confirmation.resolved",
            extra={"request_id": request_id, "granted": granted, "reason": reason},
        )
        return granted

    async def _on_command(self, event: Event) -> None:
        """Interpreta el 'sí'/'no' del usuario cuando hay algo pendiente."""
        future = self._oldest_pending()
        if future is None:
            return
        command = CommandReceivedPayload.model_validate(event.payload)
        if command.internal:
            return  # una reacción sintética no confirma nada
        text = command.text
        if _YES.match(text):
            future.set_result(True)
        elif _NO.match(text):
            future.set_result(False)
        # Cualquier otra cosa: se ignora y la confirmación sigue esperando.

    async def _on_resolved(self, event: Event) -> None:
        """Permite resolver desde otro canal (p.ej. un botón en la web)."""
        payload = ConfirmationResolvedPayload.model_validate(event.payload)
        future = self._pending.get(payload.request_id)
        if future is not None and not future.done():
            future.set_result(payload.granted)

    def _oldest_pending(self) -> asyncio.Future[bool] | None:
        for future in self._pending.values():
            if not future.done():
                return future
        return None
