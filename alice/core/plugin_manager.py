"""Descubrimiento y ciclo de vida de plugins.

Escanea ``plugins/*/``; cada carpeta con ``manifest.toml`` + ``plugin.py`` que
expone una clase ``AlicePlugin(Plugin)`` se carga con importlib. El manager
suscribe el plugin a los eventos declarados en su manifest y gestiona su ciclo
de vida. Un plugin que falla al cargar o arrancar se loguea y NO impide el
arranque del resto.
"""

from __future__ import annotations

import importlib.util
import tomllib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alice.core.plugin import Plugin, PluginContext, PluginManifest
from alice.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from alice.core.event_bus import EventBus, Subscription
    from alice.core.events import Event
    from alice.core.scheduler import Scheduler

_logger = get_logger("alice.core.plugin_manager")

PLUGIN_CLASS_NAME = "AlicePlugin"
MANIFEST_FILE = "manifest.toml"
PLUGIN_FILE = "plugin.py"


@dataclass
class _LoadedPlugin:
    instance: Plugin
    subscriptions: list[Subscription] = field(default_factory=list)


class PluginManager:
    """Descubre, inicializa, arranca y detiene plugins."""

    def __init__(
        self,
        *,
        bus: EventBus,
        scheduler: Scheduler,
        plugins_dir: Path,
        enabled: list[str] | None = None,
        disabled: list[str] | None = None,
    ) -> None:
        self._bus = bus
        self._scheduler = scheduler
        self._plugins_dir = plugins_dir
        # Allowlist (si no-vacía, solo estos) y denylist (nunca estos).
        self._enabled = set(enabled) if enabled else None
        self._disabled = set(disabled or ())
        self._loaded: list[_LoadedPlugin] = []

    @property
    def loaded_names(self) -> list[str]:
        """Nombres de los plugins cargados con éxito."""
        return [lp.instance.manifest.name for lp in self._loaded]

    def discover_and_load(self) -> None:
        """Escanea el directorio de plugins y carga cada uno de forma aislada."""
        if not self._plugins_dir.is_dir():
            _logger.warning("plugin_manager.no_plugins_dir", extra={"dir": str(self._plugins_dir)})
            return
        for entry in sorted(self._plugins_dir.iterdir()):
            if entry.is_dir() and (entry / MANIFEST_FILE).is_file():
                self._load_one(entry)

    def _load_one(self, folder: Path) -> None:
        """Carga un único plugin; captura y loguea cualquier fallo sin propagarlo."""
        try:
            manifest = self._read_manifest(folder / MANIFEST_FILE)
            # El chequeo de activación va ANTES de importar: así un plugin
            # desactivado (p.ej. visión) no arrastra sus dependencias pesadas.
            if not self._is_enabled(manifest):
                _logger.info("plugin_manager.skipped", extra={"plugin": manifest.name})
                return
            instance = self._import_plugin(folder, manifest)
            self._loaded.append(_LoadedPlugin(instance=instance))
            _logger.info("plugin_manager.loaded", extra={"plugin": manifest.name})
        except Exception:  # noqa: BLE001 - un plugin roto no debe frenar a los demás
            _logger.exception("plugin_manager.load_failed", extra={"folder": str(folder)})

    def _is_enabled(self, manifest: PluginManifest) -> bool:
        """Decide si un plugin debe cargarse: manifest + allowlist + denylist."""
        if not manifest.enabled:
            return False
        if self._enabled is not None and manifest.name not in self._enabled:
            return False
        return manifest.name not in self._disabled

    @staticmethod
    def _read_manifest(path: Path) -> PluginManifest:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return PluginManifest.model_validate(data.get("plugin", data))

    @staticmethod
    def _import_plugin(folder: Path, manifest: PluginManifest) -> Plugin:
        module_name = f"alice_plugin_{manifest.name}"
        spec = importlib.util.spec_from_file_location(module_name, folder / PLUGIN_FILE)
        if spec is None or spec.loader is None:
            raise ImportError(f"No se pudo cargar el módulo del plugin en {folder}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin_cls = getattr(module, PLUGIN_CLASS_NAME, None)
        if plugin_cls is None or not issubclass(plugin_cls, Plugin):
            raise TypeError(f"{folder} no expone una clase {PLUGIN_CLASS_NAME}(Plugin)")
        instance: Plugin = plugin_cls()
        instance.manifest = manifest  # el manager es la fuente de verdad del manifest
        return instance

    async def initialize_all(self) -> None:
        """Inicializa cada plugin con su contexto y lo suscribe a sus eventos."""
        for lp in list(self._loaded):
            manifest = lp.instance.manifest
            ctx = PluginContext(
                publish=self._bus.publish,
                config={},
                logger=get_logger(f"alice.plugin.{manifest.name}"),
                scheduler=self._scheduler,
            )
            try:
                await lp.instance.initialize(ctx)
                self._subscribe(lp)
            except Exception:  # noqa: BLE001
                _logger.exception("plugin_manager.init_failed", extra={"plugin": manifest.name})
                self._loaded.remove(lp)

    def _subscribe(self, lp: _LoadedPlugin) -> None:
        for event_type in lp.instance.manifest.subscribes:
            sub = self._bus.subscribe(event_type, self._make_handler(lp.instance))
            lp.subscriptions.append(sub)

    @staticmethod
    def _make_handler(plugin: Plugin) -> _Handler:
        async def handler(event: Event) -> None:
            await plugin.handle_event(event)

        return handler

    async def start_all(self) -> None:
        """Arranca todos los plugins inicializados."""
        for lp in list(self._loaded):
            try:
                await lp.instance.start()
            except Exception:  # noqa: BLE001
                name = lp.instance.manifest.name
                _logger.exception("plugin_manager.start_failed", extra={"plugin": name})

    async def stop_all(self) -> None:
        """Detiene todos los plugins y limpia sus suscripciones."""
        for lp in reversed(self._loaded):
            for sub in lp.subscriptions:
                self._bus.unsubscribe(sub)
            try:
                await lp.instance.stop()
            except Exception:  # noqa: BLE001
                name = lp.instance.manifest.name
                _logger.exception("plugin_manager.stop_failed", extra={"plugin": name})
        self._loaded.clear()


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    _Handler = Callable[[Event], Awaitable[None]]
