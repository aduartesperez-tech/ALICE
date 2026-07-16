"""Entrypoint de Alice Core.

Carga configuración, configura logging, compone el Orchestrator con los módulos
internos y corre hasta recibir Ctrl+C (SIGINT), momento en el que apaga limpio.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import TYPE_CHECKING

from alice.config import load_settings
from alice.core.orchestrator import CoreModule, Orchestrator
from alice.logging import get_logger, setup_logging

if TYPE_CHECKING:
    from alice.brain.agent_reasoner import AgentReasoner


def build_modules(orchestrator: Orchestrator) -> list[CoreModule]:
    """Construye los módulos internos que se enchufan al bus del orquestador.

    El orquestador ya está creado, así que los módulos reciben su ``bus`` y
    ``scheduler``. Este es el único punto de composición (composition root).
    """
    from alice.brain.executor import ActionExecutor
    from alice.brain.llm import LLMModule, LLMProvider, NullLLMProvider
    from alice.brain.memory import InMemoryShortTermMemory
    from alice.brain.memory_module import MemoryModule
    from alice.brain.memory_sqlite import (
        SqliteEpisodicMemory,
        SqliteLongTermMemory,
        open_database,
    )
    from alice.brain.planner import Planner, PlanningStrategy
    from alice.tools.builtin.camera_tool import CameraTool
    from alice.tools.builtin.datetime_tool import DateTimeTool
    from alice.tools.builtin.recall_tool import RecallMemoryTool
    from alice.tools.manager import ToolManager

    settings = orchestrator._settings  # noqa: SLF001 - composition root
    bus = orchestrator.bus

    # Memoria persistente (SQLite) + short-term en RAM
    conn = open_database(settings.memory.db_path)
    episodic = SqliteEpisodicMemory(conn)
    long_term = SqliteLongTermMemory(conn)
    short_term = InMemoryShortTermMemory(max_items=settings.memory.short_term_max_items)
    memory = MemoryModule(
        bus=bus,
        episodic=episodic,
        long_term=long_term,
        short_term=short_term,
        connection=conn,
    )

    # Herramientas (recall_memory necesita la memoria episódica ya creada)
    tool_manager = ToolManager(
        bus=bus,
        granted_permissions=settings.tools.granted_permissions,
        default_timeout=settings.tools.default_timeout_seconds,
    )
    tool_manager.register(DateTimeTool())
    tool_manager.register(RecallMemoryTool(episodic))
    tool_manager.register(CameraTool())

    # Proveedor LLM: real (OpenAI-compatible) o nulo, según config
    provider: LLMProvider
    if settings.llm.provider == "openai_compat":
        from alice.brain.llm_openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            base_url=settings.llm.base_url,
            model=settings.llm.model,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
            timeout_seconds=settings.llm.timeout_seconds,
        )
    else:
        provider = NullLLMProvider()

    llm = LLMModule(
        bus=bus,
        provider=provider,
        short_term=short_term,
        system_prompt=settings.llm.system_prompt,
        history_turns=settings.llm.history_turns,
    )

    # Estrategia de planificación (requiere proveedor real):
    #   "agent_loop":   bucle agente multi-vuelta (razona, usa tools, itera).
    #   "tool_calling": el LLM elige tools del catálogo real en una sola vuelta.
    #   "hybrid":       regex + clasificador de intención cerrado.
    #   "rules" (o sin proveedor): solo regex.
    strategy: PlanningStrategy | None = None
    agent_reasoner: AgentReasoner | None = None
    if settings.llm.provider == "openai_compat":
        if settings.planner.strategy == "agent_loop":
            from alice.brain.agent_loop import AgentLoopStrategy
            from alice.brain.agent_reasoner import AgentReasoner
            from alice.brain.tool_catalog import build_tool_specs

            strategy = AgentLoopStrategy(narrate=settings.llm.narrate)
            # El razonador ve el catálogo REAL de tools: añadir una la hace usable.
            agent_reasoner = AgentReasoner(
                bus=bus,
                provider=provider,
                tools=build_tool_specs(tool_manager.definitions()),
                system_prompt=settings.llm.system_prompt,
                timeout_seconds=settings.planner.intent_timeout_seconds,
            )
        elif settings.planner.strategy == "tool_calling":
            from alice.brain.planner_tools import ToolCallingStrategy
            from alice.brain.tool_catalog import build_tool_specs

            strategy = ToolCallingStrategy(
                provider=provider,
                tools=build_tool_specs(tool_manager.definitions()),
                narrate=settings.llm.narrate,
                timeout_seconds=settings.planner.intent_timeout_seconds,
            )
        elif settings.planner.strategy == "hybrid":
            from alice.brain.planner_llm import LLMIntentStrategy

            strategy = LLMIntentStrategy(
                provider=provider,
                narrate=settings.llm.narrate,
                timeout_seconds=settings.planner.intent_timeout_seconds,
            )
    planner = Planner(bus=bus, strategy=strategy, narrate=settings.llm.narrate)
    # El timeout del plan da margen al LLM local (lento). El bucle agente puede
    # encadenar varias llamadas por turno, así que se dimensiona a las vueltas.
    if agent_reasoner is not None:
        plan_timeout = settings.llm.timeout_seconds * (settings.planner.max_agent_iters + 1) + 30.0
    elif settings.llm.narrate:
        plan_timeout = settings.llm.timeout_seconds + 30.0
    else:
        plan_timeout = 30.0
    executor = ActionExecutor(
        bus=bus,
        scheduler=orchestrator.scheduler,
        plan_timeout_seconds=plan_timeout,
        max_agent_iters=settings.planner.max_agent_iters,
    )

    # Orden de arranque: memoria y tools/llm/razonador listos antes que executor y planner.
    modules: list[CoreModule] = [tool_manager, memory, llm]
    if agent_reasoner is not None:
        modules.append(agent_reasoner)
    modules.extend([executor, planner])
    return modules


async def _main() -> None:
    settings = load_settings()
    setup_logging(
        level=settings.logging.level,
        log_dir=settings.logging.log_dir,
        file_name=settings.logging.file_name,
        to_console=settings.logging.to_console,
    )
    log = get_logger("alice.main")

    orchestrator = Orchestrator(settings)
    orchestrator._modules = build_modules(orchestrator)  # noqa: SLF001 - composition root

    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, orchestrator)

    log.info("alice.booting")
    await orchestrator.run_forever()
    log.info("alice.exited")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    orchestrator: Orchestrator,
) -> None:
    """Instala manejadores de señal para un apagado ordenado (portátil)."""

    def _request_stop() -> None:
        orchestrator.request_stop()

    # SIGBREAK existe solo en Windows (Ctrl+Break); en Unix se ignora.
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows no soporta add_signal_handler: se usa signal.signal y se
            # despierta el loop de forma segura con call_soon_threadsafe.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_request_stop))


def main() -> int:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
