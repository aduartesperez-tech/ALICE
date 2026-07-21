"""SystemControlTool: volumen, bloqueo, capturas y energía. Riesgo medio.

Agrupa el control del equipo que no es abrir apps ni mover ventanas. Lo ordinario
(volumen, bloquear, captura) se hace sin molestar; lo irreversible en el momento
(apagar, reiniciar, suspender) **siempre pide confirmación**, porque un
malentendido del modelo no puede costarte el trabajo abierto.

El volumen usa ``pycaw`` (extra opcional ``[control]``). Si no está instalado, la
tool degrada con un mensaje claro en vez de reventar: Alice sigue viva.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from alice.logging import get_logger
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult

_logger = get_logger("alice.tools.system")

_TIMEOUT = 20.0

# Acciones que no tienen vuelta atrás inmediata: se confirman SIEMPRE.
_NEEDS_CONFIRMATION = {
    "shutdown": "apagar el equipo",
    "restart": "reiniciar el equipo",
    "sleep": "suspender el equipo",
}

_SCREENSHOT_PS = """
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$bmp.Save('{path}')
$g.Dispose(); $bmp.Dispose()
"""


class SystemParams(BaseModel):
    """Parámetros del control del sistema."""

    action: Literal[
        "volume_get", "volume_set", "mute", "unmute",
        "lock", "screenshot", "sleep", "shutdown", "restart",
    ] = Field(description="Qué hacer con el equipo.")
    level: int | None = Field(
        default=None, description="Volumen de 0 a 100. Solo con action='volume_set'."
    )


class SystemControlTool(Tool):
    """Volumen, silencio, bloqueo, capturas y energía del equipo."""

    definition = ToolDefinition(
        name="system_control",
        description=(
            "Controla el equipo: subir/bajar volumen, silenciar, bloquear la pantalla, "
            "hacer una captura, y apagar/reiniciar/suspender (esto último pide "
            "confirmación al usuario)."
        ),
        parameters=SystemParams,
        permissions={Permission.WRITE_SYSTEM},
        audit=True,
        timeout_seconds=_TIMEOUT + 10.0,
    )

    def __init__(self, screenshot_dir: Path | None = None) -> None:
        self._shots = (screenshot_dir or Path("data/screenshots")).resolve()

    def confirmation_question(self, params: BaseModel) -> str | None:
        assert isinstance(params, SystemParams)
        what = _NEEDS_CONFIRMATION.get(params.action)
        if what is None:
            return None
        return f"Voy a {what}. ¿Seguro? Dime sí o no."

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, SystemParams)
        action = params.action
        if action == "volume_get":
            return self._volume_get()
        if action == "volume_set":
            return self._volume_set(params.level)
        if action in ("mute", "unmute"):
            return self._set_mute(action == "mute")
        if action == "lock":
            return await self._shell("rundll32.exe user32.dll,LockWorkStation", {"bloqueado": True})
        if action == "screenshot":
            return await self._screenshot()
        if action == "sleep":
            return await self._shell(
                "rundll32.exe powrprof.dll,SetSuspendState 0,1,0", {"suspendiendo": True}
            )
        if action == "shutdown":
            return await self._shell("shutdown /s /t 0", {"apagando": True})
        return await self._shell("shutdown /r /t 0", {"reiniciando": True})

    # --- Volumen (pycaw) ------------------------------------------------------

    @staticmethod
    def _endpoint() -> Any:
        """Interfaz de volumen del dispositivo por defecto, o None si falta pycaw.

        OJO: la API de pycaw cambió; ``GetSpeakers()`` devuelve un ``AudioDevice``
        que ya expone ``EndpointVolume`` (el ``Activate(...)`` de los ejemplos
        antiguos que circulan por internet ya no funciona).
        """
        try:
            from pycaw.utils import AudioUtilities
        except ImportError:
            return None
        return AudioUtilities.GetSpeakers().EndpointVolume

    def _volume_get(self) -> ToolResult:
        endpoint = self._endpoint()
        if endpoint is None:
            return self._no_pycaw()
        return ToolResult(
            success=True,
            output={
                "volumen": round(endpoint.GetMasterVolumeLevelScalar() * 100),
                "silenciado": bool(endpoint.GetMute()),
            },
        )

    def _volume_set(self, level: int | None) -> ToolResult:
        if level is None:
            return ToolResult(success=False, error="dime a qué nivel (0-100)")
        endpoint = self._endpoint()
        if endpoint is None:
            return self._no_pycaw()
        clamped = max(0, min(100, level))
        endpoint.SetMasterVolumeLevelScalar(clamped / 100.0, None)
        _logger.info("system.volume_set", extra={"level": clamped})
        return ToolResult(success=True, output={"volumen": clamped})

    def _set_mute(self, mute: bool) -> ToolResult:
        endpoint = self._endpoint()
        if endpoint is None:
            return self._no_pycaw()
        endpoint.SetMute(1 if mute else 0, None)
        return ToolResult(success=True, output={"silenciado": mute})

    @staticmethod
    def _no_pycaw() -> ToolResult:
        return ToolResult(
            success=False,
            error="no puedo controlar el volumen: falta pycaw (pip install -e \".[control]\")",
        )

    # --- Captura y comandos ---------------------------------------------------

    async def _screenshot(self) -> ToolResult:
        self._shots.mkdir(parents=True, exist_ok=True)
        path = self._shots / f"captura_{datetime.now():%Y%m%d_%H%M%S}.png"
        result = await self._shell(
            _SCREENSHOT_PS.format(path=str(path).replace("\\", "\\\\")),
            {"captura": str(path)},
            powershell=True,
        )
        if result.success and not path.is_file():
            return ToolResult(success=False, error="la captura no se guardó")
        return result

    async def _shell(
        self, command: str, ok: dict[str, Any], *, powershell: bool = False
    ) -> ToolResult:
        argv = (
            ["powershell", "-NoProfile", "-Command", command]
            if powershell
            else ["cmd", "/c", command]
        )
        _logger.info("system.command", extra={"powershell": powershell})
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(success=False, error="la operación tardó demasiado")
        except OSError as exc:
            return ToolResult(success=False, error=f"no se pudo ejecutar: {exc}")
        if proc.returncode:
            detail = raw_err.decode("utf-8", errors="replace").strip()[:300]
            return ToolResult(success=False, error=detail or "el comando falló")
        return ToolResult(success=True, output=ok)
