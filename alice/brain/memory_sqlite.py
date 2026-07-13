"""Implementaciones SQLite de la memoria episódica y de largo plazo.

Detrás de los Protocols de ``memory.py``: el resto del sistema no sabe que hay
SQLite. Un solo archivo (``data/alice.db``) en modo WAL, sin dependencias
externas. Sin vectores todavía (eso es v2.x).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from alice.brain.memory import MemoryItem
from alice.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger("alice.brain.memory_sqlite")

_EPISODES_DDL = """
CREATE TABLE IF NOT EXISTS episodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    content        TEXT NOT NULL,
    correlation_id TEXT
)
"""
_EPISODES_INDEX = "CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(timestamp)"
_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def open_database(db_path: Path) -> sqlite3.Connection:
    """Abre (o crea) la base, activa WAL y asegura el esquema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_EPISODES_DDL)
    conn.execute(_EPISODES_INDEX)
    conn.execute(_FACTS_DDL)
    conn.commit()
    _logger.info("memory.db_opened", extra={"db_path": str(db_path)})
    return conn


class SqliteEpisodicMemory:
    """Memoria episódica persistente: turnos, eventos importantes, thoughts."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add_episode(self, item: MemoryItem) -> None:
        self._conn.execute(
            "INSERT INTO episodes (timestamp, kind, content, correlation_id) VALUES (?, ?, ?, ?)",
            (
                item.timestamp.isoformat(),
                item.kind,
                json.dumps(item.content, ensure_ascii=False, default=str),
                item.correlation_id,
            ),
        )
        self._conn.commit()

    def query_by_date(self, day: date) -> list[MemoryItem]:
        cursor = self._conn.execute(
            "SELECT timestamp, kind, content, correlation_id FROM episodes "
            "WHERE substr(timestamp, 1, 10) = ? ORDER BY id",
            (day.isoformat(),),
        )
        return [self._row_to_item(row) for row in cursor.fetchall()]

    def recent_by_kind(self, kind: str, limit: int = 20) -> list[MemoryItem]:
        """Últimos episodios de un tipo dado (p.ej. ``user_fact``), del más
        antiguo al más reciente dentro del corte."""
        cursor = self._conn.execute(
            "SELECT timestamp, kind, content, correlation_id FROM episodes "
            "WHERE kind = ? ORDER BY id DESC LIMIT ?",
            (kind, limit),
        )
        rows = cursor.fetchall()
        # DESC para coger los más recientes; se re-ordena a cronológico ascendente.
        return [self._row_to_item(row) for row in reversed(rows)]

    def daily_summary(self, day: date) -> str | None:
        # Stub documentado: el resumen diario necesita LLM + un job del scheduler
        # (feature de v1.3+). Hoy no hay resumen.
        return None

    @staticmethod
    def _row_to_item(row: tuple[Any, ...]) -> MemoryItem:
        timestamp, kind, content, correlation_id = row
        return MemoryItem(
            timestamp=datetime.fromisoformat(timestamp),
            kind=kind,
            content=json.loads(content),
            correlation_id=correlation_id,
        )


class SqliteLongTermMemory:
    """Memoria de largo plazo persistente: hechos clave→valor."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> Any | None:
        cursor = self._conn.execute("SELECT value FROM facts WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False, default=str), datetime.now().isoformat()),
        )
        self._conn.commit()
