"""Tests del PluginManager: descubrimiento, ciclo de vida y aislamiento de fallos."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

from alice.core.event_bus import EventBus
from alice.core.events import COMMAND_RECEIVED, Event
from alice.core.plugin_manager import PluginManager
from alice.core.scheduler import Scheduler

_GOOD_PLUGIN = '''
from __future__ import annotations
from alice.core.events import Event, EventPriority
from alice.core.plugin import Plugin, PluginManifest

class AlicePlugin(Plugin):
    manifest = PluginManifest(name="good", subscribes=["command.received"])

    async def initialize(self, ctx):
        self._ctx = ctx
        self.events = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def handle_event(self, event):
        self.events.append(event)
        await self._ctx.publish(
            Event(type="good.replied", source="plugin.good", priority=EventPriority.LOW)
        )
'''

_BROKEN_PLUGIN = '''
raise RuntimeError("este plugin explota al importar")
'''


def _make_plugin(root: Path, name: str, code: str, subscribes: list[str] | None = None) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    subs = ", ".join(f'"{s}"' for s in (subscribes or []))
    (folder / "manifest.toml").write_text(
        textwrap.dedent(f'''
        [plugin]
        name = "{name}"
        version = "1.0.0"
        subscribes = [{subs}]
        '''),
        encoding="utf-8",
    )
    (folder / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")


async def test_discovers_and_runs_plugin(tmp_path: Path) -> None:
    _make_plugin(tmp_path, "good", _GOOD_PLUGIN, subscribes=[COMMAND_RECEIVED])
    bus = EventBus(max_queue_size=50)
    sched = Scheduler()
    sched.start()
    mgr = PluginManager(bus=bus, scheduler=sched, plugins_dir=tmp_path)

    mgr.discover_and_load()
    assert mgr.loaded_names == ["good"]

    await mgr.initialize_all()
    await mgr.start_all()

    replies: list[Event] = []
    bus.subscribe("good.replied", lambda e: _collect(replies, e))

    task = bus.start_consumer()
    await bus.publish(Event(type=COMMAND_RECEIVED, source="test", payload={"text": "hola"}))
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await mgr.stop_all()
    await sched.stop()

    assert len(replies) == 1


async def test_broken_plugin_does_not_block_others(tmp_path: Path) -> None:
    _make_plugin(tmp_path, "broken", _BROKEN_PLUGIN)
    _make_plugin(tmp_path, "good", _GOOD_PLUGIN, subscribes=[COMMAND_RECEIVED])
    bus = EventBus(max_queue_size=50)
    sched = Scheduler()
    sched.start()
    mgr = PluginManager(bus=bus, scheduler=sched, plugins_dir=tmp_path)

    mgr.discover_and_load()

    # El plugin roto se descarta; el bueno sigue disponible.
    assert mgr.loaded_names == ["good"]
    await sched.stop()


async def test_empty_plugins_dir(tmp_path: Path) -> None:
    bus = EventBus()
    sched = Scheduler()
    mgr = PluginManager(bus=bus, scheduler=sched, plugins_dir=tmp_path / "nonexistent")
    mgr.discover_and_load()
    assert mgr.loaded_names == []


async def _collect(sink: list[Event], event: Event) -> None:
    sink.append(event)
