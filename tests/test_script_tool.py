"""Tests del ScriptTool: catálogo curado, barandillas y ejecución real."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from alice.tools.builtin.script_tool import ScriptParams, ScriptTool
from alice.tools.tool import Permission

if TYPE_CHECKING:
    from pathlib import Path

_CATALOG = """
[[script]]
name = "saluda"
description = "Imprime un saludo"
file = "saluda.py"
confirm = false

[[script]]
name = "peligroso"
description = "Haria algo serio"
file = "saluda.py"
confirm = true

[[script]]
name = "fantasma"
description = "Su archivo no existe"
file = "no_existe.py"
confirm = false

[[script]]
name = "escapista"
description = "Intenta salirse de scripts/"
file = "../../secreto.py"
confirm = false
"""


def _make_scripts(root: Path) -> ScriptTool:
    (root / "catalog.toml").write_text(_CATALOG, encoding="utf-8")
    (root / "saluda.py").write_text("print('hola desde el script')\n", encoding="utf-8")
    return ScriptTool(root, timeout_seconds=20.0)


def test_definition_requires_shell_and_audits() -> None:
    tool = ScriptTool()
    assert Permission.SHELL in tool.definition.permissions
    assert tool.definition.audit is True  # toda ejecución queda registrada


async def test_list_shows_catalog(tmp_path: Path) -> None:
    tool = _make_scripts(tmp_path)
    result = await tool.execute(ScriptParams(action="list"))
    assert result.success
    assert result.output is not None
    names = {s["name"] for s in result.output["scripts"]}
    assert names == {"saluda", "peligroso", "fantasma", "escapista"}


async def test_run_executes_and_captures_output(tmp_path: Path) -> None:
    tool = _make_scripts(tmp_path)
    result = await tool.execute(ScriptParams(action="run", name="saluda"))
    assert result.success, result.error
    assert result.output is not None
    assert result.output["exit_code"] == 0
    assert "hola desde el script" in result.output["stdout"]


async def test_unknown_script_is_rejected(tmp_path: Path) -> None:
    tool = _make_scripts(tmp_path)
    result = await tool.execute(ScriptParams(action="run", name="rm_-rf_todo"))
    assert result.success is False
    assert "no existe" in (result.error or "")


async def test_path_traversal_is_blocked(tmp_path: Path) -> None:
    # Una entrada que apunta fuera de scripts/ no se ejecuta.
    tool = _make_scripts(tmp_path)
    result = await tool.execute(ScriptParams(action="run", name="escapista"))
    assert result.success is False


async def test_missing_file_is_reported(tmp_path: Path) -> None:
    tool = _make_scripts(tmp_path)
    result = await tool.execute(ScriptParams(action="run", name="fantasma"))
    assert result.success is False


def test_confirmation_only_for_marked_scripts(tmp_path: Path) -> None:
    tool = _make_scripts(tmp_path)
    # Listar nunca pide permiso; un script con confirm=false tampoco.
    assert tool.confirmation_question(ScriptParams(action="list")) is None
    assert tool.confirmation_question(ScriptParams(action="run", name="saluda")) is None
    # El marcado con confirm=true sí, y la pregunta menciona el script.
    question = tool.confirmation_question(ScriptParams(action="run", name="peligroso"))
    assert question is not None
    assert "peligroso" in question


async def test_failing_script_reports_exit_code(tmp_path: Path) -> None:
    (tmp_path / "catalog.toml").write_text(
        '[[script]]\nname = "falla"\ndescription = "sale con error"\n'
        'file = "falla.py"\nconfirm = false\n',
        encoding="utf-8",
    )
    (tmp_path / "falla.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    tool = ScriptTool(tmp_path, timeout_seconds=20.0)
    result = await tool.execute(ScriptParams(action="run", name="falla"))
    assert result.success is False
    assert result.output is not None
    assert result.output["exit_code"] == 3


async def test_timeout_kills_runaway_script(tmp_path: Path) -> None:
    (tmp_path / "catalog.toml").write_text(
        '[[script]]\nname = "eterno"\ndescription = "no termina"\n'
        'file = "eterno.py"\nconfirm = false\n',
        encoding="utf-8",
    )
    (tmp_path / "eterno.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    tool = ScriptTool(tmp_path, timeout_seconds=0.5)
    result = await tool.execute(ScriptParams(action="run", name="eterno"))
    assert result.success is False
    assert "super" in (result.error or "")  # "superó Ns"


async def test_empty_catalog_is_safe(tmp_path: Path) -> None:
    tool = ScriptTool(tmp_path)  # sin catalog.toml
    listed = await tool.execute(ScriptParams(action="list"))
    assert listed.success
    assert listed.output == {"scripts": []}
    run = await tool.execute(ScriptParams(action="run", name="lo_que_sea"))
    assert run.success is False


def test_python_scripts_use_the_venv_interpreter() -> None:
    # El .py se corre con el intérprete actual (el del venv), no con "python"
    # del PATH, que podría ser otro sin las dependencias.
    from alice.tools.builtin.script_tool import _RUNNERS

    assert _RUNNERS[".py"] == [sys.executable]
