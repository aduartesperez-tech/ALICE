"""AppLauncherTool: abrir y cerrar aplicaciones. El nivel de riesgo más bajo.

Sin dependencias: abre con ``start`` de Windows y cierra con ``taskkill``. Trae
un diccionario de alias en español para que "abre el bloc de notas" funcione sin
que el usuario sepa que el ejecutable se llama ``notepad``.

Barandillas:
- Cierre **amable** (``taskkill`` sin ``/f``): la app pide guardar si hace falta,
  así que cerrar no destruye trabajo y no necesita confirmación.
- Los procesos críticos del sistema no se tocan ni con confirmación: matar
  ``lsass`` o ``csrss`` tumba Windows.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from alice.logging import get_logger
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult

_logger = get_logger("alice.tools.app")

_TIMEOUT = 15.0

# Alias coloquial -> (qué se abre, proceso para cerrarlo).
_APPS: dict[str, tuple[str, str]] = {
    "bloc de notas": ("notepad", "notepad.exe"),
    "notepad": ("notepad", "notepad.exe"),
    "calculadora": ("calc", "CalculatorApp.exe"),
    "explorador": ("explorer", "explorer.exe"),
    "explorador de archivos": ("explorer", "explorer.exe"),
    "navegador": ("https://www.google.com", "msedge.exe"),
    "chrome": ("chrome", "chrome.exe"),
    "edge": ("msedge", "msedge.exe"),
    "firefox": ("firefox", "firefox.exe"),
    "spotify": ("spotify", "Spotify.exe"),
    "terminal": ("wt", "WindowsTerminal.exe"),
    "cmd": ("cmd", "cmd.exe"),
    "powershell": ("powershell", "powershell.exe"),
    "paint": ("mspaint", "mspaint.exe"),
    "word": ("winword", "WINWORD.EXE"),
    "excel": ("excel", "EXCEL.EXE"),
    "configuracion": ("ms-settings:", "SystemSettings.exe"),
    "configuración": ("ms-settings:", "SystemSettings.exe"),
    "ajustes": ("ms-settings:", "SystemSettings.exe"),
}

# Procesos que nunca se cierran: matarlos deja el equipo inutilizable.
_PROTECTED = {
    "lsass.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "smss.exe", "svchost.exe", "system", "registry", "dwm.exe",
}


class AppParams(BaseModel):
    """Parámetros de la tool de aplicaciones."""

    action: Literal["open", "close"] = Field(
        default="open", description="'open' para abrir una app, 'close' para cerrarla."
    )
    app: str = Field(
        description="Nombre de la aplicación, coloquial ('bloc de notas') o ejecutable."
    )


class AppLauncherTool(Tool):
    """Abre y cierra aplicaciones del sistema."""

    definition = ToolDefinition(
        name="app",
        description=(
            "Abre o cierra aplicaciones del ordenador (navegador, bloc de notas, "
            "Spotify, calculadora...). Usa action='open' para abrir y 'close' para cerrar."
        ),
        parameters=AppParams,
        permissions={Permission.WRITE_SYSTEM},
        audit=True,
        timeout_seconds=_TIMEOUT + 10.0,
    )

    @staticmethod
    def _resolve(app: str) -> tuple[str, str]:
        """Traduce el nombre a (objetivo para abrir, proceso para cerrar)."""
        key = app.strip().lower()
        if key in _APPS:
            return _APPS[key]
        # Desconocida: se intenta tal cual; Windows resuelve lo que esté registrado.
        target = app.strip()
        process = target if target.lower().endswith(".exe") else f"{target}.exe"
        return target, process

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, AppParams)
        if not params.app.strip():
            return ToolResult(success=False, error="no me has dicho qué aplicación")
        target, process = self._resolve(params.app)

        if params.action == "close":
            if process.lower() in _PROTECTED:
                return ToolResult(
                    success=False,
                    error=f"no puedo cerrar '{process}': es un proceso crítico de Windows",
                )
            return await self._run(
                ["taskkill", "/im", process],  # sin /f: cierre amable
                ok={"cerrada": params.app, "proceso": process},
                fail=f"no encontré '{params.app}' abierta",
            )

        # Abrir: `start` acepta ejecutables, URLs y protocolos (ms-settings:).
        return await self._run(
            ["cmd", "/c", "start", "", target],
            ok={"abierta": params.app},
            fail=f"no pude abrir '{params.app}'",
        )

    async def _run(self, command: list[str], *, ok: dict[str, Any], fail: str) -> ToolResult:
        _logger.info("app.command", extra={"command": command})
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(success=False, error="la operación tardó demasiado")
        except OSError as exc:
            return ToolResult(success=False, error=f"no se pudo ejecutar: {exc}")
        if proc.returncode:
            detail = raw_err.decode("utf-8", errors="replace").strip()
            return ToolResult(success=False, error=f"{fail} ({detail})" if detail else fail)
        return ToolResult(success=True, output=ok)
