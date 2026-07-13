"""Interfaces de memoria (tres niveles) y la única implementación de v1.

Arquitectura preparada, sin bases vectoriales ni persistencia:
- ShortTermMemory: RAM, conversación actual y eventos recientes (implementada).
- EpisodicMemory: conversaciones y resúmenes (Protocol + no-op documentado).
- LongTermMemory: hechos permanentes (Protocol + no-op documentado).

El backend real (SQLite, vectorial) será un detalle detrás de estos Protocols.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class MemoryItem(BaseModel):
    """Elemento genérico almacenado en memoria."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: str
    content: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None  # encadena el episodio con los logs del flujo


@runtime_checkable
class ShortTermMemory(Protocol):
    """Memoria de trabajo en RAM: acotada, volátil."""

    def add(self, item: MemoryItem) -> None: ...
    def recent(self, limit: int = 20) -> list[MemoryItem]: ...
    def clear(self) -> None: ...


@runtime_checkable
class EpisodicMemory(Protocol):
    """Memoria episódica: conversaciones, eventos importantes, resúmenes."""

    def add_episode(self, item: MemoryItem) -> None: ...
    def query_by_date(self, day: date) -> list[MemoryItem]: ...
    def recent_by_kind(self, kind: str, limit: int = 20) -> list[MemoryItem]: ...
    def daily_summary(self, day: date) -> str | None: ...


@runtime_checkable
class LongTermMemory(Protocol):
    """Memoria de largo plazo: hechos permanentes (preferencias, personas...)."""

    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any) -> None: ...


class InMemoryShortTermMemory:
    """Implementación en RAM con un buffer circular acotado."""

    def __init__(self, max_items: int = 200) -> None:
        self._items: deque[MemoryItem] = deque(maxlen=max_items)

    def add(self, item: MemoryItem) -> None:
        self._items.append(item)

    def recent(self, limit: int = 20) -> list[MemoryItem]:
        items = list(self._items)
        return items[-limit:]

    def clear(self) -> None:
        self._items.clear()


class NoOpEpisodicMemory:
    """Implementación no-op documentada; se sustituye por SQLite/vectorial en el futuro."""

    def add_episode(self, item: MemoryItem) -> None:
        return None

    def query_by_date(self, day: date) -> list[MemoryItem]:
        return []

    def recent_by_kind(self, kind: str, limit: int = 20) -> list[MemoryItem]:
        return []

    def daily_summary(self, day: date) -> str | None:
        return None


class NoOpLongTermMemory:
    """Implementación no-op documentada; se sustituye por un backend persistente."""

    def get(self, key: str) -> Any | None:
        return None

    def set(self, key: str, value: Any) -> None:
        return None
