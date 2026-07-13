"""Tests de los stores SQLite: persistencia, query_by_date y upsert de facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from alice.brain.memory import MemoryItem
from alice.brain.memory_sqlite import (
    SqliteEpisodicMemory,
    SqliteLongTermMemory,
    open_database,
)


def test_episodes_persist_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "alice.db"
    conn = open_database(db)
    episodic = SqliteEpisodicMemory(conn)
    episodic.add_episode(
        MemoryItem(kind="user_turn", content={"text": "hola"}, correlation_id="abc")
    )
    conn.close()

    # Reabrir el mismo archivo: el dato sigue ahí.
    conn2 = open_database(db)
    episodic2 = SqliteEpisodicMemory(conn2)
    today = datetime.now(UTC).date()
    items = episodic2.query_by_date(today)
    conn2.close()

    assert len(items) == 1
    assert items[0].kind == "user_turn"
    assert items[0].content["text"] == "hola"
    assert items[0].correlation_id == "abc"


def test_query_by_date_filters(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "alice.db")
    episodic = SqliteEpisodicMemory(conn)
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    episodic.add_episode(MemoryItem(timestamp=now, kind="event", content={"n": 1}))
    episodic.add_episode(MemoryItem(timestamp=yesterday, kind="event", content={"n": 2}))

    today_items = episodic.query_by_date(now.date())
    conn.close()

    assert len(today_items) == 1
    assert today_items[0].content["n"] == 1


def test_daily_summary_is_stub(tmp_path: Path) -> None:
    conn = open_database(tmp_path / "alice.db")
    episodic = SqliteEpisodicMemory(conn)
    assert episodic.daily_summary(datetime.now(UTC).date()) is None
    conn.close()


def test_facts_upsert_and_persist(tmp_path: Path) -> None:
    db = tmp_path / "alice.db"
    conn = open_database(db)
    lt = SqliteLongTermMemory(conn)
    lt.set("user.nombre", "Adrián")
    lt.set("user.nombre", "Adrian")  # sobrescribe (upsert)
    lt.set("pref.idioma", "es")
    conn.close()

    conn2 = open_database(db)
    lt2 = SqliteLongTermMemory(conn2)
    nombre = lt2.get("user.nombre")
    idioma = lt2.get("pref.idioma")
    ausente = lt2.get("no.existe")
    conn2.close()

    assert nombre == "Adrian"
    assert idioma == "es"
    assert ausente is None
