"""Herramienta ``datetime``: devuelve la fecha y hora actuales.

Única herramienta real e inofensiva de v1. Sirve para validar el pipeline
completo (planner → tool_manager → resultado) sin ningún efecto secundario.
Solo requiere el permiso ``read_system``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult


class DateTimeParams(BaseModel):
    """Parámetros de la herramienta datetime."""

    # Zona no soportada aún; se acepta pero se ignora (solo UTC en v1).
    timezone: str | None = None


class DateTimeTool(Tool):
    """Devuelve la fecha/hora actual en ISO 8601 (UTC)."""

    definition = ToolDefinition(
        name="datetime",
        description="Devuelve la fecha y hora actuales.",
        parameters=DateTimeParams,
        permissions={Permission.READ_SYSTEM},
    )

    async def execute(self, params: BaseModel) -> ToolResult:
        now = datetime.now(UTC)
        return ToolResult(
            success=True,
            output={
                "iso": now.isoformat(),
                "date": now.date().isoformat(),
                "time": now.time().isoformat(timespec="seconds"),
            },
        )
