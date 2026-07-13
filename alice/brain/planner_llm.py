"""Estrategia de planificación híbrida: reglas primero, LLM como intérprete.

Las reglas regex de ``RuleBasedStrategy`` resuelven las frases exactas (gratis,
instantáneo). Cuando ninguna aplica, en vez de mandar el texto directo al chat,
se le pide al LLM que CLASIFIQUE la intención del usuario ("¿pide la hora?,
¿un recordatorio?, ¿solo conversa?") y con esa intención se construye el plan.
Así "me dices qué horas son porfa" llega a la tool ``datetime`` aunque ningún
regex la reconozca.

Si el LLM tarda, falla o responde algo no parseable, se degrada al plan de
chat normal: la clasificación nunca puede romper el flujo.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

from alice.brain.llm import LLMMessage, LLMRequest
from alice.brain.planner import (
    RuleBasedStrategy,
    build_chat_plan,
    build_datetime_plan,
    build_recall_plan,
    build_remember_plan,
    build_reminder_plan,
    build_vision_plan,
)
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.brain.llm import LLMProvider
    from alice.brain.plan import Plan
    from alice.core.payloads import CommandReceivedPayload

_logger = get_logger("alice.brain.planner_llm")

# Máximo de un recordatorio: 7 días. Evita que un número mal extraído
# programe un job absurdo.
_MAX_REMINDER_SECONDS = 7 * 24 * 3600

_CLASSIFY_SYSTEM = """\
Eres el clasificador de intenciones de Alice. Analiza el mensaje del usuario \
y responde SOLO con un objeto JSON en una línea, sin explicaciones ni texto extra.

Reglas para los params:
- Extrae los valores ÚNICAMENTE del mensaje del usuario. NUNCA copies datos de \
los ejemplos de abajo: son solo formato, no información real.
- Distingue GUARDAR de RECUPERAR:
  - "remember" = el usuario te DA un dato nuevo para que lo guardes \
("recuerda que...", "apréndete...", "ten presente que...", "mi X es Y").
  - "recall" = el usuario te PIDE un dato que ya deberías saber \
("qué sabes de mí", "cómo me llamo", "recuerdas...?", "qué te dije de...").

Intenciones posibles:
- "datetime": pregunta la hora, la fecha o el día actual. params: {}
- "reminder": pide que le avises algo tras un tiempo. \
params: {"message": "<qué recordar>", "seconds": <tiempo en segundos>}
- "remember": te da un dato NUEVO sobre él para guardar. params: {"fact": "<el dato>"}
- "recall": te pide datos que YA guardó sobre él. params: {}
- "vision": pregunta qué ves por la cámara AHORA (si le ves, quién está, qué \
gesto hace). params: {}
- "chat": cualquier otra cosa (saludos, preguntas generales, conversación). params: {}

Ejemplos (el contenido es ilustrativo, no lo reutilices):
Usuario: dame la hora porfa
{"intent": "datetime", "params": {}}
Usuario: sabes en qué día estamos?
{"intent": "datetime", "params": {}}
Usuario: me avisas en 10 minutos que llame al médico
{"intent": "reminder", "params": {"message": "llamar al médico", "seconds": 600}}
Usuario: no me dejes olvidar la reunión, es en dos horas
{"intent": "reminder", "params": {"message": "la reunión", "seconds": 7200}}
Usuario: apréndete que mi perro se llama Rocky
{"intent": "remember", "params": {"fact": "mi perro se llama Rocky"}}
Usuario: qué sabes de mí?
{"intent": "recall", "params": {}}
Usuario: cómo me llamo?
{"intent": "recall", "params": {}}
Usuario: recuerdas lo que te conté?
{"intent": "recall", "params": {}}
Usuario: me ves bien?
{"intent": "vision", "params": {}}
Usuario: quién está frente a la cámara?
{"intent": "vision", "params": {}}
Usuario: cuéntame un chiste
{"intent": "chat", "params": {}}
Usuario: qué opinas de la lluvia
{"intent": "chat", "params": {}}\
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class LLMIntentStrategy:
    """Estrategia híbrida: regex de ``RuleBasedStrategy`` + clasificación LLM.

    Cumple ``PlanningStrategy`` en su variante asíncrona. Reutiliza el mismo
    ``LLMProvider`` que el módulo de chat (misma conexión al backend).
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        narrate: bool = False,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._provider = provider
        self._rules = RuleBasedStrategy(narrate=narrate)
        self._narrate = narrate
        self._timeout = timeout_seconds

    async def plan(self, command: CommandReceivedPayload) -> Plan:
        if command.internal:
            # Reacción sintética (visión, etc.): va directa a chat, sin clasificar.
            return build_chat_plan(command.text, rule="reaction")
        base = self._rules.plan(command)
        if base.rule != "fallback_llm":
            # Una regla explícita ya resolvió el comando: no gastamos LLM.
            return base

        started = time.monotonic()
        try:
            intent, params = await asyncio.wait_for(
                self._classify(command.text), timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - la clasificación nunca rompe el flujo
            _logger.warning(
                "planner_llm.classify_failed",
                extra={"command": command.text, "error": str(exc)},
            )
            return base

        elapsed_ms = int((time.monotonic() - started) * 1000)
        _logger.info(
            "planner_llm.intent",
            extra={"command": command.text, "intent": intent, "elapsed_ms": elapsed_ms},
        )
        return self._build(command.text, intent, params) or base

    async def _classify(self, text: str) -> tuple[str, dict[str, Any]]:
        request = LLMRequest(
            messages=[LLMMessage(role="user", content=text)],
            system=_CLASSIFY_SYSTEM,
            temperature=0.0,  # clasificar debe ser determinista, no creativo
            max_tokens=120,
        )
        response = await self._provider.generate(request)
        match = _JSON_BLOCK.search(response.text)
        if match is None:
            raise ValueError(f"sin JSON en la respuesta: {response.text!r}")
        data = json.loads(match.group(0))
        intent = str(data.get("intent", "")).strip().lower()
        params = data.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        return intent, params

    def _build(self, text: str, intent: str, params: dict[str, Any]) -> Plan | None:
        """Traduce (intención, params) a un Plan. ``None`` si no es utilizable."""
        rule = f"llm_intent:{intent}"
        if intent == "datetime":
            return build_datetime_plan(text, rule=rule, narrate=self._narrate)

        if intent == "recall":
            return build_recall_plan(text, rule=rule, narrate=self._narrate)

        if intent == "vision":
            return build_vision_plan(text, rule=rule)

        if intent == "reminder":
            message = str(params.get("message", "")).strip()
            try:
                seconds = int(params.get("seconds", 0))
            except (TypeError, ValueError):
                seconds = 0
            if not message or not 0 < seconds <= _MAX_REMINDER_SECONDS:
                _logger.warning(
                    "planner_llm.bad_params", extra={"intent": intent, "params": params}
                )
                return None
            return build_reminder_plan(
                text, rule=rule, seconds=seconds, message=message, narrate=self._narrate
            )

        if intent == "remember":
            fact = str(params.get("fact", "")).strip()
            if not fact:
                _logger.warning(
                    "planner_llm.bad_params", extra={"intent": intent, "params": params}
                )
                return None
            return build_remember_plan(text, rule=rule, fact=fact, narrate=self._narrate)

        if intent == "chat":
            return build_chat_plan(text, rule=rule)

        # Intención desconocida (el modelo inventó una): mejor caer a chat.
        _logger.warning("planner_llm.unknown_intent", extra={"intent": intent})
        return None
