"""Tests de configuración: cargador .env sin dependencias."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from alice.config.settings import _load_dotenv

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_KEY = "ALICE_TEST_DOTENV_VALUE"


def test_dotenv_loads_and_strips_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(f'# un comentario\n{_KEY}="con-comillas"\n\n', encoding="utf-8")
    monkeypatch.delenv(_KEY, raising=False)
    _load_dotenv(env)
    assert os.environ[_KEY] == "con-comillas"  # comillas envolventes quitadas


def test_dotenv_does_not_override_real_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(f"{_KEY}=del-archivo\n", encoding="utf-8")
    monkeypatch.setenv(_KEY, "del-entorno")
    _load_dotenv(env)
    assert os.environ[_KEY] == "del-entorno"  # la variable real del entorno gana


def test_dotenv_missing_file_is_noop(tmp_path: Path) -> None:
    _load_dotenv(tmp_path / "no-existe.env")  # no debe lanzar
