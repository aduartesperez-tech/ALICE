"""Máquina de estados del sistema.

STARTING → RUNNING → STOPPING → STOPPED. Emite eventos de sistema en las
transiciones relevantes. Es consultable en cualquier momento.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from alice.core.events import SYSTEM_STARTED, SYSTEM_STOPPED, Event, EventPriority
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.core.event_bus import EventBus

_logger = get_logger("alice.core.state")


class SystemState(StrEnum):
    """Estados posibles del sistema."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class StateManager:
    """Gestiona el estado global del sistema y publica eventos en las transiciones."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._state = SystemState.STOPPED

    @property
    def state(self) -> SystemState:
        """Estado actual del sistema."""
        return self._state

    def _transition(self, new_state: SystemState) -> None:
        old = self._state
        self._state = new_state
        _logger.info("state.transition", extra={"from": old.value, "to": new_state.value})

    def set_starting(self) -> None:
        self._transition(SystemState.STARTING)

    async def set_running(self) -> None:
        self._transition(SystemState.RUNNING)
        await self._bus.publish(
            Event(type=SYSTEM_STARTED, source="core.state", priority=EventPriority.HIGH)
        )

    def set_stopping(self) -> None:
        self._transition(SystemState.STOPPING)

    async def set_stopped(self) -> None:
        await self._bus.publish(
            Event(type=SYSTEM_STOPPED, source="core.state", priority=EventPriority.HIGH)
        )
        self._transition(SystemState.STOPPED)
