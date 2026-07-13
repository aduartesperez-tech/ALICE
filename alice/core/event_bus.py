"""EventBus asíncrono con cola de prioridad acotada.

- ``publish`` encola sin bloquear jamás al publicador (política de backpressure).
- ``subscribe`` registra un handler para un tipo de evento (o ``"*"`` comodín).
- ``run`` consume la cola y despacha a los handlers suscritos.
- Un handler que lanza excepción NO tumba el bus: se captura y se loguea.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alice.core.events import WILDCARD, Event
from alice.logging import get_logger

if TYPE_CHECKING:
    from uuid import UUID

EventHandler = Callable[[Event], Awaitable[None]]

_logger = get_logger("alice.core.event_bus")


@dataclass(frozen=True, eq=False)
class Subscription:
    """Handle opaco de una suscripción; se usa para desuscribir."""

    id: int
    event_type: str
    handler: EventHandler = field(compare=False)


@dataclass(order=True)
class _QueueItem:
    """Envoltorio ordenable: (prioridad, secuencia) → FIFO dentro de igual prioridad."""

    priority: int
    sequence: int
    event: Event = field(compare=False)


class EventBus:
    """Bus de eventos asíncrono con prioridad y backpressure."""

    def __init__(self, max_queue_size: int = 1000) -> None:
        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue(
            maxsize=max_queue_size
        )
        self._subscribers: dict[str, list[Subscription]] = {}
        self._counter = itertools.count()
        self._sub_ids = itertools.count()
        self._running = False
        self._consumer: asyncio.Task[None] | None = None

    # --- Suscripción ---------------------------------------------------------

    def subscribe(self, event_type: str, handler: EventHandler) -> Subscription:
        """Registra un handler para ``event_type`` (o ``"*"`` para todos los eventos)."""
        sub = Subscription(id=next(self._sub_ids), event_type=event_type, handler=handler)
        self._subscribers.setdefault(event_type, []).append(sub)
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        """Elimina una suscripción por su handle."""
        subs = self._subscribers.get(subscription.event_type)
        if not subs:
            return
        self._subscribers[subscription.event_type] = [s for s in subs if s.id != subscription.id]

    # --- Publicación ---------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Encola un evento. Nunca bloquea: si la cola está llena aplica backpressure."""
        item = _QueueItem(priority=int(event.priority), sequence=next(self._counter), event=event)
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._handle_full_queue(event)
            return
        _logger.debug(
            "event.published",
            extra=self._event_fields(event.id, event),
        )

    def _handle_full_queue(self, event: Event) -> None:
        """Política de cola llena: descartar eventos LOW, avisar de los demás."""
        _logger.warning(
            "event.dropped_queue_full",
            extra=self._event_fields(event.id, event),
        )

    # --- Consumo -------------------------------------------------------------

    async def run(self) -> None:
        """Bucle consumidor: extrae de la cola y despacha hasta ``stop()``."""
        self._running = True
        _logger.info("event_bus.started")
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                await self._dispatch(item.event)
            finally:
                self._queue.task_done()
        _logger.info("event_bus.stopped")

    async def stop(self) -> None:
        """Detiene el consumidor tras drenar los eventos pendientes."""
        await self._queue.join()
        self._running = False

    async def _dispatch(self, event: Event) -> None:
        """Entrega el evento a los handlers suscritos a su tipo y a los comodín."""
        subs = [*self._subscribers.get(event.type, []), *self._subscribers.get(WILDCARD, [])]
        for sub in subs:
            try:
                await sub.handler(event)
            except Exception:  # noqa: BLE001 - un handler no debe tumbar el bus
                _logger.exception(
                    "event_bus.handler_error",
                    extra=self._event_fields(event.id, event) | {"subscription_id": sub.id},
                )

    @staticmethod
    def _event_fields(event_id: UUID, event: Event) -> dict[str, object]:
        return {
            "event_id": str(event_id),
            "event_type": event.type,
            "source": event.source,
            "priority": int(event.priority),
            "correlation_id": str(event.correlation_id) if event.correlation_id else None,
        }

    # --- Utilidad para el Orchestrator --------------------------------------

    def start_consumer(self) -> asyncio.Task[None]:
        """Arranca el bucle ``run`` como tarea de fondo y devuelve el handle."""
        self._consumer = asyncio.create_task(self.run(), name="event_bus_consumer")
        return self._consumer
