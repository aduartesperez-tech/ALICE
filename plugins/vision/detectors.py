"""Motor de visión: detección + reconocimiento de caras (InsightFace) y gestos
de mano (MediaPipe Tasks GestureRecognizer).

Todo el trabajo pesado (modelos, inferencia) vive aquí. El plugin solo maneja la
cámara, el streaming y los eventos. ``process`` corre una imagen y devuelve un
``VisionResult`` estructurado; ``annotate`` dibuja las detecciones sobre el frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from alice.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from faces import FaceStore
    from numpy.typing import NDArray

_logger = get_logger("alice.plugin.vision.detectors")

# Gestos crudos de MediaPipe -> nombre canónico (más legible para eventos/LLM).
_GESTURE_MAP = {
    "Open_Palm": "open_palm",
    "Closed_Fist": "closed_fist",
    "Thumb_Up": "thumb_up",
    "Thumb_Down": "thumb_down",
    "Victory": "victory",
    "Pointing_Up": "pointing_up",
    "ILoveYou": "i_love_you",
}

_GREEN = (80, 220, 100)
_ORANGE = (60, 160, 240)


@dataclass
class FaceDetection:
    """Una cara detectada en un frame."""

    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    name: str | None  # None si es desconocida
    score: float  # similitud coseno con el match (o det_score si sin enrolados)
    embedding: NDArray[np.float32] | None = None


@dataclass
class VisionResult:
    """Resultado de procesar un frame."""

    faces: list[FaceDetection] = field(default_factory=list)
    gesture: str | None = None
    gesture_hand: str | None = None

    @property
    def present(self) -> bool:
        return len(self.faces) > 0

    @property
    def count(self) -> int:
        return len(self.faces)

    def known_names(self) -> list[str]:
        return [f.name for f in self.faces if f.name]


class VisionEngine:
    """Detección/reconocimiento de caras y gestos. Se carga con ``load`` (pesado)."""

    def __init__(
        self,
        *,
        face_store: FaceStore,
        gesture_model_path: Path,
        det_size: int = 320,
        min_gesture_score: float = 0.5,
    ) -> None:
        self._faces = face_store
        self._gesture_model_path = gesture_model_path
        self._det_size = det_size
        self._min_gesture_score = min_gesture_score
        self._app: Any | None = None
        self._recognizer: Any | None = None

    def load(self) -> None:
        """Carga los modelos (bloqueante ~segundos + descarga la 1ª vez)."""
        from insightface.app import FaceAnalysis

        _logger.info("vision.loading_insightface")
        self._app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        self._app.prepare(ctx_id=0, det_size=(self._det_size, self._det_size))

        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        _logger.info("vision.loading_gesture_model")
        options = mp_vision.GestureRecognizerOptions(
            base_options=mp_python.BaseOptions(
                model_asset_path=str(self._gesture_model_path)
            ),
            running_mode=mp_vision.RunningMode.IMAGE,
            num_hands=2,
        )
        self._recognizer = mp_vision.GestureRecognizer.create_from_options(options)
        self._mp = mp
        _logger.info("vision.engine_ready")

    def process(self, frame_bgr: NDArray[np.uint8]) -> VisionResult:
        """Procesa un frame BGR y devuelve caras (con identidad) y gesto."""
        result = VisionResult()
        result.faces = self._detect_faces(frame_bgr)
        gesture, hand = self._detect_gesture(frame_bgr)
        result.gesture, result.gesture_hand = gesture, hand
        return result

    def _detect_faces(self, frame_bgr: NDArray[np.uint8]) -> list[FaceDetection]:
        assert self._app is not None
        out: list[FaceDetection] = []
        for face in self._app.get(frame_bgr):
            emb = face.normed_embedding.astype(np.float32)
            name, sim = self._faces.identify(emb)
            x1, y1, x2, y2 = face.bbox.astype(int).tolist()
            out.append(
                FaceDetection(
                    bbox=(x1, y1, x2, y2),
                    name=name,
                    score=float(sim),
                    embedding=emb,
                )
            )
        return out

    def _detect_gesture(self, frame_bgr: NDArray[np.uint8]) -> tuple[str | None, str | None]:
        assert self._recognizer is not None
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        res = self._recognizer.recognize(mp_image)
        if not res.gestures:
            return None, None
        top = res.gestures[0][0]  # gesto principal de la primera mano
        if top.score < self._min_gesture_score:
            return None, None
        canonical = _GESTURE_MAP.get(top.category_name)
        if canonical is None:
            return None, None
        hand = None
        if res.handedness:
            hand = res.handedness[0][0].category_name.lower()  # "left" | "right"
        return canonical, hand

    def annotate(self, frame_bgr: NDArray[np.uint8], result: VisionResult) -> NDArray[np.uint8]:
        """Dibuja cajas de cara con nombre y una etiqueta del gesto."""
        for face in result.faces:
            x1, y1, x2, y2 = face.bbox
            color = _GREEN if face.name else _ORANGE
            label = f"{face.name} ({face.score:.2f})" if face.name else "desconocido"
            cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), color, 2)
            cv2.rectangle(frame_bgr, (x1, y1 - 22), (x2, y1), color, -1)
            cv2.putText(
                frame_bgr, label, (x1 + 4, y1 - 6),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA,
            )
        if result.gesture:
            text = f"gesto: {result.gesture}"
            cv2.putText(
                frame_bgr, text, (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, _GREEN, 2, cv2.LINE_AA,
            )
        return frame_bgr
