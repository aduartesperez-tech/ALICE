"""Plugin vision: Alice ve por la webcam y reconoce caras, presencia y gestos.

Un hilo maneja la cámara (OpenCV, bloqueante) y corre el motor de visión; el
resultado se anota sobre el vídeo, que se sirve como stream MJPEG en una página
web. Los cambios relevantes (aparece alguien, te reconoce, haces un gesto) se
publican como eventos de percepción en el bus, y opcionalmente disparan una
reacción hablada (un ``command.received`` que Alice narra y dice en voz alta).

Config por entorno:
  ALICE_VISION_PORT (8757), ALICE_VISION_CAMERA (0), ALICE_VISION_REACT (1).
  Rendimiento (para equipos modestos):
    ALICE_VISION_INFER_INTERVAL (0.4 s entre inferencias pesadas; súbelo si va lento)
    ALICE_VISION_FPS (15, cadencia del vídeo mostrado)
    ALICE_VISION_WIDTH (640) / ALICE_VISION_HEIGHT (480)
    ALICE_VISION_DET_SIZE (320)
    ALICE_VISION_MODEL (buffalo_l; usa buffalo_s para mucha menos CPU)
    ALICE_VISION_GESTURES (1; ponlo a 0 para apagar los gestos y ahorrar CPU)
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
from aiohttp import web

from alice.core.events import (
    COMMAND_RECEIVED,
    FACE_RECOGNIZED,
    HAND_DETECTED,
    PERSON_DETECTED,
    Event,
)
from alice.core.payloads import (
    CommandReceivedPayload,
    FaceRecognizedPayload,
    GestureDetectedPayload,
    PersonDetectedPayload,
)
from alice.core.plugin import Plugin, PluginManifest

sys.path.insert(0, str(Path(__file__).parent))
from detectors import VisionEngine, VisionResult  # noqa: E402  # type: ignore[import-not-found]
from faces import FaceStore  # noqa: E402  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from alice.core.plugin import PluginContext

_STATIC_DIR = Path(__file__).parent / "static"
_GESTURE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/1/gesture_recognizer.task"
)

# Ajustes de estabilidad/anti-spam (en inferencias o segundos).
_PRESENCE_FRAMES = 3  # inferencias consecutivas para confirmar un cambio de presencia
# Solo se vuelve a saludar a alguien si estuvo AUSENTE al menos esto. Mientras
# sigas presente, Alice saluda UNA vez (al llegar) y no repite: se siente natural.
_REGREET_ABSENCE = 180.0
_GESTURE_STABLE = 3  # inferencias con el mismo gesto para darlo por válido
_GESTURE_COOLDOWN = 6.0  # espera entre reacciones a gestos
_REACTION_COOLDOWN = 12.0  # freno global entre cualquier par de reacciones habladas


class AlicePlugin(Plugin):
    """Ojos de Alice: cámara, reconocimiento y reacciones."""

    manifest = PluginManifest(
        name="vision",
        version="1.0.0",
        description="Visión por webcam: caras, presencia y gestos.",
        subscribes=[],
    )

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._log = ctx.get_logger()
        self._port = int(os.environ.get("ALICE_VISION_PORT", "8757"))
        self._camera_index = int(os.environ.get("ALICE_VISION_CAMERA", "0"))
        det_size = int(os.environ.get("ALICE_VISION_DET_SIZE", "320"))
        self._react = os.environ.get("ALICE_VISION_REACT", "1") != "0"
        # Tunables de rendimiento (equipos modestos): freno de inferencia, cadencia
        # de vídeo, resolución de captura, modelo y si se detectan gestos.
        self._infer_interval = float(os.environ.get("ALICE_VISION_INFER_INTERVAL", "0.4"))
        self._display_fps = float(os.environ.get("ALICE_VISION_FPS", "15"))
        self._cap_width = int(os.environ.get("ALICE_VISION_WIDTH", "640"))
        self._cap_height = int(os.environ.get("ALICE_VISION_HEIGHT", "480"))
        model_name = os.environ.get("ALICE_VISION_MODEL", "buffalo_l")
        enable_gestures = os.environ.get("ALICE_VISION_GESTURES", "1") != "0"

        data_dir = Path("data")
        model_path = data_dir / "gesture_recognizer.task"
        if enable_gestures:
            self._ensure_gesture_model(model_path)
        self._faces = FaceStore(data_dir / "faces.json")
        self._engine = VisionEngine(
            face_store=self._faces,
            gesture_model_path=model_path,
            det_size=det_size,
            model_name=model_name,
            enable_gestures=enable_gestures,
        )

        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._latest_jpeg: bytes | None = None
        self._latest_result = VisionResult()
        self._last_process_time = 0.0  # time.time() del último frame procesado
        self._result_lock = threading.Lock()

        # Estado para debounce/cooldown de eventos y reacciones.
        self._present = False
        self._presence_run = 0
        self._last_seen: dict[str, float] = {}  # persona -> última vez vista (monotonic)
        self._last_gesture: str | None = None
        self._gesture_run = 0
        self._last_gesture_react = 0.0
        self._last_reaction = 0.0  # freno global: evita encolar reacciones en ráfaga
        self._stream_clients = 0  # nº de navegadores mirando el stream (para no codificar en balde)

        self._runner: web.AppRunner | None = None
        self._app = self._build_app()

    def _ensure_gesture_model(self, path: Path) -> None:
        if path.is_file():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._log.info("vision.downloading_gesture_model")
        urllib.request.urlretrieve(_GESTURE_MODEL_URL, path)  # noqa: S310 - URL fija de Google

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/stream", self._handle_stream),
                web.get("/faces", self._handle_faces),
                web.get("/state", self._handle_state),
                web.post("/enroll", self._handle_enroll),
                web.post("/forget", self._handle_forget),
            ]
        )
        return app

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()
        self._running = True
        self._thread = threading.Thread(target=self._camera_loop, name="vision_cam", daemon=True)
        self._thread.start()
        url = f"http://127.0.0.1:{self._port}"
        self._log.info("vision.started", extra={"url": url})
        print(f"\n👁️  Visión de Alice: {url}\n", flush=True)

    async def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def handle_event(self, event: Event) -> None:
        # No se suscribe a ningún evento (solo publica percepción); no-op.
        return None

    # --- Hilo de cámara -------------------------------------------------------

    def _camera_loop(self) -> None:
        """Captura, procesa (con throttle por tiempo), anota y publica. Hilo daemon."""
        try:
            self._engine.load()
        except Exception:  # noqa: BLE001 - sin modelos no hay visión, pero no tumba Alice
            self._log.exception("vision.engine_load_failed")
            return
        cap = cv2.VideoCapture(self._camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self._log.warning("vision.camera_unavailable", extra={"index": self._camera_index})
            return
        # Baja resolución + buffer mínimo: menos CPU y sin lag acumulado de frames viejos.
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._cap_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cap_height)
        cap.set(cv2.CAP_PROP_FPS, self._display_fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        result = VisionResult()
        frame_period = 1.0 / self._display_fps if self._display_fps > 0 else 0.0
        last_infer = 0.0
        while self._running:
            tick = time.time()
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            # La inferencia pesada (caras + gestos, InsightFace/MediaPipe en CPU) se
            # limita por TIEMPO, no por frame: es el freno que evita saturar la CPU
            # y dejar el equipo trabado. Entre inferencias se reusa el último result.
            if tick - last_infer >= self._infer_interval:
                last_infer = tick
                try:
                    result = self._engine.process(frame)
                except Exception:  # noqa: BLE001 - un frame malo no rompe el bucle
                    self._log.exception("vision.process_failed")
                    result = VisionResult()
                with self._result_lock:
                    self._latest_result = result
                    self._last_process_time = time.time()
                self._handle_transitions(result)
            # Codificar el JPEG solo si alguien está viendo el stream: si la página
            # web no está abierta (lo normal), nos ahorramos ese trabajo por frame.
            if self._stream_clients > 0:
                annotated = self._engine.annotate(frame, result)
                ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self._latest_jpeg = buf.tobytes()
            # Cadencia del bucle: cede CPU en vez de girar a tope.
            spent = time.time() - tick
            if frame_period > spent:
                time.sleep(frame_period - spent)
        cap.release()

    def _handle_transitions(self, result: VisionResult) -> None:
        """Detecta cambios relevantes y emite eventos/reacciones (con debounce)."""
        self._track_presence(result)
        self._track_identity(result)
        self._track_gesture(result)

    def _track_presence(self, result: VisionResult) -> None:
        if result.present == self._present:
            self._presence_run = 0
            return
        self._presence_run += 1
        if self._presence_run >= _PRESENCE_FRAMES:
            self._present = result.present
            self._presence_run = 0
            self._emit_event(
                PERSON_DETECTED,
                PersonDetectedPayload(present=self._present, count=result.count).model_dump(),
            )

    def _track_identity(self, result: VisionResult) -> None:
        """Saluda a alguien AL LLEGAR, no en bucle: solo la primera vez que aparece
        o cuando vuelve tras una ausencia real. Mientras siga presente, calla."""
        now = time.monotonic()
        for name in result.known_names():
            last = self._last_seen.get(name)
            self._last_seen[name] = now
            # Sigue presente (o se ausentó solo un instante): no repetir el saludo.
            if last is not None and now - last < _REGREET_ABSENCE:
                continue
            self._emit_event(
                FACE_RECOGNIZED,
                FaceRecognizedPayload(name=name, known=True).model_dump(),
            )
            if last is None:
                self._react_text(
                    f"(Acabas de ver a {name} por la cámara. Salúdale por su nombre, "
                    f"breve, cálida y natural, como una amiga que se alegra de verle.)"
                )
            else:
                self._react_text(
                    f"(Vuelves a ver a {name} tras un rato sin verle. Salúdale con "
                    f"naturalidad y cariño, muy breve, como a un amigo que regresa.)"
                )

    def _track_gesture(self, result: VisionResult) -> None:
        gesture = result.gesture
        if gesture and gesture == self._last_gesture:
            self._gesture_run += 1
        else:
            self._gesture_run = 1 if gesture else 0
            self._last_gesture = gesture
        if not gesture or self._gesture_run != _GESTURE_STABLE:
            return
        self._emit_event(
            HAND_DETECTED,
            GestureDetectedPayload(gesture=gesture, hand=result.gesture_hand).model_dump(),
        )
        now = time.monotonic()
        if now - self._last_gesture_react >= _GESTURE_COOLDOWN:
            self._last_gesture_react = now
            prompt = _GESTURE_PROMPTS.get(gesture)
            if prompt:
                self._react_text(prompt)

    # --- Puente hilo -> loop asyncio -----------------------------------------

    def _emit_event(self, event_type: str, payload: dict[str, object]) -> None:
        self._publish(Event(type=event_type, source="plugin.vision", payload=payload))

    def _react_text(self, text: str) -> None:
        if not self._react:
            return
        # Freno global: nunca dispares dos reacciones muy seguidas, aunque sean
        # de distinto tipo (saludo + varios gestos). Evita encolar llamadas al
        # LLM más rápido de lo que se responden (lo que atascaba el sistema).
        now = time.monotonic()
        if now - self._last_reaction < _REACTION_COOLDOWN:
            return
        self._last_reaction = now
        # internal=True: es una reacción, no una petición del usuario. Va directa
        # a chat (sin selección de tools ni clasificación), más rápida y barata.
        self._publish(
            Event(
                type=COMMAND_RECEIVED,
                source="plugin.vision",
                payload=CommandReceivedPayload(text=text, internal=True).model_dump(),
            )
        )

    def _publish(self, event: Event) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._ctx.publish(event), self._loop)

    # --- Rutas HTTP -----------------------------------------------------------

    async def _handle_index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(_STATIC_DIR / "vision.html")

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"}
        )
        await resp.prepare(request)
        # Con al menos un espectador, el bucle de cámara codifica el JPEG.
        self._stream_clients += 1
        try:
            while self._running:
                jpeg = self._latest_jpeg
                if jpeg is not None:
                    await resp.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
                    )
                await asyncio.sleep(0.05)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._stream_clients = max(0, self._stream_clients - 1)
        return resp

    async def _handle_faces(self, _request: web.Request) -> web.Response:
        return web.json_response({"faces": self._faces.names()})

    async def _handle_state(self, _request: web.Request) -> web.Response:
        """Lo que Alice ve ahora mismo, en JSON (sin embeddings). Lo consume la tool camera."""
        with self._result_lock:
            result = self._latest_result
            when = self._last_process_time
        age = round(time.time() - when, 1) if when else None
        faces = [
            {
                "name": f.name,
                "known": f.name is not None,
                "score": round(f.score, 3),
            }
            for f in result.faces
        ]
        return web.json_response(
            {
                "present": result.present,
                "count": result.count,
                "faces": faces,
                "known_names": result.known_names(),
                "gesture": result.gesture,
                "gesture_hand": result.gesture_hand,
                "seconds_since_frame": age,
            }
        )

    async def _handle_enroll(self, request: web.Request) -> web.Response:
        data = await request.json()
        name = str(data.get("name", "")).strip()
        if not name:
            return web.json_response({"error": "falta el nombre"}, status=400)
        with self._result_lock:
            faces = self._latest_result.faces
        if not faces:
            return web.json_response({"error": "no veo ninguna cara ahora mismo"}, status=400)
        # La cara más grande (la más cercana) es la que se enrola.
        target = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        if target.embedding is None:
            return web.json_response({"error": "no pude extraer el rostro"}, status=400)
        total = self._faces.enroll(name, target.embedding)
        self._log.info("vision.enrolled", extra={"name": name, "samples": total})
        return web.json_response({"name": name, "samples": total})

    async def _handle_forget(self, request: web.Request) -> web.Response:
        data = await request.json()
        name = str(data.get("name", "")).strip()
        existed = self._faces.forget(name)
        return web.json_response({"forgotten": existed})


_GESTURE_PROMPTS = {
    "open_palm": (
        "(El usuario te saluda con la mano abierta por la cámara. "
        "Responde al saludo, breve.)"
    ),
    "thumb_up": (
        "(El usuario te hace un pulgar arriba por la cámara. "
        "Responde con entusiasmo, muy breve.)"
    ),
    "victory": (
        "(El usuario te hace el gesto de victoria por la cámara. "
        "Reacciona con buen rollo, muy breve.)"
    ),
    "i_love_you": (
        "(El usuario te hace el gesto de 'te quiero' por la cámara. "
        "Responde con cariño, muy breve.)"
    ),
}
