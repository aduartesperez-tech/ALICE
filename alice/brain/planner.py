"""Planner: decide QUÉ hacer ante un comando. No responde ni ejecuta.

Escucha ``command.received``, produce un ``Plan`` mediante una
``PlanningStrategy`` intercambiable y emite ``plan.created``. La ejecución la
hace el Action Executor; el Planner solo decide y traza su decisión.

El objetivo es MINIMIZAR el uso del LLM: si una regla resuelve el comando con una
herramienta local, el plan no incluye ``call_llm``.
"""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING, Protocol

from alice.brain.plan import Action, ActionKind, Plan
from alice.core.events import COMMAND_RECEIVED, PLAN_CREATED, Event
from alice.core.payloads import CommandReceivedPayload
from alice.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from alice.core.event_bus import EventBus, Subscription

_logger = get_logger("alice.brain.planner")

_UNIT_SECONDS = {
    "seg": 1, "segundo": 1, "segundos": 1,
    "min": 60, "minuto": 60, "minutos": 60,
    "hora": 3600, "horas": 3600,
}


class PlanningStrategy(Protocol):
    """Contrato de una estrategia de planificación (reglas o LLM).

    ``plan`` puede ser síncrono (reglas) o asíncrono (consulta a un LLM);
    el Planner acepta ambos.
    """

    def plan(self, command: CommandReceivedPayload) -> Plan | Awaitable[Plan]: ...


def _tail(narrate: bool) -> list[Action]:
    """Cola de un plan de tool: narra con el LLM antes de responder si procede."""
    if narrate:
        return [
            Action(kind=ActionKind.CALL_LLM, target="llm"),
            Action(kind=ActionKind.RESPOND),
        ]
    return [Action(kind=ActionKind.RESPOND)]


# --- Constructores de planes -------------------------------------------------
# Compartidos por RuleBasedStrategy (regex) y LLMIntentStrategy (planner_llm):
# la intención puede venir de una regla o del LLM, pero el plan es el mismo.


def build_datetime_plan(text: str, *, rule: str, narrate: bool) -> Plan:
    return Plan(
        goal="Decir al usuario la fecha y hora actuales",
        rule=rule,
        user_text=text,
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="datetime"),
            *_tail(narrate),
        ],
    )


def build_vision_plan(text: str, *, rule: str) -> Plan:
    # Consultar la cámara: tool de lectura + narración. Siempre pasa por el LLM
    # (una escena en crudo no responde a "¿me ves?").
    return Plan(
        goal="Responder al usuario con lo que Alice ve por la cámara",
        rule=rule,
        user_text=text,
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="camera"),
            Action(kind=ActionKind.CALL_LLM, target="llm"),
            Action(kind=ActionKind.RESPOND),
        ],
    )


def build_recall_plan(text: str, *, rule: str, narrate: bool) -> Plan:
    # Recuperar lo que Alice sabe del usuario: tool de lectura + narración.
    # Recall siempre pasa por el LLM (aunque narrate=False): una lista de hechos
    # en crudo no responde a "¿cómo me llamo?".
    return Plan(
        goal="Responder al usuario con lo que Alice sabe de él",
        rule=rule,
        user_text=text,
        actions=[
            Action(kind=ActionKind.USE_TOOL, target="recall_memory"),
            Action(kind=ActionKind.CALL_LLM, target="llm"),
            Action(kind=ActionKind.RESPOND),
        ],
    )


def build_reminder_plan(text: str, *, rule: str, seconds: int, message: str, narrate: bool) -> Plan:
    # Recordar es una acción sobre el scheduler, NO una herramienta.
    return Plan(
        goal=f"Confirmar al usuario que le recordarás: {message}",
        rule=rule,
        user_text=text,
        actions=[
            Action(
                kind=ActionKind.SCHEDULE,
                target="scheduler",
                params={"seconds": seconds, "message": message},
            ),
            *_tail(narrate),
        ],
    )


def build_remember_plan(text: str, *, rule: str, fact: str, narrate: bool) -> Plan:
    # Guardar un hecho es una acción sobre la memoria, NO una herramienta.
    return Plan(
        goal=f"Confirmar al usuario que has guardado: {fact}",
        rule=rule,
        user_text=text,
        actions=[
            Action(
                kind=ActionKind.REMEMBER,
                target="episodic",
                params={
                    "store": "episodic",
                    "kind": "user_fact",
                    "content": {"text": fact},
                },
            ),
            *_tail(narrate),
        ],
    )


def build_chat_plan(text: str, *, rule: str = "fallback_llm") -> Plan:
    return Plan(
        goal="Responder al usuario en lenguaje natural",
        rule=rule,
        user_text=text,
        actions=[
            Action(kind=ActionKind.CALL_LLM, target="llm"),
            Action(kind=ActionKind.RESPOND),
        ],
    )


class RuleBasedStrategy:
    """Estrategia v1: reglas por palabras clave. Intercambiable sin tocar el Planner.

    Con ``narrate=True``, los planes que producen datos con una tool intercalan
    ``call_llm`` antes de ``respond`` para que el LLM los redacte en prosa. Con
    ``narrate=False`` (o proveedor nulo), las respuestas salen en crudo.
    """

    def __init__(self, *, narrate: bool = False) -> None:
        self._narrate = narrate

    _DATETIME = re.compile(r"(\bhora\b|\bfecha\b|qu[eé]\s+d[ií]a|\bd[ií]a de hoy\b)", re.IGNORECASE)
    _INTERNET = re.compile(
        r"(busca en internet|búscame|buscar en internet|googlea)", re.IGNORECASE
    )
    _SHELL = re.compile(r"(apaga|apagar|reinicia|ejecuta|mata el proceso)", re.IGNORECASE)
    _REMINDER = re.compile(
        r"recu[ée]rdame\s+(?P<msg>.+?)\s+en\s+(?P<n>\d+)\s*(?P<unit>segundos?|minutos?|horas?|seg|min)",
        re.IGNORECASE,
    )
    _REMEMBER = re.compile(r"recuerda que\s+(?P<fact>.+)", re.IGNORECASE)
    # Consultas sobre lo que Alice sabe del usuario ("¿qué sabes de mí?",
    # "¿cómo me llamo?", "¿recuerdas...?"). OJO: "recuerda que" (guardar) va antes.
    _RECALL = re.compile(
        r"(qu[eé] sabes de m[ií]|c[oó]mo me llamo|mi nombre|"
        r"recuerdas|qu[eé] recuerdas|qu[eé] te dije|qu[eé] sabes sobre m[ií])",
        re.IGNORECASE,
    )
    # Consultas sobre lo que Alice ve por la cámara.
    _VISION = re.compile(
        r"(me ves|puedes verme|qu[eé] ves|a qui[eé]n ves|qui[eé]n est[aá]|"
        r"qui[eé]n hay|me est[aá]s viendo|qu[eé] estoy haciendo|"
        r"qu[eé] gesto|mira(?:me)?\b)",
        re.IGNORECASE,
    )

    def plan(self, command: CommandReceivedPayload) -> Plan:
        text = command.text

        reminder = self._REMINDER.search(text)
        if reminder:
            seconds = int(reminder.group("n")) * _UNIT_SECONDS[reminder.group("unit").lower()]
            message = reminder.group("msg").strip()
            return build_reminder_plan(
                text, rule="reminder", seconds=seconds, message=message, narrate=self._narrate
            )

        remember = self._REMEMBER.search(text)
        if remember:
            fact = remember.group("fact").strip()
            return build_remember_plan(
                text, rule="remember_fact", fact=fact, narrate=self._narrate
            )

        if self._VISION.search(text):
            return build_vision_plan(text, rule="vision")

        if self._RECALL.search(text):
            return build_recall_plan(text, rule="recall", narrate=self._narrate)

        if self._DATETIME.search(text):
            # Hora/fecha se resuelve con una tool local: NO se llama al LLM.
            return build_datetime_plan(text, rule="datetime", narrate=self._narrate)

        if self._INTERNET.search(text):
            # Buscar en internet y luego redactar con el LLM.
            return Plan(
                goal="Buscar información en internet y responder",
                rule="internet_search",
                user_text=text,
                actions=[
                    Action(kind=ActionKind.USE_TOOL, target="internet", params={"query": text}),
                    Action(kind=ActionKind.CALL_LLM, target="llm"),
                    Action(kind=ActionKind.RESPOND),
                ],
            )

        if self._SHELL.search(text):
            # Acción del sistema con una tool shell: NO se llama al LLM.
            return Plan(
                goal="Ejecutar una acción del sistema",
                rule="shell",
                user_text=text,
                actions=[
                    Action(kind=ActionKind.USE_TOOL, target="shell", params={"command": text}),
                    *_tail(self._narrate),
                ],
            )

        # Sin regla que aplique: el comando va directo al LLM.
        return build_chat_plan(text)


class Planner:
    """Módulo interno que planifica comandos y emite ``plan.created``."""

    name = "planner"

    def __init__(
        self,
        *,
        bus: EventBus,
        strategy: PlanningStrategy | None = None,
        narrate: bool = False,
    ) -> None:
        self._bus = bus
        self._strategy: PlanningStrategy = strategy or RuleBasedStrategy(narrate=narrate)
        self._subscription: Subscription | None = None

    async def start(self) -> None:
        self._subscription = self._bus.subscribe(COMMAND_RECEIVED, self._on_command)
        _logger.info("planner.started", extra={"strategy": type(self._strategy).__name__})

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)

    async def _on_command(self, event: Event) -> None:
        command = CommandReceivedPayload.model_validate(event.payload)
        result = self._strategy.plan(command)
        # La estrategia puede ser síncrona (reglas) o asíncrona (LLM).
        plan = await result if inspect.isawaitable(result) else result
        # correlation_id: encadena todo el flujo derivado de este comando.
        correlation_id = event.correlation_id or event.id

        _logger.info(
            "planner.decision",
            extra={
                "command": command.text,
                "rule": plan.rule,
                "requires_llm": plan.requires_llm,
                "actions": plan.action_kinds,
                "correlation_id": str(correlation_id),
            },
        )
        await self._bus.publish(
            Event(
                type=PLAN_CREATED,
                source="brain.planner",
                correlation_id=correlation_id,
                payload=plan.model_dump(),
            )
        )
