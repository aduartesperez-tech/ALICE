"""Plugin voice_web: interfaz web para hablar con Alice (voz + texto).

Levanta un servidor local (aiohttp) que sirve una página de chat. La página:
- envía texto o audio del micrófono;
- el audio se transcribe con Whisper (faster-whisper) y se manda como comando;
- las respuestas de Alice llegan por WebSocket y se muestran y se leen en voz
  alta (TTS del navegador).

Encaja como un plugin normal: publica ``command.received`` cuando el usuario
habla/escribe y escucha ``response.ready`` para devolver la respuesta. No toca
el núcleo. Config por variables de entorno:
  ALICE_VOICE_HOST (def. 127.0.0.1), ALICE_VOICE_PORT (def. 8756),
  ALICE_WHISPER_MODEL (def. small).
"""

from __future__ import annotations

import os
import sys
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from alice.core.events import COMMAND_RECEIVED, RESPONSE_READY, Event
from alice.core.observation import render_observations_plain
from alice.core.payloads import CommandReceivedPayload, ResponseReadyPayload
from alice.core.plugin import Plugin, PluginManifest

# El loader importa plugin.py por ruta, sin añadir su carpeta al path: lo hacemos
# aquí para poder importar el módulo hermano ``transcribe``.
sys.path.insert(0, str(Path(__file__).parent))
from transcribe import WhisperTranscriber  # noqa: E402  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from alice.core.plugin import PluginContext

_STATIC_DIR = Path(__file__).parent / "static"


class AlicePlugin(Plugin):
    """Canal web de entrada/salida con voz."""

    manifest = PluginManifest(
        name="voice_web",
        version="1.0.0",
        description="Interfaz web con voz (Whisper + TTS).",
        subscribes=[RESPONSE_READY],
    )

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._log = ctx.get_logger()
        self._host = os.environ.get("ALICE_VOICE_HOST", "127.0.0.1")
        self._port = int(os.environ.get("ALICE_VOICE_PORT", "8756"))
        model = os.environ.get("ALICE_WHISPER_MODEL", "small")
        device = os.environ.get("ALICE_WHISPER_DEVICE", "cpu")
        compute = os.environ.get("ALICE_WHISPER_COMPUTE", "int8")
        self._transcriber = WhisperTranscriber(
            model_size=model, device=device, compute_type=compute
        )
        # Clientes WebSocket conectados; weak-set para no retener sockets muertos.
        self._clients: weakref.WeakSet[web.WebSocketResponse] = weakref.WeakSet()
        self._runner: web.AppRunner | None = None
        self._app = self._build_app()

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/ws", self._handle_ws),
                web.post("/transcribe", self._handle_transcribe),
            ]
        )
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        url = f"http://{self._host}:{self._port}"
        self._log.info("voice_web.started", extra={"url": url})
        print(f"\n🎙️  Interfaz de voz de Alice: {url}\n", flush=True)

    async def stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- Salida: respuestas de Alice -> navegador ----------------------------

    async def handle_event(self, event: Event) -> None:
        if event.type != RESPONSE_READY:
            return
        payload = ResponseReadyPayload.model_validate(event.payload)
        text = payload.text or render_observations_plain(payload.observations)
        await self._broadcast({"type": "response", "text": text})

    async def _broadcast(self, message: dict[str, str]) -> None:
        for ws in list(self._clients):
            if not ws.closed:
                try:
                    await ws.send_json(message)
                except ConnectionError:  # cliente que se fue a mitad de envío
                    self._log.warning("voice_web.send_failed")

    # --- Rutas HTTP -----------------------------------------------------------

    async def _handle_index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        self._log.info("voice_web.client_connected")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_client_text(msg.json())
                elif msg.type == WSMsgType.ERROR:
                    self._log.warning("voice_web.ws_error", extra={"exc": str(ws.exception())})
        finally:
            self._clients.discard(ws)
            self._log.info("voice_web.client_disconnected")
        return ws

    async def _on_client_text(self, data: dict[str, str]) -> None:
        """Mensaje del navegador por WS: un comando de texto para Alice."""
        if data.get("type") == "text":
            text = (data.get("text") or "").strip()
            if text:
                await self._publish_command(text)

    async def _handle_transcribe(self, request: web.Request) -> web.Response:
        """Recibe audio del micrófono, lo transcribe y publica el comando."""
        audio = await request.read()
        if not audio:
            return web.json_response({"error": "audio vacío"}, status=400)
        try:
            text = await self._transcriber.transcribe(audio)
        except Exception as exc:  # noqa: BLE001 - fallo de STT no debe tumbar el server
            self._log.exception("voice_web.transcribe_failed")
            return web.json_response({"error": str(exc)}, status=500)
        if text:
            await self._publish_command(text)
        return web.json_response({"text": text})

    async def _publish_command(self, text: str) -> None:
        await self._ctx.publish(
            Event(
                type=COMMAND_RECEIVED,
                source="plugin.voice_web",
                payload=CommandReceivedPayload(text=text).model_dump(),
            )
        )
