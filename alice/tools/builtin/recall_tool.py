"""Herramienta ``recall_memory``: recupera lo que Alice sabe del usuario.

Lee de la memoria episódica los hechos que el usuario ha pedido guardar
(episodios ``user_fact``) y los devuelve como datos estructurados. El LLM los
narra después; esta tool NO produce prosa, solo hechos.

Es el complemento de la acción ``remember``: una guarda, esta recupera. Solo
requiere ``read_system`` (lee la propia memoria de Alice, sin efectos).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from alice.brain.memory import EpisodicMemory

# Tipo de episodio bajo el que la acción ``remember`` guarda los datos del usuario.
_USER_FACT_KIND = "user_fact"


class RecallParams(BaseModel):
    """Parámetros de la herramienta recall_memory."""

    # Cuántos hechos recuperar como máximo (los más recientes).
    limit: int = Field(default=20, ge=1, le=100)


class RecallMemoryTool(Tool):
    """Devuelve los hechos que el usuario ha pedido recordar."""

    definition = ToolDefinition(
        name="recall_memory",
        description="Recupera lo que Alice sabe del usuario (hechos guardados).",
        parameters=RecallParams,
        permissions={Permission.READ_SYSTEM},
    )

    def __init__(self, episodic: EpisodicMemory) -> None:
        self._episodic = episodic

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, RecallParams)
        items = self._episodic.recent_by_kind(_USER_FACT_KIND, limit=params.limit)
        facts = [
            text
            for item in items
            if (text := str(item.content.get("text", "")).strip())
        ]
        return ToolResult(
            success=True,
            output={"facts": facts, "count": len(facts)},
        )
