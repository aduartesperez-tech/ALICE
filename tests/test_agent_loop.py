"""Tests de AgentLoopStrategy: regex primero, bucle agente si nada aplica."""

from __future__ import annotations

from alice.brain.agent_loop import AgentLoopStrategy
from alice.brain.plan import ActionKind
from alice.core.payloads import CommandReceivedPayload


def _cmd(text: str, *, internal: bool = False) -> CommandReceivedPayload:
    return CommandReceivedPayload(text=text, internal=internal)


def test_chat_enters_agent_loop() -> None:
    # Nada trivial: el comando entra al bucle agente (un único paso AGENT_STEP).
    plan = AgentLoopStrategy().plan(_cmd("hazme un resumen de mi situación general"))
    assert plan.rule == "agent"
    assert plan.action_kinds == ["agent_step"]
    assert plan.actions[0].kind is ActionKind.AGENT_STEP
    assert plan.user_text  # el bucle necesita el texto original del usuario


def test_reminder_fastpath_skips_loop() -> None:
    plan = AgentLoopStrategy().plan(_cmd("recuérdame estirar en 10 segundos"))
    assert plan.rule == "reminder"  # el regex lo resolvió: sin bucle ni LLM
    assert "agent_step" not in plan.action_kinds


def test_datetime_fastpath_skips_loop() -> None:
    plan = AgentLoopStrategy().plan(_cmd("¿qué hora es?"))
    assert plan.rule == "datetime"
    assert "agent_step" not in plan.action_kinds


def test_internal_reaction_goes_to_chat() -> None:
    # Una reacción sintética (visión) no necesita tools: va directa a chat.
    plan = AgentLoopStrategy().plan(_cmd("(Viste a Adrián. Salúdalo.)", internal=True))
    assert plan.rule == "reaction"
    assert "agent_step" not in plan.action_kinds
