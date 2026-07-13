"""Configuración tipada del sistema (pydantic-settings).

Se carga desde ``config/alice.toml`` y admite override por variables de entorno
con prefijo ``ALICE_``. Reemplazable sin tocar código.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EventBusSettings(BaseModel):
    """Parámetros del EventBus."""

    max_queue_size: int = 1000


class LoggingSettings(BaseModel):
    """Parámetros de logging estructurado."""

    level: str = "INFO"
    log_dir: Path = Path("logs")
    file_name: str = "alice.log"
    to_console: bool = True


class PluginSettings(BaseModel):
    """Parámetros de descubrimiento de plugins."""

    plugins_dir: Path = Path("plugins")


class ToolSettings(BaseModel):
    """Parámetros del sistema de herramientas."""

    default_timeout_seconds: float = 10.0
    # Permisos concedidos globalmente; una tool que requiera un permiso fuera
    # de este conjunto será rechazada por el ToolManager.
    granted_permissions: set[str] = Field(default_factory=lambda: {"read_system"})


DEFAULT_SYSTEM_PROMPT = (
    "Eres Alice, una asistente local. Respondes de forma breve, natural y en el "
    "mismo idioma del usuario. Usa únicamente los datos que se te dan en "
    "OBSERVATIONS; nunca inventes información que no esté ahí."
)


class LLMSettings(BaseModel):
    """Parámetros del proveedor de LLM."""

    provider: str = "null"  # "openai_compat" | "null"
    base_url: str = "http://localhost:1234/v1"  # LM Studio; Ollama: :11434/v1
    model: str = "local-model"
    temperature: float = 0.7
    max_tokens: int = 512
    timeout_seconds: float = 60.0
    narrate: bool = False  # si True, los planes con tool pasan por el LLM para narrarse
    history_turns: int = 6
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


class MemorySettings(BaseModel):
    """Parámetros de la memoria persistente."""

    db_path: Path = Path("data/alice.db")
    short_term_max_items: int = 200


class PlannerSettings(BaseModel):
    """Parámetros del planner.

    ``strategy = "rules"``: solo regex (sin LLM, determinista).
    ``strategy = "hybrid"``: regex primero; si ninguna regla aplica, el LLM
    clasifica la intención en un catálogo cerrado. Requiere proveedor real.
    ``strategy = "tool_calling"``: regex primero; si ninguna aplica, el LLM elige
    tools del catálogo real (function calling). Añadir una tool la hace usable
    sin tocar el planner. Requiere proveedor real y modelo con function calling.
    """

    strategy: str = "rules"  # "rules" | "hybrid" | "tool_calling"
    intent_timeout_seconds: float = 20.0


class AliceSettings(BaseSettings):
    """Configuración raíz del sistema."""

    model_config = SettingsConfigDict(
        env_prefix="ALICE_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    event_bus: EventBusSettings = Field(default_factory=EventBusSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    planner: PlannerSettings = Field(default_factory=PlannerSettings)

    @model_validator(mode="after")
    def _coerce_narrate(self) -> AliceSettings:
        # Narrar o clasificar con el proveedor nulo no tiene sentido: se apagan solos.
        if self.llm.provider == "null":
            self.llm.narrate = False
            self.planner.strategy = "rules"
        return self


def load_settings(config_path: Path | None = None) -> AliceSettings:
    """Carga la configuración desde un TOML (si existe) y aplica overrides de entorno."""
    data: dict[str, Any] = {}
    path = config_path or Path("config/alice.toml")
    if path.is_file():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    return AliceSettings(**data)
