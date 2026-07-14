"""Tests del Orchestrator: arranque, emisión de eventos de sistema y apagado."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alice.config import AliceSettings
from alice.core.events import COMMAND_RECEIVED, SYSTEM_STARTED, SYSTEM_STOPPED, Event
from alice.core.orchestrator import Orchestrator
from alice.core.state import SystemState

_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _settings() -> AliceSettings:
    s = AliceSettings()
    s.plugins.plugins_dir = _PLUGINS_DIR
    # Solo plugins ligeros: sin webcam ni Whisper, que colgarían el test.
    s.plugins.enabled = ["echo", "console"]
    return s


async def test_startup_and_shutdown_emit_system_events() -> None:
    orch = Orchestrator(_settings())
    seen: list[str] = []
    orch.bus.subscribe(SYSTEM_STARTED, lambda e: _record(seen, e))
    orch.bus.subscribe(SYSTEM_STOPPED, lambda e: _record(seen, e))

    await orch.startup()
    assert orch.state.state is SystemState.RUNNING
    await asyncio.sleep(0.05)  # deja fluir system.started

    await orch.shutdown()
    assert orch.state.state is SystemState.STOPPED
    assert SYSTEM_STARTED in seen
    assert SYSTEM_STOPPED in seen


async def test_echo_plugin_loaded_and_responds() -> None:
    orch = Orchestrator(_settings())
    replies: list[Event] = []
    orch.bus.subscribe("echo.replied", lambda e: _collect(replies, e))

    await orch.startup()
    assert "echo" in orch.plugins.loaded_names

    await orch.bus.publish(
        Event(type=COMMAND_RECEIVED, source="test", payload={"text": "hola alice"})
    )
    await orch.bus._queue.join()  # noqa: SLF001
    await orch.shutdown()

    assert len(replies) == 1
    assert replies[0].payload["text"] == "hola alice"


async def test_run_forever_stops_on_request() -> None:
    orch = Orchestrator(_settings())
    stopped: list[str] = []
    orch.bus.subscribe(SYSTEM_STOPPED, lambda e: _record(stopped, e))

    run_task = asyncio.create_task(orch.run_forever())
    # Espera a que el sistema llegue a RUNNING.
    for _ in range(50):
        if orch.state.state is SystemState.RUNNING:
            break
        await asyncio.sleep(0.01)
    assert orch.state.state is SystemState.RUNNING

    orch.request_stop()  # equivalente a lo que hace el manejador de señales
    await asyncio.wait_for(run_task, timeout=5.0)

    assert orch.state.state is SystemState.STOPPED
    assert SYSTEM_STOPPED in stopped


async def test_double_shutdown_is_safe() -> None:
    orch = Orchestrator(_settings())
    await orch.startup()
    await orch.shutdown()
    await orch.shutdown()  # no debe lanzar
    assert orch.state.state is SystemState.STOPPED


async def _record(sink: list[str], event: Event) -> None:
    sink.append(event.type)


async def _collect(sink: list[Event], event: Event) -> None:
    sink.append(event)
