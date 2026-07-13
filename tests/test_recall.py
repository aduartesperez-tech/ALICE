"""Tests de la memoria de recall: recent_by_kind + RecallMemoryTool."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alice.brain.memory import MemoryItem
from alice.brain.memory_sqlite import SqliteEpisodicMemory, open_database
from alice.tools.builtin.recall_tool import RecallMemoryTool, RecallParams


@pytest.fixture
def episodic(tmp_path: Path) -> SqliteEpisodicMemory:
    conn: sqlite3.Connection = open_database(tmp_path / "test.db")
    return SqliteEpisodicMemory(conn)


def _fact(text: str) -> MemoryItem:
    return MemoryItem(kind="user_fact", content={"text": text})


def test_recent_by_kind_filters_and_orders(episodic: SqliteEpisodicMemory) -> None:
    episodic.add_episode(_fact("me llamo Adrián"))
    episodic.add_episode(MemoryItem(kind="user_turn", content={"text": "hola"}))
    episodic.add_episode(_fact("trabajo de noche"))

    facts = episodic.recent_by_kind("user_fact")
    textos = [f.content["text"] for f in facts]
    # Solo user_fact, en orden cronológico ascendente.
    assert textos == ["me llamo Adrián", "trabajo de noche"]


def test_recent_by_kind_respects_limit(episodic: SqliteEpisodicMemory) -> None:
    for i in range(5):
        episodic.add_episode(_fact(f"dato {i}"))
    facts = episodic.recent_by_kind("user_fact", limit=2)
    # Los 2 más recientes, en orden ascendente.
    assert [f.content["text"] for f in facts] == ["dato 3", "dato 4"]


def test_recent_by_kind_empty(episodic: SqliteEpisodicMemory) -> None:
    assert episodic.recent_by_kind("user_fact") == []


async def test_recall_tool_returns_facts(episodic: SqliteEpisodicMemory) -> None:
    episodic.add_episode(_fact("me llamo Adrián"))
    episodic.add_episode(_fact("tengo un perro"))
    tool = RecallMemoryTool(episodic)

    result = await tool.execute(RecallParams())
    assert result.success
    assert result.output == {"facts": ["me llamo Adrián", "tengo un perro"], "count": 2}


async def test_recall_tool_skips_blank_facts(episodic: SqliteEpisodicMemory) -> None:
    episodic.add_episode(_fact("dato real"))
    episodic.add_episode(_fact("   "))  # vacío tras strip: se descarta
    tool = RecallMemoryTool(episodic)

    result = await tool.execute(RecallParams())
    assert result.output == {"facts": ["dato real"], "count": 1}


async def test_recall_tool_empty_memory(episodic: SqliteEpisodicMemory) -> None:
    tool = RecallMemoryTool(episodic)
    result = await tool.execute(RecallParams())
    assert result.success
    assert result.output == {"facts": [], "count": 0}
