"""Tests del Planner: decisiones de reglas y emisión de plan.created."""

from __future__ import annotations

import asyncio

from alice.brain.plan import ActionKind, Plan
from alice.brain.planner import Planner, RuleBasedStrategy
from alice.core.event_bus import EventBus
from alice.core.events import COMMAND_RECEIVED, PLAN_CREATED, Event
from alice.core.payloads import CommandReceivedPayload


def test_datetime_rule_avoids_llm() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="¿Qué hora es?"))
    assert plan.rule == "datetime"
    assert plan.requires_llm is False
    assert plan.actions[0].kind is ActionKind.USE_TOOL
    assert plan.actions[0].target == "datetime"


def test_internet_query_falls_back_to_chat() -> None:
    # Las reglas internet/shell se eliminaron (apuntaban a tools inexistentes).
    # Hasta que exista la tool `internet` (Fase 2), estas consultas van a chat.
    plan = RuleBasedStrategy().plan(
        CommandReceivedPayload(text="Busca en internet cómo funciona MQTT")
    )
    assert plan.rule == "fallback_llm"
    assert plan.actions[0].kind is ActionKind.CALL_LLM


def test_shell_query_falls_back_to_chat() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="Apaga el servidor FTP"))
    assert plan.rule == "fallback_llm"
    assert plan.actions[0].kind is ActionKind.CALL_LLM


def test_reminder_rule_schedules() -> None:
    plan = RuleBasedStrategy().plan(
        CommandReceivedPayload(text="Recuérdame estirar en 10 segundos")
    )
    assert plan.rule == "reminder"
    assert plan.requires_llm is False
    schedule = plan.actions[0]
    assert schedule.kind is ActionKind.SCHEDULE
    assert schedule.params["seconds"] == 10
    assert "estirar" in schedule.params["message"]


def test_reminder_rule_converts_minutes() -> None:
    plan = RuleBasedStrategy().plan(
        CommandReceivedPayload(text="recuérdame llamar a mamá en 2 minutos")
    )
    assert plan.actions[0].params["seconds"] == 120


def test_fallback_goes_to_llm() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="Cuéntame un chiste"))
    assert plan.rule == "fallback_llm"
    assert plan.requires_llm is True
    assert plan.actions[0].kind is ActionKind.CALL_LLM


def test_remember_fact_rule() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="recuerda que me llamo Adrián"))
    assert plan.rule == "remember_fact"
    assert plan.actions[0].kind is ActionKind.REMEMBER
    assert plan.actions[0].params["content"]["text"] == "me llamo Adrián"


def test_remember_narrates_confirmation() -> None:
    # Con narrate, remember confirma en lenguaje natural (no vuelca crudo).
    plan = RuleBasedStrategy(narrate=True).plan(
        CommandReceivedPayload(text="recuerda que me llamo Adrián")
    )
    assert plan.action_kinds == ["remember", "call_llm", "respond"]


def test_recall_rule_reads_memory_and_narrates() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="¿qué sabes de mí?"))
    assert plan.rule == "recall"
    assert plan.actions[0].kind is ActionKind.USE_TOOL
    assert plan.actions[0].target == "recall_memory"
    # Recall siempre narra, aunque narrate=False (una lista cruda no responde).
    assert plan.requires_llm is True


def test_recall_como_me_llamo() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="cómo me llamo?"))
    assert plan.rule == "recall"


def test_vision_rule_uses_camera_and_narrates() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="¿me ves?"))
    assert plan.rule == "vision"
    assert plan.actions[0].kind is ActionKind.USE_TOOL
    assert plan.actions[0].target == "camera"
    assert plan.requires_llm is True  # una escena en crudo no responde "¿me ves?"


def test_vision_quien_esta() -> None:
    plan = RuleBasedStrategy().plan(CommandReceivedPayload(text="quién está frente a la cámara"))
    assert plan.rule == "vision"


def test_remember_wins_over_recall() -> None:
    # "recuerda que..." es guardar, no debe confundirse con "recuerdas...?".
    plan = RuleBasedStrategy().plan(
        CommandReceivedPayload(text="recuerda que mi color favorito es el azul")
    )
    assert plan.rule == "remember_fact"


def test_narrate_flag_adds_llm_to_tool_plans() -> None:
    # Sin narrar: datetime resuelve en crudo (sin LLM).
    plain = RuleBasedStrategy(narrate=False).plan(CommandReceivedPayload(text="¿qué hora es?"))
    assert plain.requires_llm is False

    # Narrando: el mismo plan intercala call_llm antes de responder.
    narrated = RuleBasedStrategy(narrate=True).plan(CommandReceivedPayload(text="¿qué hora es?"))
    assert narrated.requires_llm is True
    assert narrated.action_kinds == ["use_tool", "call_llm", "respond"]


async def test_planner_emits_plan_created() -> None:
    bus = EventBus(max_queue_size=50)
    planner = Planner(bus=bus)
    await planner.start()

    plans: list[Event] = []
    bus.subscribe(PLAN_CREATED, lambda e: _collect(plans, e))

    task = bus.start_consumer()
    await bus.publish(
        Event(type=COMMAND_RECEIVED, source="test", payload={"text": "¿qué hora es?"})
    )
    await bus._queue.join()  # noqa: SLF001
    await bus.stop()
    await asyncio.wait_for(task, timeout=1.0)
    await planner.stop()

    assert len(plans) == 1
    plan = Plan.model_validate(plans[0].payload)
    assert plan.rule == "datetime"
    assert plans[0].correlation_id is not None  # el flujo queda encadenado


async def _collect(sink: list[Event], event: Event) -> None:
    sink.append(event)
