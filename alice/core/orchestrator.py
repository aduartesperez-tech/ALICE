"""Orchestrator: composición y supervisión del sistema.

Su ÚNICA función es componer las piezas (bus, scheduler, estado, módulos
internos y plugins), arrancarlas y detenerlas en orden. NO contiene lógica de
negocio ni toma decisiones inteligentes: eso vive en el Planner y los módulos.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from alice.core.event_bus import EventBus
from alice.core.plugin_manager import PluginManager
from alice.core.scheduler import Scheduler
from alice.core.state import StateManager, SystemState
from alice.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from alice.config import AliceSettings

_logger = get_logger("alice.core.orchestrator")


@runtime_checkable
class CoreModule(Protocol):
    """Módulo interno con ciclo de vida (Planner, ToolManager, LLMModule...).

    Cada módulo se suscribe al bus en su ``start`` y se limpia en su ``stop``.
    """

    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class Orchestrator:
    """Punto de composición y supervisión del sistema."""

    def __init__(self, settings: AliceSettings, modules: Sequence[CoreModule] = ()) -> None:
        self._settings = settings
        self.bus = EventBus(max_queue_size=settings.event_bus.max_queue_size)
        self.scheduler = Scheduler()
        self.state = StateManager(self.bus)
        self.plugins = PluginManager(
            bus=self.bus,
            scheduler=self.scheduler,
            plugins_dir=settings.plugins.plugins_dir,
        )
        self._modules = list(modules)
        self._consumer: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def startup(self) -> None:
        """Arranca todo en orden: bus → scheduler → módulos → plugins → RUNNING."""
        self.state.set_starting()
        self._consumer = self.bus.start_consumer()
        self.scheduler.start()

        for module in self._modules:
            await module.start()
            _logger.info("orchestrator.module_started", extra={"module_name": module.name})

        self.plugins.discover_and_load()
        await self.plugins.initialize_all()
        await self.plugins.start_all()

        await self.state.set_running()
        _logger.info("orchestrator.started", extra={"plugins": self.plugins.loaded_names})

    async def shutdown(self) -> None:
        """Apaga todo en orden inverso: plugins → módulos → estado → scheduler → bus."""
        if self.state.state in (SystemState.STOPPING, SystemState.STOPPED):
            return
        self.state.set_stopping()

        await self.plugins.stop_all()
        for module in reversed(self._modules):
            await module.stop()
            _logger.info("orchestrator.module_stopped", extra={"module_name": module.name})

        await self.state.set_stopped()
        await self.scheduler.stop()

        await self.bus.stop()
        if self._consumer is not None:
            try:
                await asyncio.wait_for(self._consumer, timeout=5.0)
            except TimeoutError:
                self._consumer.cancel()
        _logger.info("orchestrator.stopped")

    async def run_forever(self) -> None:
        """Arranca y bloquea hasta que se solicite el apagado (o llega una señal).

        El ``finally`` garantiza el apagado ordenado también si el ``wait`` se
        interrumpe con KeyboardInterrupt (Ctrl+C) o se cancela la tarea.
        """
        await self.startup()
        try:
            await self._stop_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        """Señaliza el apagado ordenado (llamado por el manejador de señales)."""
        self._stop_event.set()
