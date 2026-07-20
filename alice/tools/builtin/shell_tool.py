"""ShellTool: ejecuta comandos del sistema, con tres niveles de riesgo.

A diferencia de ``ScriptTool`` (catálogo cerrado), aquí el comando es libre. Por
eso el control es de tres niveles:

1. **Denylist dura**: lo destructivo (formatear, borrado recursivo, apagar,
   tocar el registro, borrar copias de seguridad...) NO se ejecuta nunca, ni
   aunque el usuario lo autorice. No se pregunta siquiera.
2. **Allowlist de solo lectura**: un puñado de comandos inofensivos (``dir``,
   ``whoami``, ``ping``...) corren directamente, sin molestar al usuario.
3. **Todo lo demás**: pide confirmación humana antes de ejecutarse.

Detalle importante: si el comando encadena o redirige (``&&``, ``|``, ``>``...)
pierde el pase de la allowlist y pasa al nivel 3, porque ``dir && algo`` empieza
por ``dir`` pero puede hacer cualquier cosa.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from pydantic import BaseModel, Field

from alice.logging import get_logger
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult

_logger = get_logger("alice.tools.shell")

_MAX_OUTPUT = 2000
_DEFAULT_TIMEOUT = 30.0

# Nivel 1 — prohibido siempre. Se busca en TODO el comando, no solo al principio.
_DENY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bformat\b", re.IGNORECASE), "formatear discos"),
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "particionar discos"),
    (re.compile(r"\bmkfs\b", re.IGNORECASE), "formatear discos"),
    (re.compile(r"\bdd\s+if=", re.IGNORECASE), "escribir directo a disco"),
    (re.compile(r"\brm\s+-[a-z]*[rf]", re.IGNORECASE), "borrado recursivo"),
    (re.compile(r"\b(rmdir|rd)\b[^\n]*\s/s", re.IGNORECASE), "borrar carpetas recursivamente"),
    (re.compile(r"\bdel\b[^\n]*\s/[sq]", re.IGNORECASE), "borrado masivo de archivos"),
    (
        re.compile(r"\b(shutdown|restart-computer|stop-computer)\b", re.IGNORECASE),
        "apagar o reiniciar el equipo",
    ),
    (re.compile(r"\bregedit\b|\breg\s+delete\b", re.IGNORECASE), "modificar el registro"),
    (re.compile(r"\bvssadmin\b[^\n]*delete", re.IGNORECASE), "borrar copias de seguridad"),
    (re.compile(r"\bbcdedit\b", re.IGNORECASE), "modificar el arranque del sistema"),
    (re.compile(r"\bcipher\b[^\n]*\s/w", re.IGNORECASE), "borrado seguro de disco"),
    (re.compile(r"\bsc\s+delete\b", re.IGNORECASE), "borrar servicios del sistema"),
    (re.compile(r"\bnet\s+user\b[^\n]*\s/add", re.IGNORECASE), "crear usuarios"),
    (re.compile(r"\btakeown\b|\bicacls\b", re.IGNORECASE), "cambiar permisos del sistema"),
    (re.compile(r":\(\)\s*\{.*\}\s*;\s*:", re.DOTALL), "fork bomb"),
]

# Nivel 2 — solo lectura: corren sin preguntar.
_READ_ONLY = {
    "dir", "ls", "type", "cat", "echo", "whoami", "hostname", "ver", "date", "time",
    "systeminfo", "tasklist", "ipconfig", "ping", "tracert", "nslookup", "netstat",
    "where", "which", "findstr", "tree", "vol", "df", "free", "uptime", "pwd",
}
# Comandos que solo son de lectura según su subcomando.
_READ_ONLY_SUB: dict[str, set[str]] = {
    "git": {"status", "log", "diff", "branch", "show", "remote", "config"},
    "pip": {"list", "show", "freeze"},
    "docker": {"ps", "images", "logs"},
}

# Encadenar o redirigir anula el pase de la allowlist.
_CHAINING = re.compile(r"[&|;<>`]|\$\(")


class ShellParams(BaseModel):
    """Parámetros de la tool de shell."""

    command: str = Field(
        description="El comando a ejecutar, tal cual se escribiría en la terminal."
    )


class ShellTool(Tool):
    """Ejecuta comandos del sistema con denylist, allowlist y confirmación."""

    definition = ToolDefinition(
        name="shell",
        description=(
            "Ejecuta un comando en la terminal del sistema y devuelve su salida. "
            "Úsala para consultar el estado del equipo o realizar tareas que el "
            "usuario pida explícitamente. Los comandos destructivos están prohibidos "
            "y los que modifican algo piden confirmación al usuario."
        ),
        parameters=ShellParams,
        permissions={Permission.SHELL},
        audit=True,
        timeout_seconds=_DEFAULT_TIMEOUT + 15.0,
    )

    def __init__(self, *, timeout_seconds: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout_seconds

    # --- Clasificación de riesgo ---------------------------------------------

    @staticmethod
    def denied_reason(command: str) -> str | None:
        """Motivo por el que el comando está prohibido, o None si no lo está."""
        for pattern, reason in _DENY:
            if pattern.search(command):
                return reason
        return None

    @staticmethod
    def is_read_only(command: str) -> bool:
        """Si el comando es de la allowlist de solo lectura (y no encadena nada)."""
        if _CHAINING.search(command):
            return False  # `dir && algo` empieza por dir pero no es inofensivo
        tokens = command.strip().split()
        if not tokens:
            return False
        head = tokens[0].lower().removesuffix(".exe")
        if head in _READ_ONLY:
            return True
        allowed_subs = _READ_ONLY_SUB.get(head)
        if allowed_subs is None:
            return False
        return len(tokens) > 1 and tokens[1].lower() in allowed_subs

    def confirmation_question(self, params: BaseModel) -> str | None:
        assert isinstance(params, ShellParams)
        command = params.command
        if self.denied_reason(command) is not None:
            return None  # ni se pregunta: `execute` lo rechaza
        if self.is_read_only(command):
            return None  # inofensivo: no molestamos al usuario
        return f"Voy a ejecutar en la terminal: «{command}». ¿Lo hago? Dime sí o no."

    # --- Ejecución ------------------------------------------------------------

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, ShellParams)
        command = params.command.strip()
        if not command:
            return ToolResult(success=False, error="comando vacío")

        reason = self.denied_reason(command)
        if reason is not None:
            _logger.warning("shell.denied", extra={"command": command, "reason": reason})
            return ToolResult(
                success=False,
                error=f"me niego a ejecutar eso: implica {reason}. No está permitido.",
            )

        _logger.info("shell.running", extra={"command": command})
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolResult(success=False, error=f"no se pudo lanzar el comando: {exc}")
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                success=False, error=f"el comando superó {self._timeout:.0f}s y se detuvo"
            )
        code = proc.returncode or 0
        output: dict[str, Any] = {
            "command": command,
            "exit_code": code,
            "stdout": _clip(raw_out),
            "stderr": _clip(raw_err),
        }
        _logger.info("shell.finished", extra={"command": command, "exit_code": code})
        return ToolResult(
            success=code == 0,
            output=output,
            error=None if code == 0 else f"el comando terminó con código {code}",
        )


def _clip(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n... (recortado, {len(text)} caracteres en total)"
