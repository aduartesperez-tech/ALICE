"""Contratos del sistema de herramientas.

Una ``Tool`` declara su schema de parámetros (un modelo Pydantic) y los permisos
que requiere. Nadie ejecuta una tool directamente: el ToolManager valida y
ejecuta. Ni el planner ni (en el futuro) el LLM tocan ``execute``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Permission(StrEnum):
    """Permisos que una herramienta puede requerir."""

    READ_SYSTEM = "read_system"
    WRITE_SYSTEM = "write_system"
    NETWORK = "network"
    SHELL = "shell"
    FILESYSTEM = "filesystem"


class ToolDefinition(BaseModel):
    """Metadatos de una herramienta."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    parameters: type[BaseModel]
    permissions: set[Permission] = Field(default_factory=set)


class ToolResult(BaseModel):
    """Resultado de ejecutar una herramienta."""

    success: bool
    output: dict[str, Any] | None = None
    error: str | None = None


class Tool(ABC):
    """Interfaz común de toda herramienta."""

    definition: ToolDefinition

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult:
        """Ejecuta la herramienta con parámetros ya validados contra el schema."""
