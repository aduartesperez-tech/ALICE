"""Plugin de ejemplo. Demuestra que "crear un módulo = crear una carpeta".

Escucha ``command.received`` y publica un evento ``echo.replied`` con el mismo
texto. No importa ningún otro módulo: solo usa el ``PluginContext`` que recibe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alice.core.events import COMMAND_RECEIVED, Event, EventPriority
from alice.core.payloads import CommandReceivedPayload
from alice.core.plugin import Plugin, PluginManifest

if TYPE_CHECKING:
    from alice.core.plugin import PluginContext

ECHO_REPLIED = "echo.replied"


class AlicePlugin(Plugin):
    """Re-emite cada comando como un evento de eco."""

    manifest = PluginManifest(
        name="echo",
        version="1.0.0",
        description="Re-emite cada comando recibido.",
        subscribes=[COMMAND_RECEIVED],
    )

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._log = ctx.get_logger()

    async def start(self) -> None:
        self._log.info("echo.started")

    async def stop(self) -> None:
        self._log.info("echo.stopped")

    async def handle_event(self, event: Event) -> None:
        payload = CommandReceivedPayload.model_validate(event.payload)
        self._log.info("echo.received", extra={"text": payload.text})
        await self._ctx.publish(
            Event(
                type=ECHO_REPLIED,
                source="plugin.echo",
                priority=EventPriority.LOW,
                correlation_id=event.correlation_id,
                payload={"text": payload.text},
            )
        )
