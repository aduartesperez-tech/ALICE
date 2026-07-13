"""Observación: unidad de dato estructurado que fluye hacia el usuario.

Es vocabulario de IPC (como ``Event``), no lógica de negocio: por eso vive en
``core``. Las herramientas y acciones NUNCA producen prosa para humanos;
producen ``Observation`` estructuradas. Un LLM (en el futuro) las narra; hasta
entonces se renderizan en crudo con ``render_observations_plain``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Observation(BaseModel):
    """Un dato estructurado producido por un paso de un plan."""

    source: str  # p.ej. "tool:datetime", "reminder", "error"
    kind: str  # p.ej. "datetime", "search_result", "error"
    data: dict[str, Any] = Field(default_factory=dict)


def render_observations_plain(observations: list[Observation]) -> str:
    """Renderizado legible en crudo (sin IA), para el canal de salida de v1.1.

    Es un placeholder honesto hasta que el LLM narre: muestra los datos tal
    cual, etiquetados por su tipo.
    """
    if not observations:
        return "(sin datos)"
    lines: list[str] = []
    for obs in observations:
        pairs = ", ".join(f"{k}={v}" for k, v in obs.data.items())
        lines.append(f"[{obs.kind}] {pairs}" if pairs else f"[{obs.kind}]")
    return "\n".join(lines)
