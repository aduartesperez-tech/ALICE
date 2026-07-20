"""Proveedor LLM para servidores con API OpenAI-compatible.

Sirve para LM Studio (``:1234/v1``), Ollama (``:11434/v1``) y cualquier servidor
local o remoto que exponga ``/chat/completions``. Local vs remoto es solo una
``base_url`` distinta. Ningún tipo de la API sale de este archivo: la frontera
del resto del sistema sigue siendo ``LLMRequest`` / ``LLMResponse``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from alice.brain.llm import AgentStep, LLMProvider, LLMResponse, LLMToolCall, LLMUsage
from alice.logging import get_logger

if TYPE_CHECKING:
    from alice.brain.llm import LLMRequest, ToolSpec

_logger = get_logger("alice.brain.llm_openai_compat")


class OpenAICompatProvider(LLMProvider):
    """Cliente de un endpoint OpenAI-compatible (chat completions)."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 512,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        normalized = base_url.rstrip("/") + "/"
        # Servidores locales (LM Studio/Ollama) no piden clave; OpenAI y compañía sí.
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self._client = httpx.AsyncClient(
            base_url=normalized,
            timeout=timeout_seconds,
            transport=transport,
            headers=headers,
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)

        # None = "usa el default del proveedor". No usar `or`: temperature=0.0
        # es un valor válido y explícito, no una ausencia.
        temperature = request.temperature if request.temperature is not None else self._temperature
        max_tokens = request.max_tokens if request.max_tokens is not None else self._max_tokens
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        response = await self._client.post("chat/completions", json=body)
        response.raise_for_status()
        data = response.json()
        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=data.get("model", self._model),
            usage=LLMUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            ),
        )

    async def select_tools(
        self, request: LLMRequest, tools: list[ToolSpec]
    ) -> list[LLMToolCall]:
        """Pide al modelo que elija tools vía function calling (``tool_choice=auto``).

        Si el modelo no soporta tools o no elige ninguna, devuelve lista vacía y
        el sistema degrada a conversación. Solo se usa para SELECCIONAR: los
        resultados se narran después por el pipeline normal del executor.
        """
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ],
            "tool_choice": "auto",
            "temperature": 0.0,  # elegir tool debe ser determinista
            "stream": False,
        }
        response = await self._client.post("chat/completions", json=body)
        response.raise_for_status()
        message = response.json()["choices"][0].get("message", {})
        return self._parse_tool_calls(message.get("tool_calls") or [])

    async def run_agent_step(
        self, request: LLMRequest, tools: list[ToolSpec]
    ) -> AgentStep:
        """Un paso del bucle agente: el modelo elige tools o responde.

        Igual que ``select_tools`` pero, si el modelo NO pide tools, aprovecha el
        texto que devolvió como respuesta final (una sola llamada por vuelta).
        """
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ],
            "tool_choice": "auto",
            "temperature": self._temperature,
            "stream": False,
        }
        response = await self._client.post("chat/completions", json=body)
        response.raise_for_status()
        message = response.json()["choices"][0].get("message", {})
        calls = self._parse_tool_calls(message.get("tool_calls") or [])
        if calls:
            return AgentStep(tool_calls=calls)
        return AgentStep(text=message.get("content") or "")

    @staticmethod
    def _parse_tool_calls(raw: list[dict[str, Any]]) -> list[LLMToolCall]:
        calls: list[LLMToolCall] = []
        for tc in raw:
            fn = tc.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (ValueError, TypeError):
                args = {}
            calls.append(LLMToolCall(name=str(name), arguments=args))
        return calls

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("models")
        except httpx.HTTPError as exc:
            _logger.warning("llm.provider_down", extra={"error": str(exc)})
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()
