# Alice Core v1.0 — Planeación Técnica

> Documento de planeación. El objetivo de la v1.0 **no es** tener IA funcionando:
> es tener un núcleo asíncrono, tipado y desacoplado al que cualquier módulo
> futuro (voz, visión, LLM, herramientas) se conecte solo mediante eventos.

---

## 1. Decisiones de arquitectura (cerradas)

Estas decisiones se toman ahora para que la implementación no las improvise:

| Tema | Decisión | Justificación |
|---|---|---|
| Runtime | `asyncio`, un solo event loop | Un proceso, concurrencia por corrutinas; los módulos pesados futuros (Whisper, OpenCV) correrán en executors o procesos aparte y se comunicarán por eventos |
| Modelos de datos | Pydantic v2, eventos inmutables (`frozen=True`) | Validación en el borde, serializables a JSON para logging |
| Tipado | `mypy --strict` obligatorio en todo el paquete | El proyecto crecerá durante años; el tipado es el contrato |
| Cola de eventos | `asyncio.PriorityQueue` acotada | Prioridad + backpressure desde el día uno |
| Comunicación entre módulos | **Solo** eventos vía EventBus | Prohibido que un plugin importe otro plugin; se valida en revisión |
| Inyección de dependencias | El Orchestrator es la *composition root*; los plugins reciben un `PluginContext` | Nadie construye sus propias dependencias |
| Logging | `logging` stdlib con formatter JSON propio (sin dependencias extra) | Logs estructurados, un archivo rotativo en `logs/` + consola |
| Config | `pydantic-settings`, archivo `config/alice.toml` + override por variables de entorno | Reemplazable sin tocar código |
| Tests | `pytest` + `pytest-asyncio` | El bus, el scheduler y el planner son 100% testeables sin IA |
| Calidad | `ruff` (lint+format) + `mypy` en `pyproject.toml` | Un solo archivo de configuración |

Dependencias totales de la v1.0: `pydantic`, `pydantic-settings`, `pytest`, `pytest-asyncio`, `ruff`, `mypy`. Nada más.

---

## 2. Estructura de archivos

```
alice/
├── pyproject.toml            # deps, ruff, mypy strict, pytest
├── main.py                   # entrypoint: carga config, crea Orchestrator, run()
├── README.md
├── alice/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── events.py         # Event base + catálogo de eventos + EventPriority
│   │   ├── event_bus.py      # EventBus (publish/subscribe/unsubscribe, cola async)
│   │   ├── orchestrator.py   # ciclo principal, arranque/apagado ordenado
│   │   ├── scheduler.py      # tareas futuras y periódicas (asyncio, sin cron)
│   │   ├── plugin_manager.py # descubrimiento y ciclo de vida de plugins
│   │   ├── plugin.py         # ABC Plugin + PluginContext + PluginManifest
│   │   └── state.py          # StateManager (máquina de estados del sistema)
│   ├── brain/
│   │   ├── __init__.py
│   │   ├── planner.py        # Planner basado en reglas + interfaz PlanningStrategy
│   │   ├── plan.py           # modelos Plan / PlanStep / StepKind
│   │   ├── llm.py            # LLMProvider (ABC) + LLMRequest/LLMResponse + NullProvider
│   │   └── memory.py         # interfaces ShortTerm/Episodic/LongTerm + impl. en RAM
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── manager.py        # ToolManager: registro, validación, permisos, ejecución
│   │   ├── tool.py           # ABC Tool + ToolDefinition + ToolResult + Permission
│   │   └── builtin/
│   │       ├── __init__.py
│   │       └── datetime_tool.py  # herramienta demo (prueba el pipeline completo)
│   ├── logging/
│   │   ├── __init__.py
│   │   └── setup.py          # configuración de logging JSON estructurado
│   └── config/
│       ├── __init__.py
│       └── settings.py       # AliceSettings (pydantic-settings)
├── plugins/                  # carpetas de plugins descubiertas en runtime
│   └── echo/                 # plugin de ejemplo: demuestra "crear módulo = crear carpeta"
│       ├── manifest.toml
│       └── plugin.py
├── config/
│   └── alice.toml
├── logs/                     # .gitignore, se crea al arrancar
└── tests/
    ├── test_event_bus.py
    ├── test_scheduler.py
    ├── test_plugin_manager.py
    ├── test_planner.py
    ├── test_tool_manager.py
    └── test_integration.py   # escenario end-to-end sin IA
```

Nota sobre `voice/` y `vision/`: en v1.0 **no existen como código**, existirán como
plugins futuros dentro de `plugins/`. Crear carpetas vacías no aporta nada; el
plugin `echo/` demuestra el mecanismo que voz y visión usarán.

---

## 3. Contratos principales

### 3.1 Evento

```python
class EventPriority(IntEnum):
    CRITICAL = 0   # menor número = mayor prioridad en la PriorityQueue
    HIGH = 1
    NORMAL = 2
    LOW = 3

class Event(BaseModel, frozen=True):
    id: UUID                    # uuid4 automático
    timestamp: datetime         # UTC automático
    type: str                   # p.ej. "command.received"
    source: str                 # módulo emisor, p.ej. "plugin.echo"
    priority: EventPriority
    correlation_id: UUID | None # encadena eventos de un mismo flujo (trazabilidad)
    payload: dict[str, Any]
```

Catálogo inicial (constantes en `events.py`, nomenclatura `dominio.acción`):

`system.started`, `system.stopped`, `speech.detected`, `person.detected`,
`hand.detected`, `command.received`, `internet.search_requested`,
`internet.search_completed`, `tool.requested`, `tool.finished`,
`llm.requested`, `llm.finished`, `conversation.started`, `conversation.ended`,
`plan.created`.

Cada tipo de evento define además un **modelo Pydantic de payload** (p.ej.
`CommandReceivedPayload(text: str, user: str | None)`) para que los consumidores
validen en lugar de leer dicts a ciegas.

### 3.2 EventBus

```python
class EventBus:
    async def publish(self, event: Event) -> None
    def subscribe(self, event_type: str, handler: EventHandler) -> Subscription
    def unsubscribe(self, subscription: Subscription) -> None
    async def run(self) -> None      # bucle consumidor de la cola
    async def stop(self) -> None     # drena la cola y termina
```

Reglas de comportamiento:
- Cola `PriorityQueue` acotada (tamaño configurable, default 1000). Orden:
  `(priority, contador_secuencial)` → FIFO dentro de la misma prioridad.
- Si la cola está llena: se registra un log `WARNING` y se descarta el evento
  de prioridad `LOW` (política configurable). Nunca se bloquea al publicador.
- `subscribe("*")` permite suscripción comodín (el logger la usa).
- Un handler que lanza excepción **no tumba el bus**: se captura, se loguea con
  el evento que la causó, y el bus continúa.
- `Subscription` es un handle opaco; con él se desuscribe (no por referencia a función).

### 3.3 Plugin

```python
class Plugin(ABC):
    manifest: PluginManifest                     # name, version, subscribes, description
    async def initialize(self, ctx: PluginContext) -> None
    async def start(self) -> None
    async def stop(self) -> None
    async def handle_event(self, event: Event) -> None

class PluginContext:                             # lo ÚNICO que un plugin conoce del sistema
    async def publish(self, event: Event) -> None
    def get_config(self, key: str) -> Any
    def get_logger(self) -> Logger
    def scheduler(self) -> SchedulerFacade
```

Descubrimiento: `PluginManager` escanea `plugins/*/`; cada carpeta con
`manifest.toml` + `plugin.py` (que expone una clase `AlicePlugin(Plugin)`) se
carga con `importlib`. El manifest declara a qué eventos se suscribe; el
manager hace las suscripciones — el plugin no toca el bus directamente, solo
publica por su contexto. Un plugin que falla al cargar se loguea y **no impide
el arranque del resto**.

### 3.4 Planner (basado en reglas, v1)

```python
class PlanningStrategy(Protocol):
    def plan(self, command: CommandReceivedPayload) -> Plan

class Plan(BaseModel):
    steps: list[PlanStep]        # ejecutados en orden por el propio planner
    requires_llm: bool

class PlanStep(BaseModel):
    kind: StepKind               # USE_TOOL | CALL_LLM | RESPOND
    tool_name: str | None
    params: dict[str, Any]
```

- Escucha `command.received`, produce un `Plan`, lo registra en el log
  (**cada decisión queda trazada**: comando de entrada, regla que aplicó, plan resultante).
- v1: reglas por patrones/palabras clave (hora/fecha → tool datetime; "busca en
  internet" → tool internet + LLM; "apaga X" → tool shell; sin regla → LLM directo).
- Emite `tool.requested` o `llm.requested` según el paso; escucha
  `tool.finished` / `llm.finished` para avanzar el plan (usa `correlation_id`).
- La estrategia es intercambiable: en el futuro un `LLMPlanningStrategy`
  reemplaza a `RuleBasedStrategy` sin tocar nada más.

### 3.5 LLM (solo interfaz)

```python
class LLMProvider(ABC):
    async def generate(self, request: LLMRequest) -> LLMResponse
    async def health_check(self) -> bool
```

`LLMRequest` (messages, system, max_tokens, temperature) y `LLMResponse`
(text, usage, model) son modelos propios de Alice — **el resto del sistema
nunca ve tipos de OpenAI/Anthropic/Ollama**. v1 incluye solo `NullLLMProvider`
(responde un texto fijo, útil para tests e integración). Un módulo `LLMModule`
escucha `llm.requested`, llama al provider configurado y emite `llm.finished`.

### 3.6 Memoria (interfaces preparadas, sin bases vectoriales)

```python
class ShortTermMemory(Protocol):     # RAM: deque acotada de turnos + eventos recientes
class EpisodicMemory(Protocol):      # add_episode / query_by_date / daily_summary
class LongTermMemory(Protocol):      # get/set de hechos: preferencias, personas, proyectos
```

v1 implementa solo `InMemoryShortTermMemory`; episódica y de largo plazo quedan
como Protocols + implementaciones no-op documentadas. El backend de
persistencia (SQLite, vectorial) será un detalle detrás del Protocol.

### 3.7 Tool Manager

```python
class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: type[BaseModel]      # schema de params = modelo Pydantic
    permissions: set[Permission]     # READ_SYSTEM | WRITE_SYSTEM | NETWORK | SHELL ...

class Tool(ABC):
    definition: ToolDefinition
    async def execute(self, params: BaseModel) -> ToolResult
```

Flujo: `tool.requested` → ToolManager valida (¿existe? ¿params válidos contra
el schema? ¿permisos concedidos en config?) → ejecuta con timeout → publica
`tool.finished` con `ToolResult(success, output, error)`. Nada ejecuta
herramientas directamente; ni el planner ni (en el futuro) el LLM.
v1 incluye una sola herramienta real e inofensiva: `datetime` (devuelve
fecha/hora), suficiente para probar el pipeline completo.

### 3.8 Scheduler

```python
class Scheduler:
    def schedule_once(self, delay: timedelta, callback: AsyncCallback) -> JobHandle
    def schedule_interval(self, every: timedelta, callback: AsyncCallback) -> JobHandle
    def cancel(self, handle: JobHandle) -> None
```

Basado en `asyncio` (sin cron, sin threads). Los jobs típicos publican eventos.
Excepciones en un job se loguean sin matar el scheduler; un job periódico que
falla sigue programado.

### 3.9 Orchestrator y arranque

`main.py` → carga settings → configura logging → crea `Orchestrator`.

Orden de arranque: EventBus → Scheduler → StateManager → módulos internos
(Planner, ToolManager, LLMModule, Memory) → PluginManager (descubre y arranca
plugins) → publica `system.started`.
Apagado (SIGINT/Ctrl+C): orden inverso, publica `system.stopped`, drena la
cola, cancela jobs, `stop()` de cada plugin con timeout. El Orchestrator solo
compone y supervisa: **cero lógica de negocio**.

`StateManager`: máquina de estados `STARTING → RUNNING → STOPPING → STOPPED`,
consultable, y emite eventos de sistema en las transiciones.

---

## 4. Fases de implementación (orden para Opus)

Cada fase termina con sus tests en verde y `mypy --strict` limpio.

1. **Fase 0 — Esqueleto**: `pyproject.toml` (deps + ruff + mypy strict + pytest),
   paquete `alice/`, settings, setup de logging JSON.
2. **Fase 1 — Eventos + EventBus**: `events.py`, `event_bus.py`, tests de
   prioridad, wildcard, unsubscribe, handler que lanza excepción, cola llena.
3. **Fase 2 — Scheduler**: once/interval/cancel, tests con tiempos cortos.
4. **Fase 3 — Sistema de plugins**: ABC, contexto, manifest, descubrimiento por
   carpeta, plugin `echo/` de ejemplo, tests de ciclo de vida y de plugin roto.
5. **Fase 4 — Orchestrator + State + main.py**: el sistema arranca, loguea
   `system.started`, el plugin echo responde a un evento, Ctrl+C apaga limpio.
6. **Fase 5 — Brain**: modelos de Plan, planner con reglas + tests de decisión,
   interfaz LLM + NullProvider + LLMModule, interfaces de memoria + short-term.
7. **Fase 6 — Tools**: Tool ABC, ToolManager con validación/permisos/timeout,
   herramienta `datetime`, tests (params inválidos, permiso denegado, timeout).
8. **Fase 7 — Integración**: test end-to-end del escenario de aceptación,
   README con cómo arrancar y cómo crear un plugin.

## 5. Criterios de aceptación de la v1.0

1. `python main.py` arranca, loguea `system.started` en JSON y queda corriendo;
   Ctrl+C produce un apagado ordenado con `system.stopped`.
2. Existe el plugin `plugins/echo/`; borrar o añadir su carpeta cambia lo que
   se carga **sin tocar ninguna otra línea de código**.
3. Test de integración: se publica `command.received` con "¿qué hora es?" →
   el planner decide (y loguea) *usar tool datetime, no llamar LLM* → el
   ToolManager valida y ejecuta → llega `tool.finished` con la hora → el flujo
   completo queda encadenado por `correlation_id` en los logs.
4. Mismo test con "busca en internet cómo funciona MQTT" → el plan generado
   incluye tool internet + LLM (aunque la tool no exista aún: el plan se crea
   y el ToolManager responde `tool.finished` con error "tool desconocida").
5. Ningún módulo fuera de `core/` importa otro módulo de negocio: `brain`,
   `tools` y `plugins` solo importan de `core` (eventos/contratos).
6. `mypy --strict`, `ruff check` y `pytest` pasan sin errores.

## 6. Fuera de alcance (explícito)

Whisper/voz, OpenCV/visión, cualquier proveedor LLM real, bases vectoriales,
SQLite, herramientas reales (shell, internet, FTP...), UI. Todo eso entra
después **como plugins/providers**, sin modificar el núcleo.
