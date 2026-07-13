"""Herramienta ``camera``: consulta qué está viendo Alice ahora mismo.

Pregunta al plugin de visión (su endpoint ``/state``) por la última escena:
quién hay delante, si los reconoce y qué gesto hace. Devuelve datos
estructurados; el LLM los narra. Si la visión no está activa (sin cámara o
plugin caído), degrada con ``available=False`` en vez de fallar.

Es el puente que faltaba: la visión dejaba de ser solo *push* (eventos) para
poder consultarse *bajo demanda* cuando el usuario pregunta "¿me ves?".
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult


class CameraParams(BaseModel):
    """La tool camera no necesita parámetros."""


class CameraTool(Tool):
    """Devuelve la escena actual vista por la cámara (caras, identidad, gesto)."""

    definition = ToolDefinition(
        name="camera",
        description=(
            "Consulta qué ve la cámara AHORA: si hay alguien, a quién reconoce y "
            "qué gesto hace. Úsala cuando el usuario pregunte si le ves, quién está "
            "presente o qué está haciendo frente a la cámara."
        ),
        parameters=CameraParams,
        permissions={Permission.READ_SYSTEM},
    )

    def __init__(self, *, base_url: str = "http://127.0.0.1:8757", timeout: float = 3.0) -> None:
        self._url = base_url.rstrip("/") + "/state"
        self._timeout = timeout

    async def execute(self, params: BaseModel) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._url)
                response.raise_for_status()
                state: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError):
            # Visión apagada o sin responder: no es un error del sistema, Alice
            # simplemente no puede ver ahora.
            return ToolResult(success=True, output={"available": False})
        state["available"] = True
        return ToolResult(success=True, output=state)
