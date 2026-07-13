"""Plugin de consola: entrada/salida por terminal.

- Lee líneas de stdin (en un hilo daemon) y publica ``command.received``.
- Escucha ``response.ready`` y las imprime.

Es un plugin normal: solo conoce su ``PluginContext`` y tipos de ``core``. No
importa ningún otro módulo del sistema — valida que la arquitectura de plugins
basta para dar boca y oídos a Alice sin tocar el núcleo.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import TYPE_CHECKING

from alice.core.events import COMMAND_RECEIVED, RESPONSE_READY, Event
from alice.core.observation import render_observations_plain
from alice.core.payloads import CommandReceivedPayload, ResponseReadyPayload
from alice.core.plugin import Plugin, PluginManifest

if TYPE_CHECKING:
    from alice.core.plugin import PluginContext

_PROMPT = "tú> "
_ALICE = "alice> "


class AlicePlugin(Plugin):
    """Canal de entrada/salida por terminal."""

    manifest = PluginManifest(
        name="console",
        version="1.0.0",
        description="Entrada/salida por terminal.",
        subscribes=[RESPONSE_READY],
    )

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._log = ctx.get_logger()
        self._running = False
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        # La consola de Windows no usa UTF-8 por defecto; lo forzamos para los acentos.
        for stream in (sys.stdout, sys.stdin):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")
        if not sys.stdin or not sys.stdin.isatty():
            # Bajo pytest o entrada no interactiva no arrancamos el lector.
            self._log.info("console.non_interactive")
            return
        self._thread = threading.Thread(target=self._read_loop, name="console_stdin", daemon=True)
        self._thread.start()
        banner = "\nAlice lista. Escribe un comando (Ctrl+C para salir).\n"
        print(f"{banner}{_PROMPT}", end="", flush=True)

    async def stop(self) -> None:
        self._running = False

    async def handle_event(self, event: Event) -> None:
        if event.type != RESPONSE_READY:
            return
        payload = ResponseReadyPayload.model_validate(event.payload)
        text = payload.text or render_observations_plain(payload.observations)
        print(f"\n{_ALICE}{text}\n{_PROMPT}", end="", flush=True)

    def _read_loop(self) -> None:
        """Lee stdin en un hilo daemon y encola cada línea como comando en el loop."""
        for line in sys.stdin:
            if not self._running:
                break
            text = line.strip()
            if not text:
                print(_PROMPT, end="", flush=True)
                continue
            asyncio.run_coroutine_threadsafe(self._publish_command(text), self._loop)

    async def _publish_command(self, text: str) -> None:
        await self._ctx.publish(
            Event(
                type=COMMAND_RECEIVED,
                source="plugin.console",
                payload=CommandReceivedPayload(text=text).model_dump(),
            )
        )
