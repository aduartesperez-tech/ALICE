# Alice

Núcleo (sistema nervioso) de una IA modular basada en eventos. **No es un
chatbot**: es una plataforma asíncrona que recibe eventos de módulos (voz,
visión, herramientas, LLM…), los distribuye mediante un EventBus propio y decide
con un Planner cuándo usar herramientas y cuándo —minimizándolo— un modelo de
lenguaje.

Estado actual: además del núcleo, ya funcionan un **LLM real** (LM Studio /
Ollama, OpenAI-compatible), **memoria persistente** (SQLite), **tool-calling**
(el LLM elige del catálogo de tools), y plugins de **voz** (`voice_web`, micrófono
+ Whisper + TTS) y **visión** (`vision`, webcam + reconocimiento de caras +
gestos). El plan a futuro —hacia un agente completo— está en
[`PLAN_DEFINITIVO.md`](PLAN_DEFINITIVO.md); los documentos de planeación de las
versiones ya entregadas están archivados en [`docs/historico/`](docs/historico/).

## Requisitos

- Python 3.12+ (probado con 3.14)

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux/macOS
pip install -e ".[dev]"
```

## Arrancar el sistema

```bash
python main.py
```

Arranca el bus, el scheduler, los módulos internos (Planner, Action Executor,
ToolManager, LLMModule, MemoryModule) y descubre los plugins de `plugins/`. El
plugin de consola te da un prompt interactivo: escribe un comando y Alice
responde.

```
tú> ¿qué hora es?
alice> [datetime] iso=2026-07-07T01:51:23+00:00, date=2026-07-07, time=01:51:23
tú> recuérdame estirar en 10 segundos
alice> [reminder_set] seconds=10.0, message=estirar
   (10 segundos después)
alice> [reminder] message=estirar
```

Los logs estructurados en JSON van a `logs/alice.log` (y a stderr). Se apaga de
forma ordenada con **Ctrl+C** (publica `system.stopped`, drena la cola, detiene
plugins y jobs).

Sin un LLM configurado (`provider = "null"`, por defecto) las respuestas salen
en crudo (`[tipo] datos`). Los datos **siempre** viajan estructurados
(`Observation`); el texto en prosa lo pone el LLM. Ese es el diseño: ningún
módulo formatea prosa, la IA es la capa de presentación.

## Conectar un LLM real (LM Studio u Ollama)

Alice habla con cualquier servidor con API OpenAI-compatible. En
`config/alice.toml`, sección `[llm]`:

```toml
[llm]
provider = "openai_compat"
base_url = "http://localhost:1234/v1"   # LM Studio
# base_url = "http://localhost:11434/v1" # Ollama
model = "el-modelo-que-tengas-cargado"
narrate = true
```

- **LM Studio:** abre la app, carga un modelo, pulsa *Start Server* (puerto
  1234 por defecto). El `model` puede ser el nombre que muestra la app.
- **Ollama:** `ollama serve` y `ollama pull <modelo>`; usa el puerto 11434.
- **Servidor remoto:** cambia `base_url` a la IP del servidor. Nada más — el
  resto del sistema no distingue local de remoto.

Con `narrate = true`, "¿qué hora es?" pasa el dato de la tool por el LLM y
responde en prosa ("Son las 8:51"). Si el servidor está apagado, Alice
**no falla**: responde en crudo y loguea `llm.provider_down`.

## Memoria persistente

Alice recuerda entre reinicios en `data/alice.db` (SQLite):

- Cada turno de conversación se guarda automáticamente (memoria episódica).
- "recuerda que me llamo Adrián" guarda un hecho vía la acción `remember`.
- El LLM recibe los últimos turnos como contexto (`history_turns` en config).

## Calidad

```bash
pytest              # tests
mypy alice main.py  # tipado estricto
ruff check .        # lint
```

## Arquitectura en un vistazo

```
command.received ──> Planner ──(decide, sin IA si puede)──> tool.requested / llm.requested
                        │                                          │
                        └──> plan.created (log de la decisión)     ▼
                                                            ToolManager / LLMModule
                                                                   │
                                                            tool.finished / llm.finished
```

- **EventBus** (`alice/core/event_bus.py`): cola de prioridad asíncrona y
  acotada. Todos los módulos se comunican **solo** por eventos; ninguno importa
  a otro módulo de negocio.
- **Orchestrator** (`alice/core/orchestrator.py`): única *composition root*.
  Compone y supervisa; **no contiene lógica de negocio**.
- **Planner** (`alice/brain/planner.py`): decide *qué hacer*. Estrategia de
  reglas intercambiable (`PlanningStrategy`); en el futuro una estrategia basada
  en LLM la reemplaza sin tocar nada más.
- **ToolManager** (`alice/tools/manager.py`): única vía de ejecución de
  herramientas. Valida parámetros contra su schema, comprueba permisos y aplica
  timeout. Ni el planner ni el LLM ejecutan tools directamente.
- **LLMProvider** (`alice/brain/llm.py`): interfaz abstracta. El resto del
  sistema nunca ve tipos de un proveedor concreto. v1 incluye `NullLLMProvider`.
- **Memoria** (`alice/brain/memory.py`): tres niveles como `Protocol`
  (short/episodic/long); v1 implementa la de corto plazo en RAM.

## Crear un plugin nuevo

Un plugin es **una carpeta** en `plugins/`. No hay que tocar ninguna otra línea
del sistema: se descubre automáticamente al arrancar.

1. Crea `plugins/mi_plugin/manifest.toml`:

   ```toml
   [plugin]
   name = "mi_plugin"
   version = "1.0.0"
   description = "Qué hace el plugin."
   subscribes = ["command.received"]   # eventos que quiere recibir
   ```

2. Crea `plugins/mi_plugin/plugin.py` con una clase `AlicePlugin(Plugin)`:

   ```python
   from __future__ import annotations
   from alice.core.events import Event
   from alice.core.plugin import Plugin, PluginManifest, PluginContext

   class AlicePlugin(Plugin):
       manifest = PluginManifest(name="mi_plugin", subscribes=["command.received"])

       async def initialize(self, ctx: PluginContext) -> None:
           self._ctx = ctx

       async def start(self) -> None: ...
       async def stop(self) -> None: ...

       async def handle_event(self, event: Event) -> None:
           # Reacciona y publica nuevos eventos mediante el contexto.
           await self._ctx.publish(Event(type="mi_plugin.hecho", source="plugin.mi_plugin"))
   ```

El plugin **solo** conoce su `PluginContext` (publicar, config, logger,
scheduler). No importa ni toca a otros plugins ni al bus directamente.

Ver `plugins/echo/` como ejemplo completo.

## Qué queda preparado para las siguientes versiones

- **LLM real**: implementa `LLMProvider` (LM Studio, llama.cpp, OpenAI, Claude,
  Ollama…) y pásalo a `LLMModule`. Nada más cambia.
- **Voz / visión**: nuevos plugins en `plugins/` que publican `speech.detected`,
  `person.detected`, etc. Los eventos ya existen en `alice/core/events.py`.
- **Herramientas reales** (shell, internet, filesystem, git…): implementa `Tool`
  y regístrala en el `ToolManager`; los permisos ya se validan.
- **Memoria persistente**: implementa los `Protocol` episódico/largo plazo con
  SQLite o una base vectorial, detrás de la misma interfaz.
