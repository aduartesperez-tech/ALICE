"""Tests de LLMIntentStrategy: clasificación de intención con proveedor falso."""

from __future__ import annotations

from alice.brain.llm import LLMProvider, LLMRequest, LLMResponse
from alice.brain.planner_llm import LLMIntentStrategy
from alice.core.payloads import CommandReceivedPayload


class FakeProvider(LLMProvider):
    """Devuelve un texto fijo y registra las peticiones que recibe."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="fake")

    async def health_check(self) -> bool:
        return True


class ExplodingProvider(LLMProvider):
    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("backend caído")

    async def health_check(self) -> bool:
        return False


def _cmd(text: str) -> CommandReceivedPayload:
    return CommandReceivedPayload(text=text)


async def test_rule_match_skips_llm():
    """Si un regex resuelve el comando, el LLM no se consulta."""
    provider = FakeProvider('{"intent": "chat", "params": {}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("¿qué hora es?"))
    assert plan.rule == "datetime"
    assert provider.requests == []


async def test_llm_classifies_datetime_paraphrase():
    provider = FakeProvider('{"intent": "datetime", "params": {}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("me dices qué horas son porfa"))
    assert plan.rule == "llm_intent:datetime"
    assert plan.actions[0].target == "datetime"
    # La clasificación debe pedirse determinista.
    assert provider.requests[0].temperature == 0.0


async def test_llm_classifies_reminder_with_params():
    provider = FakeProvider(
        '{"intent": "reminder", "params": {"message": "sacar el pan", "seconds": 600}}'
    )
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("no me dejes olvidar el pan, en unos 10 minutos"))
    assert plan.rule == "llm_intent:reminder"
    assert plan.actions[0].params == {"seconds": 600, "message": "sacar el pan"}


async def test_llm_classifies_remember_fact():
    provider = FakeProvider('{"intent": "remember", "params": {"fact": "mi gato es Miso"}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("ten presente que mi gato es Miso"))
    assert plan.rule == "llm_intent:remember"
    assert plan.actions[0].params["content"] == {"text": "mi gato es Miso"}


async def test_llm_classifies_recall():
    provider = FakeProvider('{"intent": "recall", "params": {}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("tienes algún dato guardado sobre mi persona?"))
    assert plan.rule == "llm_intent:recall"
    assert plan.actions[0].target == "recall_memory"
    assert plan.requires_llm  # recall siempre narra


async def test_llm_classifies_vision():
    provider = FakeProvider('{"intent": "vision", "params": {}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("oye, échame un ojo y dime si estoy despeinado"))
    assert plan.rule == "llm_intent:vision"
    assert plan.actions[0].target == "camera"
    assert plan.requires_llm


async def test_chat_intent_goes_to_llm_chat():
    provider = FakeProvider('{"intent": "chat", "params": {}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("cuéntame algo interesante"))
    assert plan.rule == "llm_intent:chat"
    assert plan.requires_llm


async def test_garbage_output_falls_back_to_chat():
    provider = FakeProvider("claro, puedo ayudarte con eso!")
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("sabes qué tan tarde es?"))
    assert plan.rule == "fallback_llm"


async def test_unknown_intent_falls_back_to_chat():
    provider = FakeProvider('{"intent": "hackear_nasa", "params": {}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("hola alice"))
    assert plan.rule == "fallback_llm"


async def test_reminder_with_bad_params_falls_back():
    provider = FakeProvider('{"intent": "reminder", "params": {"message": "", "seconds": -5}}')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("avísame luego"))
    assert plan.rule == "fallback_llm"


async def test_provider_error_falls_back_to_chat():
    strategy = LLMIntentStrategy(provider=ExplodingProvider())
    plan = await strategy.plan(_cmd("sabes qué tan tarde es?"))
    assert plan.rule == "fallback_llm"
    assert plan.requires_llm


async def test_json_embedded_in_prose_is_extracted():
    provider = FakeProvider('Claro: {"intent": "datetime", "params": {}} espero que ayude')
    strategy = LLMIntentStrategy(provider=provider)
    plan = await strategy.plan(_cmd("sabes qué tan tarde es?"))
    assert plan.rule == "llm_intent:datetime"
