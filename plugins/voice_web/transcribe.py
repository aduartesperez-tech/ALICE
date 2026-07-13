"""Transcripción de voz a texto con faster-whisper (local, offline).

El modelo se carga de forma perezosa (la primera vez que se transcribe) y en un
hilo aparte, para no bloquear el event loop ni penalizar el arranque. Si
faster-whisper no está instalado, ``transcribe`` lanza un error claro; el resto
del plugin (chat de texto, TTS) sigue funcionando.
"""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any

from alice.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

_logger = get_logger("alice.plugin.voice_web.transcribe")


class WhisperTranscriber:
    """Envuelve un ``WhisperModel`` de faster-whisper con carga perezosa."""

    def __init__(
        self,
        *,
        model_size: str = "small",
        # CPU/int8 por defecto: siempre funciona. "cuda" requiere tener las libs
        # de CUDA (cuBLAS/cuDNN) en el PATH, que no vienen con faster-whisper.
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = "es",
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model: Any | None = None
        self._lock = asyncio.Lock()

    def _load_model(self) -> Any:
        """Importa faster-whisper y construye el modelo. Bloqueante: va en un hilo."""
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError(
                "faster-whisper no está instalado. Instala con: "
                "pip install faster-whisper"
            ) from exc
        _logger.info(
            "whisper.loading_model",
            extra={"model": self._model_size, "device": self._device},
        )
        model = WhisperModel(
            self._model_size, device=self._device, compute_type=self._compute_type
        )
        _logger.info("whisper.model_ready", extra={"model": self._model_size})
        return model

    async def _ensure_model(self) -> Any:
        # Doble comprobación con lock: solo un hilo carga el modelo, aunque
        # lleguen varias transcripciones a la vez al arrancar.
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is None:
                loop = asyncio.get_running_loop()
                self._model = await loop.run_in_executor(None, self._load_model)
            return self._model

    async def transcribe(self, audio: bytes) -> str:
        """Transcribe audio (bytes de un archivo webm/ogg/wav) a texto."""
        model = await self._ensure_model()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._run, model, audio)

    def _run(self, model: Any, audio: bytes) -> str:
        """Ejecuta la transcripción (bloqueante). faster-whisper decodifica el
        contenedor (webm/opus, ogg, wav...) internamente con PyAV."""
        segments: Iterable[Any]
        segments, _info = model.transcribe(
            io.BytesIO(audio),
            language=self._language,
            vad_filter=True,  # recorta silencios: más rápido y menos alucinación
        )
        text = "".join(segment.text for segment in segments).strip()
        _logger.info("whisper.transcribed", extra={"chars": len(text)})
        return text
