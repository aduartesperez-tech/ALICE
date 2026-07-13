"""Construye el catálogo de tools para function calling.

Traduce las ``ToolDefinition`` (schema Pydantic + permisos) que ya declaran las
herramientas al formato ``ToolSpec`` (JSON Schema) que consume el proveedor. Así
el LLM ve exactamente las tools registradas, sin duplicar descripciones: la
fuente de verdad sigue siendo la definición de cada tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alice.brain.llm import ToolSpec

if TYPE_CHECKING:
    from alice.tools.tool import ToolDefinition


def build_tool_specs(definitions: list[ToolDefinition]) -> list[ToolSpec]:
    """Convierte definiciones de tools en specs de function calling."""
    return [
        ToolSpec(
            name=d.name,
            description=d.description,
            parameters=d.parameters.model_json_schema(),
        )
        for d in definitions
    ]
