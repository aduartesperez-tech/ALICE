"""Tests del OpenAICompatProvider con servidor simulado (httpx MockTransport)."""

from __future__ import annotations

import json

import httpx
import pytest

from alice.brain.llm import LLMMessage, LLMRequest, ToolSpec
from alice.brain.llm_openai_compat import OpenAICompatProvider


def _request() -> LLMRequest:
    return LLMRequest(
        messages=[LLMMessage(role="user", content="hola")],
        system="eres alice",
    )


async def test_generate_success_and_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "qwen2.5",
                "choices": [{"message": {"role": "assistant", "content": "¡hola!"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    provider = OpenAICompatProvider(
        base_url="http://localhost:1234/v1",
        model="qwen2.5",
        transport=httpx.MockTransport(handler),
    )
    resp = await provider.generate(_request())
    await provider.aclose()

    assert resp.text == "¡hola!"
    assert resp.model == "qwen2.5"
    assert resp.usage.prompt_tokens == 5
    # La ruta conserva el /v1 y el cuerpo lleva system + user.
    assert captured["path"] == "/v1/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]


async def test_generate_raises_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("demasiado lento")

    provider = OpenAICompatProvider(
        base_url="http://localhost:1234/v1",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(httpx.TimeoutException):
        await provider.generate(_request())
    await provider.aclose()


async def test_generate_raises_on_connection_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexión rechazada")

    provider = OpenAICompatProvider(
        base_url="http://localhost:1234/v1",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(httpx.ConnectError):
        await provider.generate(_request())
    await provider.aclose()


async def test_generate_raises_on_model_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    provider = OpenAICompatProvider(
        base_url="http://localhost:1234/v1",
        model="inexistente",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await provider.generate(_request())
    await provider.aclose()


_TOOLS = [ToolSpec(name="datetime", description="hora", parameters={"type": "object"})]


async def test_run_agent_step_returns_tool_calls() -> None:
    # Si el modelo pide tools, el paso de agente devuelve esas tool calls.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "datetime",
                                        "arguments": '{"tz": "local"}',
                                    }
                                }
                            ],
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatProvider(
        base_url="http://x/v1", model="m", transport=httpx.MockTransport(handler)
    )
    step = await provider.run_agent_step(_request(), _TOOLS)
    await provider.aclose()
    assert [c.name for c in step.tool_calls] == ["datetime"]
    assert step.tool_calls[0].arguments == {"tz": "local"}
    assert step.text == ""


async def test_run_agent_step_returns_final_text() -> None:
    # Si el modelo NO pide tools, se aprovecha su texto como respuesta final.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Son las tres."}}]},
        )

    provider = OpenAICompatProvider(
        base_url="http://x/v1", model="m", transport=httpx.MockTransport(handler)
    )
    step = await provider.run_agent_step(_request(), _TOOLS)
    await provider.aclose()
    assert step.tool_calls == []
    assert step.text == "Son las tres."


async def test_api_key_sets_authorization_header() -> None:
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "hola"}}]}
        )

    provider = OpenAICompatProvider(
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        api_key="sk-secreta",
        transport=httpx.MockTransport(handler),
    )
    await provider.generate(_request())
    await provider.aclose()
    assert captured["auth"] == "Bearer sk-secreta"


async def test_no_api_key_omits_authorization_header() -> None:
    # Servidores locales (LM Studio) no piden clave: no debe mandarse la cabecera.
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "hola"}}]}
        )

    provider = OpenAICompatProvider(
        base_url="http://localhost:1234/v1", model="local", transport=httpx.MockTransport(handler)
    )
    await provider.generate(_request())
    await provider.aclose()
    assert captured["auth"] is None


async def test_health_check() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("caído")

    up = OpenAICompatProvider(
        base_url="http://x/v1", model="m", transport=httpx.MockTransport(ok)
    )
    assert await up.health_check() is True
    await up.aclose()

    off = OpenAICompatProvider(
        base_url="http://x/v1", model="m", transport=httpx.MockTransport(down)
    )
    assert await off.health_check() is False
    await off.aclose()
