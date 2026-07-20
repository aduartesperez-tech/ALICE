"""ScriptTool: ejecuta scripts de un catálogo CURADO.

Principio de la Fase 2: catálogo curado antes que shell libre. Alice no puede
ejecutar cualquier cosa; solo los scripts que tú has declarado en
``scripts/catalog.toml``. Cada entrada dice cómo se llama, qué hace y si requiere
confirmación humana antes de correr.

Barandillas:
- Solo se ejecutan nombres presentes en el catálogo (nada de rutas arbitrarias).
- El archivo debe resolverse DENTRO de ``scripts/`` (corta el path traversal).
- Timeout propio y captura de salida (stdout/stderr truncados).
- Requiere el permiso ``shell``, que se otorga explícitamente en la config.
- Toda ejecución queda auditada en la memoria episódica.
"""

from __future__ import annotations

import asyncio
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from alice.logging import get_logger
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult

_logger = get_logger("alice.tools.script")

_CATALOG_FILE = "catalog.toml"
_MAX_OUTPUT = 2000  # caracteres de stdout/stderr que se devuelven
_DEFAULT_TIMEOUT = 60.0

# Intérprete por extensión. Lo que no esté aquí, no se ejecuta.
_RUNNERS: dict[str, list[str]] = {
    ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    ".py": [sys.executable],
    ".bat": ["cmd", "/c"],
    ".cmd": ["cmd", "/c"],
    ".sh": ["bash"],
}


class ScriptParams(BaseModel):
    """Parámetros de la tool de scripts."""

    action: Literal["list", "run"] = Field(
        default="list", description="'list' para ver los scripts disponibles, 'run' para ejecutar."
    )
    name: str = Field(
        default="", description="Nombre del script a ejecutar (solo con action='run')."
    )


class _ScriptEntry(BaseModel):
    """Una entrada del catálogo."""

    name: str
    description: str = ""
    file: str
    confirm: bool = True  # por defecto se pregunta: lo seguro es preguntar de más


class ScriptTool(Tool):
    """Lista y ejecuta scripts declarados en ``scripts/catalog.toml``."""

    definition = ToolDefinition(
        name="script",
        description=(
            "Lista y ejecuta scripts del catálogo local del usuario (tareas suyas ya "
            "preparadas: backups, limpieza, informes...). Usa action='list' para ver "
            "cuáles hay y action='run' con 'name' para ejecutar uno."
        ),
        parameters=ScriptParams,
        permissions={Permission.SHELL},
        audit=True,
        timeout_seconds=_DEFAULT_TIMEOUT + 15.0,  # margen sobre el timeout interno
    )

    def __init__(
        self, scripts_dir: Path | None = None, *, timeout_seconds: float = _DEFAULT_TIMEOUT
    ) -> None:
        self._dir = (scripts_dir or Path("scripts")).resolve()
        self._timeout = timeout_seconds

    # --- Catálogo -------------------------------------------------------------

    def _catalog(self) -> dict[str, _ScriptEntry]:
        """Lee el catálogo en cada llamada: añadir un script no exige reiniciar."""
        path = self._dir / _CATALOG_FILE
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            _logger.exception("script.catalog_unreadable", extra={"path": str(path)})
            return {}
        entries: dict[str, _ScriptEntry] = {}
        for raw in data.get("script", []):
            try:
                entry = _ScriptEntry.model_validate(raw)
            except Exception:  # noqa: BLE001 - una entrada mala no invalida el resto
                _logger.warning("script.catalog_bad_entry", extra={"entry": raw})
                continue
            entries[entry.name] = entry
        return entries

    def _resolve(self, entry: _ScriptEntry) -> Path | None:
        """Ruta real del script, o None si se sale de ``scripts/`` o no existe."""
        candidate = (self._dir / entry.file).resolve()
        if not candidate.is_relative_to(self._dir):
            _logger.warning("script.path_escape", extra={"file": entry.file})
            return None
        return candidate if candidate.is_file() else None

    # --- Confirmación ---------------------------------------------------------

    def confirmation_question(self, params: BaseModel) -> str | None:
        assert isinstance(params, ScriptParams)
        if params.action != "run":
            return None  # listar no muta nada
        entry = self._catalog().get(params.name)
        if entry is None or not entry.confirm:
            return None
        what = entry.description or entry.file
        return f"¿Ejecuto el script «{entry.name}»? {what}. Dime sí o no."

    # --- Ejecución ------------------------------------------------------------

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, ScriptParams)
        catalog = self._catalog()
        if params.action == "list":
            return ToolResult(
                success=True,
                output={
                    "scripts": [
                        {"name": e.name, "description": e.description, "confirma": e.confirm}
                        for e in catalog.values()
                    ]
                },
            )

        entry = catalog.get(params.name)
        if entry is None:
            return ToolResult(
                success=False,
                error=f"no existe un script llamado '{params.name}'; usa action='list'",
            )
        path = self._resolve(entry)
        if path is None:
            return ToolResult(
                success=False, error=f"el archivo del script '{entry.name}' no existe"
            )
        runner = _RUNNERS.get(path.suffix.lower())
        if runner is None:
            return ToolResult(success=False, error=f"tipo de script no soportado: {path.suffix}")
        return await self._run(entry, [*runner, str(path)])

    async def _run(self, entry: _ScriptEntry, command: list[str]) -> ToolResult:
        _logger.info("script.running", extra={"script": entry.name})
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._dir),
            )
        except OSError as exc:
            return ToolResult(success=False, error=f"no se pudo lanzar el script: {exc}")
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                success=False, error=f"el script '{entry.name}' superó {self._timeout:.0f}s"
            )
        code = proc.returncode or 0
        output: dict[str, Any] = {
            "script": entry.name,
            "exit_code": code,
            "stdout": _clip(raw_out),
            "stderr": _clip(raw_err),
        }
        _logger.info("script.finished", extra={"script": entry.name, "exit_code": code})
        return ToolResult(
            success=code == 0,
            output=output,
            error=None if code == 0 else f"el script terminó con código {code}",
        )


def _clip(raw: bytes) -> str:
    """Decodifica y recorta la salida para que no infle el prompt del LLM."""
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n... (recortado, {len(text)} caracteres en total)"
