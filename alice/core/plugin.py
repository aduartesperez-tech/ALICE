"""Contratos del sistema de plugins.

Un plugin es la unidad de extensión del sistema. Solo conoce el ``PluginContext``
que recibe en ``initialize``; nunca importa otro plugin ni toca el bus
directamente (publica a través del contexto). El ``PluginManager`` es quien lo
suscribe a los eventos declarados en su manifest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from logging import Logger

    from alice.core.events import Event
    from alice.core.scheduler import Scheduler


class PluginManifest(BaseModel):
    """Metadatos declarativos de un plugin (leídos de ``manifest.toml``)."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    # Tipos de evento a los que el manager suscribirá el plugin.
    subscribes: list[str] = Field(default_factory=list)
    # Si es False, el plugin no se carga (útil para desactivar hardware pesado
    # sin borrar la carpeta). La allowlist/denylist de config manda por encima.
    enabled: bool = True


class PluginContext:
    """Única superficie del sistema visible para un plugin.

    Le da lo justo: publicar eventos, leer su configuración, loguear y programar
    tareas. No expone el bus, ni otros plugins, ni el orquestador.
    """

    def __init__(
        self,
        *,
        publish: PublishFn,
        config: dict[str, Any],
        logger: Logger,
        scheduler: Scheduler,
    ) -> None:
        self._publish = publish
        self._config = config
        self._logger = logger
        self._scheduler = scheduler

    async def publish(self, event: Event) -> None:
        """Publica un evento en el bus."""
        await self._publish(event)

    def get_config(self, key: str, default: Any = None) -> Any:
        """Devuelve un valor de la configuración del plugin."""
        return self._config.get(key, default)

    def get_logger(self) -> Logger:
        """Devuelve el logger del plugin."""
        return self._logger

    def scheduler(self) -> Scheduler:
        """Devuelve el scheduler del sistema para programar tareas."""
        return self._scheduler


class Plugin(ABC):
    """Interfaz común que todo plugin debe implementar."""

    manifest: PluginManifest

    @abstractmethod
    async def initialize(self, ctx: PluginContext) -> None:
        """Recibe el contexto y prepara recursos. Se llama una sola vez."""

    @abstractmethod
    async def start(self) -> None:
        """Arranca la actividad del plugin."""

    @abstractmethod
    async def stop(self) -> None:
        """Libera recursos y detiene el plugin."""

    @abstractmethod
    async def handle_event(self, event: Event) -> None:
        """Procesa un evento al que el plugin está suscrito."""


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    PublishFn = Callable[[Event], Awaitable[None]]
