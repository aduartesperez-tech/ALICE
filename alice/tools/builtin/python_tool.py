"""PythonTool: Alice escribe código y lo ejecuta.

Es la herramienta más potente del plan: convierte "no tengo una tool para eso" en
"me escribo una al vuelo" (calcular, transformar datos, inspeccionar archivos...).

Controles reales:
- **Siempre pide confirmación**: código arbitrario = poder arbitrario, así que el
  usuario ve el código y decide. Este es el control de verdad.
- Proceso separado con el intérprete del venv: si revienta, no toca a Alice.
- Timeout duro: el proceso se mata si se pasa.
- Directorio de trabajo aislado (``data/sandbox/``): las rutas relativas del
  código caen ahí, no en el proyecto.
- Salida capturada y recortada.

Honestidad sobre los límites: esto NO es un sandbox de seguridad. El código corre
con tus mismos permisos de usuario, y podría usar rutas absolutas o red si se lo
propusiera. Lo que impide un desastre es que tú ves el código y lo autorizas, no
un muro técnico. Por eso la confirmación aquí no es opcional.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alice.logging import get_logger
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult

_logger = get_logger("alice.tools.python")

_MAX_OUTPUT = 2000
_MAX_CODE_IN_QUESTION = 400  # cuánto código se le enseña al usuario al preguntar
_DEFAULT_TIMEOUT = 30.0


class PythonParams(BaseModel):
    """Parámetros de la tool de Python."""

    code: str = Field(
        description=(
            "Código Python a ejecutar. Imprime con print() lo que quieras obtener: "
            "solo se devuelve lo que salga por stdout/stderr."
        )
    )


class PythonTool(Tool):
    """Ejecuta código Python en un proceso aparte, previa confirmación del usuario."""

    definition = ToolDefinition(
        name="python",
        description=(
            "Ejecuta código Python que tú misma escribes, en un proceso aparte, y "
            "devuelve su salida. Úsala para cálculos, transformar datos o inspeccionar "
            "archivos cuando ninguna otra herramienta sirva. Imprime los resultados "
            "con print(). El usuario debe autorizar cada ejecución."
        ),
        parameters=PythonParams,
        permissions={Permission.SHELL},
        audit=True,
        timeout_seconds=_DEFAULT_TIMEOUT + 15.0,
    )

    def __init__(
        self, sandbox_dir: Path | None = None, *, timeout_seconds: float = _DEFAULT_TIMEOUT
    ) -> None:
        self._sandbox = (sandbox_dir or Path("data/sandbox")).resolve()
        self._timeout = timeout_seconds

    def confirmation_question(self, params: BaseModel) -> str | None:
        """Siempre pregunta, y enseña el código: es el control real de esta tool."""
        assert isinstance(params, PythonParams)
        code = params.code.strip()
        preview = code
        if len(code) > _MAX_CODE_IN_QUESTION:
            preview = code[:_MAX_CODE_IN_QUESTION] + "..."
        return f"Quiero ejecutar este código Python:\n{preview}\n¿Lo ejecuto? Dime sí o no."

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, PythonParams)
        code = params.code.strip()
        if not code:
            return ToolResult(success=False, error="no hay código que ejecutar")
        try:
            self._sandbox.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ToolResult(success=False, error=f"no pude preparar el directorio: {exc}")

        _logger.info("python.running", extra={"chars": len(code)})
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",  # aislado: ignora PYTHONPATH y el site del usuario
                "-c",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._sandbox),
            )
        except OSError as exc:
            return ToolResult(success=False, error=f"no se pudo lanzar Python: {exc}")
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                success=False, error=f"el código superó {self._timeout:.0f}s y se detuvo"
            )
        code_exit = proc.returncode or 0
        output: dict[str, Any] = {
            "exit_code": code_exit,
            "stdout": _clip(raw_out),
            "stderr": _clip(raw_err),
        }
        _logger.info("python.finished", extra={"exit_code": code_exit})
        return ToolResult(
            success=code_exit == 0,
            output=output,
            error=None if code_exit == 0 else "el código terminó con error (mira stderr)",
        )


def _clip(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n... (recortado, {len(text)} caracteres en total)"
