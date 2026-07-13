"""Construye el contexto que el LLM entiende mejor.

En vez de pasarle prosa al modelo, le pasamos bloques etiquetados y
deterministas: objetivo, observaciones estructuradas e instrucción. Este es el
formato normalizado que consumirá ``call_llm`` cuando exista un LLM real; hoy el
``NullLLMProvider`` lo ignora, pero el contrato ya queda fijado.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alice.core.observation import Observation

_DEFAULT_INSTRUCTION = (
    "Responde al usuario en su mismo idioma, de forma natural y breve, "
    "usando solo los datos de OBSERVATIONS. No inventes datos que no estén."
)


def render_for_llm(
    *,
    user_text: str,
    goal: str,
    observations: list[Observation],
    instruction: str = _DEFAULT_INSTRUCTION,
) -> str:
    """Serializa el contexto de un plan en bloques etiquetados para el LLM."""
    if observations:
        obs_block = "\n".join(
            f"- {obs.kind} ({obs.source}): {json.dumps(obs.data, ensure_ascii=False, default=str)}"
            for obs in observations
        )
    else:
        obs_block = "(ninguna)"

    return (
        f"[USER]\n{user_text}\n\n"
        f"[GOAL]\n{goal}\n\n"
        f"[OBSERVATIONS]\n{obs_block}\n\n"
        f"[INSTRUCTION]\n{instruction}"
    )
