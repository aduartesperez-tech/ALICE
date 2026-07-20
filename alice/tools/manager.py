"""ToolManager: registro, validación, permisos y ejecución de herramientas.

Escucha ``tool.requested``, resuelve la herramienta, valida los parámetros
contra su schema, comprueba permisos, la ejecuta con timeout y publica
``tool.finished`` con el resultado. Es la ÚNICA vía de ejecución de tools.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from alice.core.events import MEMORY_STORE_REQUESTED, TOOL_FINISHED, TOOL_REQUESTED, Event
from alice.core.payloads import (
    MemoryStoreRequestedPayload,
    ToolFinishedPayload,
    ToolRequestedPayload,
)
from alice.logging import get_logger
from alice.tools.tool import Permission, ToolResult

if TYPE_CHECKING:
    from typing import Protocol

    from alice.core.event_bus import EventBus, Subscription
    from alice.tools.tool import Tool, ToolDefinition

    class Confirmer(Protocol):
        """Quien puede preguntar al usuario y esperar su respuesta."""

        async def confirm(self, question: str, *, detail: str = "") -> bool: ...

_logger = get_logger("alice.tools.manager")


class ToolManager:
    """Registro y ejecutor controlado de herramientas. Es un ``CoreModule``."""

    name = "tool_manager"

    def __init__(
        self,
        *,
        bus: EventBus,
        granted_permissions: set[str] | None = None,
        default_timeout: float = 10.0,
        confirmer: Confirmer | None = None,
    ) -> None:
        self._bus = bus
        self._tools: dict[str, Tool] = {}
        self._granted = {Permission(p) for p in (granted_permissions or set())}
        self._timeout = default_timeout
        # Sin confirmer, una tool que pida confirmación NO se ejecuta (fail-closed).
        self._confirmer = confirmer
        self._subscription: Subscription | None = None

    def register(self, tool: Tool) -> None:
        """Registra una herramienta por su nombre."""
        self._tools[tool.definition.name] = tool
        _logger.info("tool.registered", extra={"tool": tool.definition.name})

    def definitions(self) -> list[ToolDefinition]:
        """Definiciones de las tools registradas (para el catálogo de function calling)."""
        return [tool.definition for tool in self._tools.values()]

    async def start(self) -> None:
        self._subscription = self._bus.subscribe(TOOL_REQUESTED, self._on_request)
        _logger.info("tool_manager.started", extra={"tools": list(self._tools)})

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)

    async def _on_request(self, event: Event) -> None:
        req = ToolRequestedPayload.model_validate(event.payload)
        result = await self._run(req)
        await self._bus.publish(
            Event(
                type=TOOL_FINISHED,
                source="tools.manager",
                correlation_id=event.correlation_id,
                payload=ToolFinishedPayload(
                    tool_name=req.tool_name,
                    success=result.success,
                    output=result.output,
                    error=result.error,
                ).model_dump(),
            )
        )

    async def _run(self, req: ToolRequestedPayload) -> ToolResult:
        """Valida y ejecuta una herramienta, devolviendo siempre un ToolResult."""
        tool = self._tools.get(req.tool_name)
        if tool is None:
            return self._fail(req.tool_name, f"herramienta desconocida: {req.tool_name}")

        missing = tool.definition.permissions - self._granted
        if missing:
            names = ", ".join(sorted(p.value for p in missing))
            return self._fail(req.tool_name, f"permiso denegado: {names}")

        try:
            params = tool.definition.parameters.model_validate(req.params)
        except Exception as exc:  # noqa: BLE001 - validación de parámetros
            return self._fail(req.tool_name, f"parámetros inválidos: {exc}")

        # Barandilla: lo que muta el sistema pide permiso ANTES de ejecutarse.
        question = tool.confirmation_question(params)
        if question is not None:
            if self._confirmer is None:
                return self._fail(
                    req.tool_name, "requiere confirmación y no hay canal para pedirla"
                )
            if not await self._confirmer.confirm(question, detail=req.tool_name):
                _logger.info("tool.denied", extra={"tool": req.tool_name})
                return ToolResult(success=False, error="cancelado por el usuario")

        timeout = tool.definition.timeout_seconds or self._timeout
        try:
            result = await asyncio.wait_for(tool.execute(params), timeout=timeout)
        except TimeoutError:
            result = self._fail(req.tool_name, "timeout de ejecución")
        except Exception as exc:  # noqa: BLE001 - un fallo de tool no tumba el manager
            _logger.exception("tool.execution_error", extra={"tool": req.tool_name})
            result = self._fail(req.tool_name, f"error de ejecución: {exc}")
        else:
            _logger.info("tool.finished", extra={"tool": req.tool_name, "success": result.success})

        if tool.definition.audit:
            await self._audit(req, result)
        return result

    async def _audit(self, req: ToolRequestedPayload, result: ToolResult) -> None:
        """Deja constancia de una ejecución en la memoria episódica."""
        await self._bus.publish(
            Event(
                type=MEMORY_STORE_REQUESTED,
                source="tools.manager",
                payload=MemoryStoreRequestedPayload(
                    store="episodic",
                    kind="execution",
                    content={
                        "tool": req.tool_name,
                        "params": req.params,
                        "success": result.success,
                        "output": result.output,
                        "error": result.error,
                    },
                ).model_dump(),
            )
        )

    @staticmethod
    def _fail(tool_name: str, error: str) -> ToolResult:
        _logger.warning("tool.rejected", extra={"tool": tool_name, "error": error})
        return ToolResult(success=False, error=error)
