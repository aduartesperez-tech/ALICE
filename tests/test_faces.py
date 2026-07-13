"""Tests del almacén de rostros (plugins/vision/faces.py): enrolar, identificar,
persistir y olvidar. Sin cámara ni modelos: solo embeddings sintéticos."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# faces.py vive en el plugin, no en el paquete alice: lo hacemos importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "vision"))
from faces import DEFAULT_THRESHOLD, FaceStore  # type: ignore[import-not-found]  # noqa: E402


def _vec(seed: int, dim: int = 512) -> np.ndarray:
    """Embedding pseudoaleatorio reproducible (sin normalizar: el store lo hace)."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal(dim).astype(np.float32)


def test_enroll_and_identify_same_vector(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    v = _vec(1)
    store.enroll("Adrian", v)
    name, sim = store.identify(v)
    assert name == "Adrian"
    assert sim == pytest.approx(1.0, abs=1e-4)  # idéntico -> coseno 1


def test_unknown_face_below_threshold(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.enroll("Adrian", _vec(1))
    # Un vector distinto y aleatorio no debe superar el umbral.
    name, sim = store.identify(_vec(999))
    assert name is None
    assert sim < DEFAULT_THRESHOLD


def test_empty_store_identifies_nobody(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    name, _ = store.identify(_vec(1))
    assert name is None


def test_persistence_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "faces.json"
    FaceStore(path).enroll("Adrian", _vec(1))
    # Una nueva instancia lee del disco.
    reloaded = FaceStore(path)
    assert reloaded.names() == ["Adrian"]
    name, _ = reloaded.identify(_vec(1))
    assert name == "Adrian"


def test_multiple_samples_per_person(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.enroll("Adrian", _vec(1))
    total = store.enroll("Adrian", _vec(2))
    assert total == 2
    assert store.names() == ["Adrian"]


def test_forget(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    store.enroll("Adrian", _vec(1))
    assert store.forget("Adrian") is True
    assert store.names() == []
    assert store.forget("Nadie") is False


def test_best_match_wins_among_people(tmp_path: Path) -> None:
    store = FaceStore(tmp_path / "faces.json")
    va, vb = _vec(1), _vec(2)
    store.enroll("Adrian", va)
    store.enroll("Bea", vb)
    assert store.identify(va)[0] == "Adrian"
    assert store.identify(vb)[0] == "Bea"
