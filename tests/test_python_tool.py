"""Tests del PythonTool: siempre confirma, corre aislado y con timeout."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alice.tools.builtin.python_tool import PythonParams, PythonTool
from alice.tools.tool import Permission

if TYPE_CHECKING:
    from pathlib import Path


def test_definition_requires_shell_and_audits() -> None:
    tool = PythonTool()
    assert Permission.SHELL in tool.definition.permissions
    assert tool.definition.audit is True


def test_always_asks_for_confirmation_and_shows_the_code(tmp_path: Path) -> None:
    # Código arbitrario = poder arbitrario: nunca se ejecuta sin permiso, y el
    # usuario ve QUÉ código es antes de decidir.
    tool = PythonTool(tmp_path)
    question = tool.confirmation_question(PythonParams(code="print('hola')"))
    assert question is not None
    assert "print('hola')" in question


def test_long_code_is_truncated_in_the_question(tmp_path: Path) -> None:
    tool = PythonTool(tmp_path)
    question = tool.confirmation_question(PythonParams(code="x = 1\n" * 500))
    assert question is not None
    assert "..." in question  # no se le vuelca un muro de texto al usuario


async def test_runs_code_and_returns_stdout(tmp_path: Path) -> None:
    tool = PythonTool(tmp_path, timeout_seconds=20.0)
    result = await tool.execute(PythonParams(code="print(6 * 7)"))
    assert result.success is True, result.error
    assert result.output is not None
    assert result.output["stdout"] == "42"
    assert result.output["exit_code"] == 0


async def test_error_in_code_is_reported_not_raised(tmp_path: Path) -> None:
    tool = PythonTool(tmp_path, timeout_seconds=20.0)
    result = await tool.execute(PythonParams(code="raise ValueError('ups')"))
    assert result.success is False
    assert result.output is not None
    assert "ValueError" in result.output["stderr"]


async def test_relative_paths_land_in_the_sandbox(tmp_path: Path) -> None:
    # El cwd del proceso es el directorio aislado, no el del proyecto.
    tool = PythonTool(tmp_path, timeout_seconds=20.0)
    result = await tool.execute(
        PythonParams(code="open('salida.txt','w').write('ok'); print('escrito')")
    )
    assert result.success is True, result.error
    assert (tmp_path / "salida.txt").is_file()  # cayó en el sandbox


async def test_timeout_kills_runaway_code(tmp_path: Path) -> None:
    tool = PythonTool(tmp_path, timeout_seconds=0.5)
    result = await tool.execute(PythonParams(code="import time; time.sleep(30)"))
    assert result.success is False
    assert "super" in (result.error or "")


async def test_empty_code_is_rejected(tmp_path: Path) -> None:
    result = await PythonTool(tmp_path).execute(PythonParams(code="   "))
    assert result.success is False


async def test_sandbox_dir_is_created_on_demand(tmp_path: Path) -> None:
    target = tmp_path / "no" / "existe" / "aun"
    tool = PythonTool(target, timeout_seconds=20.0)
    result = await tool.execute(PythonParams(code="print('ok')"))
    assert result.success is True, result.error
    assert target.is_dir()
