"""MemoryModule: pegamento entre el bus y los stores de memoria.

- Graba automáticamente cada turno: ``command.received`` -> episodio
  ``user_turn``; ``response.ready`` -> episodio ``alice_turn``. Los mismos
  turnos alimentan la short-term (RAM), que da el contexto de conversación.
- Atiende ``memory.store_requested`` (que emite la acción ``remember``) y
  escribe en el store indicado, confirmando con ``memory.stored``.

Las escrituras son fire-and-forget: nadie espera ``memory.stored`` para avanzar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alice.brain.memory import MemoryItem
from alice.core.events import (
    COMMAND_RECEIVED,
    MEMORY_STORE_REQUESTED,
    MEMORY_STORED,
    RESPONSE_READY,
    Event,
)
from alice.core.observation import render_observations_plain
from alice.core.payloads import (
    CommandReceivedPayload,
    MemoryStoredPayload,
    MemoryStoreRequestedPayload,
    ResponseReadyPayload,
)
from alice.logging import get_logger

if TYPE_CHECKING:
    import sqlite3

    from alice.brain.memory import EpisodicMemory, LongTermMemory, ShortTermMemory
    from alice.core.event_bus import EventBus, Subscription

_logger = get_logger("alice.brain.memory_module")


class MemoryModule:
    """Módulo interno que persiste turnos y atiende peticiones de escritura."""

    name = "memory"

    def __init__(
        self,
        *,
        bus: EventBus,
        episodic: EpisodicMemory,
        long_term: LongTermMemory,
        short_term: ShortTermMemory,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._bus = bus
        self._episodic = episodic
        self._long_term = long_term
        self._short_term = short_term
        self._connection = connection  # se cierra en stop() si el módulo lo posee
        self._subs: list[Subscription] = []

    async def start(self) -> None:
        self._subs = [
            self._bus.subscribe(COMMAND_RECEIVED, self._on_command),
            self._bus.subscribe(RESPONSE_READY, self._on_response),
            self._bus.subscribe(MEMORY_STORE_REQUESTED, self._on_store_requested),
        ]
        _logger.info("memory_module.started")

    async def stop(self) -> None:
        for sub in self._subs:
            self._bus.unsubscribe(sub)
        self._subs.clear()
        if self._connection is not None:
            self._connection.close()

    # --- Grabación automática de turnos --------------------------------------

    async def _on_command(self, event: Event) -> None:
        payload = CommandReceivedPayload.model_validate(event.payload)
        self._record(
            MemoryItem(
                kind="user_turn",
                content={"text": payload.text, "user": payload.user},
                correlation_id=str(event.correlation_id) if event.correlation_id else None,
            )
        )

    async def _on_response(self, event: Event) -> None:
        payload = ResponseReadyPayload.model_validate(event.payload)
        text = payload.text or render_observations_plain(payload.observations)
        self._record(
            MemoryItem(
                kind="alice_turn",
                content={"text": text},
                correlation_id=str(event.correlation_id) if event.correlation_id else None,
            )
        )

    def _record(self, item: MemoryItem) -> None:
        self._episodic.add_episode(item)
        self._short_term.add(item)

    # --- Escrituras explícitas (acción remember) -----------------------------

    async def _on_store_requested(self, event: Event) -> None:
        req = MemoryStoreRequestedPayload.model_validate(event.payload)
        ok = self._dispatch_store(req)
        await self._bus.publish(
            Event(
                type=MEMORY_STORED,
                source="brain.memory",
                correlation_id=event.correlation_id,
                payload=MemoryStoredPayload(store=req.store, ok=ok).model_dump(),
            )
        )

    def _dispatch_store(self, req: MemoryStoreRequestedPayload) -> bool:
        if req.store == "long_term":
            if req.key is None:
                _logger.warning("memory.store_missing_key", extra={"store": req.store})
                return False
            self._long_term.set(req.key, req.value)
            return True
        if req.store == "episodic":
            self._episodic.add_episode(MemoryItem(kind=req.kind, content=req.content))
            return True
        _logger.warning("memory.store_unknown", extra={"store": req.store})
        return False
