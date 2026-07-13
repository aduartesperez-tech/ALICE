"""Tests del catálogo de tools y del parseo de tool_calls del proveedor."""

from __future__ import annotations

from pydantic import BaseModel, Field

from alice.brain.llm_openai_compat import OpenAICompatProvider
from alice.brain.tool_catalog import build_tool_specs
from alice.tools.tool import Permission, ToolDefinition


class _Params(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


def test_build_tool_specs_from_definitions() -> None:
    definition = ToolDefinition(
        name="recall_memory",
        description="lo que sé de ti",
        parameters=_Params,
        permissions={Permission.READ_SYSTEM},
    )
    specs = build_tool_specs([definition])
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "recall_memory"
    assert spec.description == "lo que sé de ti"
    # El JSON Schema de Pydantic describe el parámetro limit.
    assert "limit" in spec.parameters["properties"]


def test_parse_tool_calls_valid_and_invalid() -> None:
    raw = [
        {"function": {"name": "datetime", "arguments": "{}"}},
        {"function": {"name": "recall_memory", "arguments": '{"limit": 3}'}},
        {"function": {"arguments": "{}"}},  # sin nombre: se ignora
        {"function": {"name": "camera", "arguments": "no-es-json"}},  # args rotos -> {}
    ]
    calls = OpenAICompatProvider._parse_tool_calls(raw)
    assert [c.name for c in calls] == ["datetime", "recall_memory", "camera"]
    assert calls[1].arguments == {"limit": 3}
    assert calls[2].arguments == {}  # args inválidos degradan a vacío
