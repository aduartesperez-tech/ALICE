"""Almacén de rostros enrolados: nombre -> embeddings, con reconocimiento por
similitud coseno.

Persistente en un JSON simple (``data/faces.json``). Cada persona guarda varios
embeddings (distintos ángulos/luz) para robustez. El embedding de InsightFace ya
viene L2-normalizado, así que la similitud coseno es el producto punto.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

# Umbral de similitud coseno para dar por reconocida a una persona. Los
# embeddings ArcFace de InsightFace rondan 0.5-0.7 para la misma persona y
# <0.3 para distintas; 0.38 es un punto medio prudente.
DEFAULT_THRESHOLD = 0.38


class FaceStore:
    """Base de rostros enrolados con persistencia JSON y matching coseno."""

    def __init__(self, path: Path, *, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._path = path
        self._threshold = threshold
        self._lock = threading.Lock()
        # nombre -> lista de embeddings (cada uno np.ndarray float32).
        self._people: dict[str, list[NDArray[np.float32]]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._people = {
            name: [np.asarray(vec, dtype=np.float32) for vec in vecs]
            for name, vecs in data.items()
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {name: [vec.tolist() for vec in vecs] for name, vecs in self._people.items()}
        self._path.write_text(json.dumps(data), encoding="utf-8")

    def enroll(self, name: str, embedding: NDArray[np.float32]) -> int:
        """Añade un embedding a una persona. Devuelve cuántos embeddings tiene ya."""
        vec = _normalize(embedding)
        with self._lock:
            self._people.setdefault(name, []).append(vec)
            self._save()
            return len(self._people[name])

    def identify(self, embedding: NDArray[np.float32]) -> tuple[str | None, float]:
        """Devuelve (nombre, similitud) del mejor match, o (None, sim) si no supera
        el umbral o no hay nadie enrolado."""
        vec = _normalize(embedding)
        best_name: str | None = None
        best_sim = -1.0
        with self._lock:
            for name, vecs in self._people.items():
                for known in vecs:
                    sim = float(np.dot(vec, known))
                    if sim > best_sim:
                        best_sim, best_name = sim, name
        if best_name is not None and best_sim >= self._threshold:
            return best_name, best_sim
        return None, best_sim

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._people)

    def forget(self, name: str) -> bool:
        with self._lock:
            existed = self._people.pop(name, None) is not None
            if existed:
                self._save()
            return existed


def _normalize(vec: NDArray[np.float32]) -> NDArray[np.float32]:
    """L2-normaliza (por si el embedding no viniera ya normalizado)."""
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else arr
