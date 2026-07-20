"""Tests del ToolManager: éxito, tool desconocida, permiso denegado, params y timeout."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from alice.core.event_bus import EventBus
from alice.core.events import TOOL_FINISHED, TOOL_REQUESTED, Event
from alice.core.payloads import ToolFinishedPayload
from alice.tools.builtin.datetime_tool import DateTimeTool
from alice.tools.manager import ToolManager
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult


class _SlowParams(BaseModel):
    pass


class _SlowTool(Tool):
    definition = ToolDefinition(
        name="slow",
        description="Tarda demasiado.",
        parameters=_SlowParams,
        permissions={Permission.READ_SYSTEM},
    )

    async def execute(self, params: BaseModel) -> ToolResult:
        await asyncio.sleep(5)
        return ToolResult(success=True)


class _NetParams(BaseModel):
    pass


class _NetTool(Tool):
    definition = ToolDefinition(
        name="net",
        description="Requiere red.",
        parameters=_NetParams,
        permissions={Permission.NETWORK},
    )

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(success=True)


async def _request_and_get_result(
    manager: ToolManager, bus: EventBus, payload: dict[str, object]
) -> ToolFinishedPayload:
    results: list[Event] = []
    bus.subscribe(TOOL_FINISHED, lambda e: _collect(results, e))
    await manager.start()
    task = bus.start_consumer()
    await bus.publish(Event(type=TOOL_REQUESTED, source="test", payload=payload))
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await manager.stop()
    assert len(results) == 1
    return ToolFinishedPayload.model_validate(results[0].payload)


async def test_datetime_tool_success() -> None:
    bus = EventBus(max_queue_size=50)
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"})
    mgr.register(DateTimeTool())
    result = await _request_and_get_result(mgr, bus, {"tool_name": "datetime", "params": {}})
    assert result.success is True
    assert result.output is not None and "iso" in result.output


async def test_unknown_tool_returns_error() -> None:
    bus = EventBus(max_queue_size=50)
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"})
    result = await _request_and_get_result(mgr, bus, {"tool_name": "ghost", "params": {}})
    assert result.success is False
    assert result.error is not None and "desconocida" in result.error


async def test_permission_denied() -> None:
    bus = EventBus(max_queue_size=50)
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"})  # sin NETWORK
    mgr.register(_NetTool())
    result = await _request_and_get_result(mgr, bus, {"tool_name": "net", "params": {}})
    assert result.success is False
    assert result.error is not None and "permiso denegado" in result.error


async def test_invalid_params() -> None:
    bus = EventBus(max_queue_size=50)
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"})
    mgr.register(DateTimeTool())
    # timezone debe ser str|None; un int es inválido.
    result = await _request_and_get_result(
        mgr, bus, {"tool_name": "datetime", "params": {"timezone": 123}}
    )
    assert result.success is False
    assert result.error is not None and "inválidos" in result.error


async def test_timeout() -> None:
    bus = EventBus(max_queue_size=50)
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"}, default_timeout=0.05)
    mgr.register(_SlowTool())
    result = await _request_and_get_result(mgr, bus, {"tool_name": "slow", "params": {}})
    assert result.success is False
    assert result.error is not None and "timeout" in result.error


class _DangerParams(BaseModel):
    pass


class _DangerTool(Tool):
    """Tool que siempre pide confirmación y deja registro."""

    definition = ToolDefinition(
        name="peligro",
        description="Muta el sistema.",
        parameters=_DangerParams,
        permissions={Permission.READ_SYSTEM},
        audit=True,
    )

    def __init__(self) -> None:
        self.ran = False

    def confirmation_question(self, params: BaseModel) -> str | None:
        return "¿Lo hago?"

    async def execute(self, params: BaseModel) -> ToolResult:
        self.ran = True
        return ToolResult(success=True, output={"hecho": True})


class _Confirmer:
    def __init__(self, answer: bool) -> None:
        self._answer = answer
        self.questions: list[str] = []

    async def confirm(self, question: str, *, detail: str = "") -> bool:
        self.questions.append(question)
        return self._answer


async def test_confirmation_granted_runs_the_tool() -> None:
    bus = EventBus(max_queue_size=50)
    confirmer = _Confirmer(True)
    tool = _DangerTool()
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"}, confirmer=confirmer)
    mgr.register(tool)
    result = await _request_and_get_result(mgr, bus, {"tool_name": "peligro", "params": {}})
    assert result.success is True
    assert tool.ran is True
    assert confirmer.questions == ["¿Lo hago?"]


async def test_confirmation_denied_blocks_the_tool() -> None:
    bus = EventBus(max_queue_size=50)
    tool = _DangerTool()
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"}, confirmer=_Confirmer(False))
    mgr.register(tool)
    result = await _request_and_get_result(mgr, bus, {"tool_name": "peligro", "params": {}})
    assert result.success is False
    assert result.error is not None and "cancelado" in result.error
    assert tool.ran is False  # no se ejecutó nada


async def test_no_confirmer_is_fail_closed() -> None:
    # Sin canal para preguntar, una tool que exige confirmación NO se ejecuta.
    bus = EventBus(max_queue_size=50)
    tool = _DangerTool()
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"})  # sin confirmer
    mgr.register(tool)
    result = await _request_and_get_result(mgr, bus, {"tool_name": "peligro", "params": {}})
    assert result.success is False
    assert tool.ran is False


async def test_audited_tool_leaves_a_record() -> None:
    # Una tool con audit=True publica un episodio "execution" en la memoria.
    from alice.core.events import MEMORY_STORE_REQUESTED

    bus = EventBus(max_queue_size=50)
    stored: list[Event] = []
    bus.subscribe(MEMORY_STORE_REQUESTED, lambda e: _collect(stored, e))
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"}, confirmer=_Confirmer(True))
    mgr.register(_DangerTool())
    await _request_and_get_result(mgr, bus, {"tool_name": "peligro", "params": {}})

    assert len(stored) == 1
    payload = stored[0].payload
    assert payload["kind"] == "execution"
    assert payload["content"]["tool"] == "peligro"
    assert payload["content"]["success"] is True


async def test_unaudited_tool_leaves_no_record() -> None:
    from alice.core.events import MEMORY_STORE_REQUESTED

    bus = EventBus(max_queue_size=50)
    stored: list[Event] = []
    bus.subscribe(MEMORY_STORE_REQUESTED, lambda e: _collect(stored, e))
    mgr = ToolManager(bus=bus, granted_permissions={"read_system"})
    mgr.register(DateTimeTool())  # audit=False por defecto
    await _request_and_get_result(mgr, bus, {"tool_name": "datetime", "params": {}})
    assert stored == []


async def _collect(sink: list[Event], event: Event) -> None:
    sink.append(event)
