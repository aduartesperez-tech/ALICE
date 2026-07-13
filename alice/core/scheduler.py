"""Scheduler interno basado en asyncio (sin cron, sin threads).

Permite programar callbacks una sola vez tras un retardo, o de forma periódica.
Los jobs típicos publican eventos en el bus. Una excepción en un job se loguea
sin matar el scheduler; un job periódico que falla sigue programado.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alice.logging import get_logger

if TYPE_CHECKING:
    from datetime import timedelta

AsyncCallback = Callable[[], Awaitable[None]]

_logger = get_logger("alice.core.scheduler")


@dataclass(frozen=True, eq=False)
class JobHandle:
    """Handle opaco de un job programado; se usa para cancelarlo."""

    id: int
    kind: str = field(compare=False)


class Scheduler:
    """Programa tareas futuras y periódicas sobre el event loop."""

    def __init__(self) -> None:
        self._jobs: dict[int, asyncio.Task[None]] = {}
        self._ids = itertools.count()
        self._running = False

    def start(self) -> None:
        """Marca el scheduler como activo (habilita la programación de jobs)."""
        self._running = True
        _logger.info("scheduler.started")

    def schedule_once(self, delay: timedelta, callback: AsyncCallback) -> JobHandle:
        """Ejecuta ``callback`` una vez, tras ``delay``."""
        handle = JobHandle(id=next(self._ids), kind="once")
        task = asyncio.create_task(
            self._run_once(handle, delay.total_seconds(), callback),
            name=f"job_once_{handle.id}",
        )
        self._jobs[handle.id] = task
        _logger.info("scheduler.job_added", extra={"job_id": handle.id, "kind": "once"})
        return handle

    def schedule_interval(self, every: timedelta, callback: AsyncCallback) -> JobHandle:
        """Ejecuta ``callback`` de forma periódica cada ``every``."""
        handle = JobHandle(id=next(self._ids), kind="interval")
        task = asyncio.create_task(
            self._run_interval(handle, every.total_seconds(), callback),
            name=f"job_interval_{handle.id}",
        )
        self._jobs[handle.id] = task
        _logger.info("scheduler.job_added", extra={"job_id": handle.id, "kind": "interval"})
        return handle

    def cancel(self, handle: JobHandle) -> None:
        """Cancela un job programado."""
        task = self._jobs.pop(handle.id, None)
        if task is not None:
            task.cancel()
            _logger.info("scheduler.job_cancelled", extra={"job_id": handle.id})

    async def stop(self) -> None:
        """Cancela todos los jobs y espera a que terminen."""
        self._running = False
        tasks = list(self._jobs.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._jobs.clear()
        _logger.info("scheduler.stopped")

    async def _run_once(self, handle: JobHandle, delay: float, callback: AsyncCallback) -> None:
        try:
            await asyncio.sleep(delay)
            await self._safe_call(handle, callback)
        finally:
            self._jobs.pop(handle.id, None)

    async def _run_interval(self, handle: JobHandle, every: float, callback: AsyncCallback) -> None:
        while True:
            await asyncio.sleep(every)
            await self._safe_call(handle, callback)

    async def _safe_call(self, handle: JobHandle, callback: AsyncCallback) -> None:
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - un job no debe tumbar el scheduler
            _logger.exception("scheduler.job_error", extra={"job_id": handle.id})
