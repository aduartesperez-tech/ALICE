"""Tests de la confirmación humana: la barandilla de lo que muta el sistema."""

from __future__ import annotations

import asyncio

from alice.brain.confirm import ConfirmationManager
from alice.core.event_bus import EventBus
from alice.core.events import (
    COMMAND_RECEIVED,
    CONFIRMATION_REQUESTED,
    CONFIRMATION_RESOLVED,
    RESPONSE_READY,
    Event,
)
from alice.core.payloads import (
    CommandReceivedPayload,
    ConfirmationResolvedPayload,
)


class _Harness:
    def __init__(self, timeout: float = 1.0) -> None:
        self.bus = EventBus(max_queue_size=100)
        self.manager = ConfirmationManager(bus=self.bus, timeout_seconds=timeout)
        self.asked: list[Event] = []
        self.responses: list[Event] = []
        self.bus.subscribe(CONFIRMATION_REQUESTED, self._on(self.asked))
        self.bus.subscribe(RESPONSE_READY, self._on(self.responses))

    def _on(self, sink: list[Event]):  # noqa: ANN202 - helper de test
        async def handler(event: Event) -> None:
            sink.append(event)

        return handler

    async def start(self) -> None:
        await self.manager.start()
        self._task = self.bus.start_consumer()

    async def stop(self) -> None:
        await self.bus.stop()
        await asyncio.wait_for(self._task, timeout=2.0)
        await self.manager.stop()

    async def answer(self, text: str) -> None:
        """Simula al usuario contestando por consola/voz."""
        await self.bus.publish(
            Event(
                type=COMMAND_RECEIVED,
                source="test",
                payload=CommandReceivedPayload(text=text).model_dump(),
            )
        )


async def test_yes_grants() -> None:
    h = _Harness()
    await h.start()
    task = asyncio.create_task(h.manager.confirm("¿Ejecuto el script?"))
    await asyncio.sleep(0.05)  # deja que se publique la pregunta
    await h.answer("sí")
    granted = await asyncio.wait_for(task, timeout=2.0)
    await h.stop()

    assert granted is True
    assert len(h.asked) == 1  # se emitió confirmation.requested
    # La pregunta llega al usuario por el canal de salida (voz/consola).
    assert any("¿Ejecuto el script?" in (r.payload.get("text") or "") for r in h.responses)


async def test_no_denies() -> None:
    h = _Harness()
    await h.start()
    task = asyncio.create_task(h.manager.confirm("¿Borro la carpeta?"))
    await asyncio.sleep(0.05)
    await h.answer("no")
    granted = await asyncio.wait_for(task, timeout=2.0)
    await h.stop()
    assert granted is False


async def test_silence_denies() -> None:
    # Fail-closed: si nadie contesta, NO se autoriza.
    h = _Harness(timeout=0.2)
    await h.start()
    granted = await asyncio.wait_for(h.manager.confirm("¿Hago algo peligroso?"), timeout=2.0)
    await h.stop()
    assert granted is False


async def test_unrelated_text_does_not_answer() -> None:
    # Algo que no es sí/no no resuelve la confirmación: sigue esperando (y expira).
    h = _Harness(timeout=0.3)
    await h.start()
    task = asyncio.create_task(h.manager.confirm("¿Ejecuto el backup?"))
    await asyncio.sleep(0.05)
    await h.answer("cuéntame un chiste")
    granted = await asyncio.wait_for(task, timeout=2.0)
    await h.stop()
    assert granted is False  # expiró, no fue autorizado por accidente


async def test_is_pending_gates_the_planner() -> None:
    h = _Harness(timeout=0.5)
    await h.start()
    assert h.manager.is_pending() is False
    task = asyncio.create_task(h.manager.confirm("¿Sigo?"))
    await asyncio.sleep(0.05)
    assert h.manager.is_pending() is True  # el Planner se calla mientras tanto
    await h.answer("sí")
    await asyncio.wait_for(task, timeout=2.0)
    assert h.manager.is_pending() is False
    await h.stop()


async def test_resolved_event_from_other_channel() -> None:
    # Un botón en la web puede resolver publicando confirmation.resolved.
    h = _Harness(timeout=1.0)
    await h.start()
    task = asyncio.create_task(h.manager.confirm("¿Ejecuto?"))
    await asyncio.sleep(0.05)
    request_id = h.asked[0].payload["request_id"]
    await h.bus.publish(
        Event(
            type=CONFIRMATION_RESOLVED,
            source="test",
            payload=ConfirmationResolvedPayload(
                request_id=str(request_id), granted=True
            ).model_dump(),
        )
    )
    granted = await asyncio.wait_for(task, timeout=2.0)
    await h.stop()
    assert granted is True
