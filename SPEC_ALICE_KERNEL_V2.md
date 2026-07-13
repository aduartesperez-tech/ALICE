# Alice Kernel — Architecture Specification v2.0

> Documento de planeación. Define los cinco pilares cognitivos sobre el núcleo
> v1.0 ya implementado y verificado. **No se escribe código con este documento
> todavía**: se cierra la arquitectura para que las versiones v1.1 → v1.4
> crezcan de forma ordenada durante los próximos años.

---

## 0. Punto de partida: qué existe y qué falta

**Existe (v1.0, verificado):** EventBus con prioridades, Scheduler, sistema de
plugins por carpeta, Orchestrator, StateManager, Planner por reglas, interfaz
LLM (NullProvider), ToolManager con permisos/timeout, memoria short-term en
RAM, logging JSON con `correlation_id`. 33 tests, mypy strict, ruff.

**Huecos reales del código actual que esta spec resuelve:**

| Hueco | Pilar que lo resuelve |
|---|---|
| El Planner solo despacha el **primer** paso del plan; nadie avanza los pasos al llegar `tool.finished`/`llm.finished`. El paso `RESPOND` no hace nada. | **Action Executor** |
| Alice no tiene entrada ni salida reales: no hay forma de hablarle ni de que responda. | **Console plugin (I/O)** |
| La memoria episódica y de largo plazo son no-op: nada se recuerda entre sesiones. | **Persistencia (SQLite)** |
| El Planner decide sin contexto: no sabe si hay conversación activa, quién está presente, qué pasó hace un minuto. | **CognitiveState** |
| Cuando existan voz/visión, TODO evento llegará al Planner sin filtro. | **Attention Manager** |
| Alice no registra lo que "piensa"; solo lo que hace. | **Internal Thoughts** |

**Terminología:** el proyecto pasa a llamarse **Alice Kernel** en la
documentación (es un microkernel: scheduler, IPC por eventos, loader de
plugins, state machine, drivers = plugins, user space = voz/visión/tools).
El paquete Python sigue siendo `alice/` — renombrarlo es churn sin beneficio.

---

## 1. El Cognitive Loop (el corazón)

El ciclo que convierte "reaccionar" en "pensar":

```
   PERCIBIR            ATENDER              ACTUALIZAR           PLANIFICAR          ACTUAR              REFLEXIONAR
┌─────────────┐   ┌──────────────────┐   ┌────────────────┐   ┌─────────────┐   ┌─────────────────┐   ┌──────────────┐
│ Percepción  │──>│ AttentionManager │──>│ CognitiveState │──>│   Planner   │──>│ Action Executor │──>│   Thoughts   │
│ voz/visión/ │   │ ¿merece atención?│   │ estado mental  │   │ ¿qué hacer? │   │ ¿quién lo hace? │   │ ¿qué aprendí?│
│ sistema     │   │ (filtra/prioriza)│   │ (solo datos)   │   │ (lee estado)│   │ tools/llm/mem/  │   │ → memoria    │
└─────────────┘   └──────────────────┘   └────────────────┘   └─────────────┘   │ sched/cognition │   └──────────────┘
                                                                                └─────────────────┘
```

Todo sigue siendo eventos sobre el mismo EventBus. Ningún pilar importa a otro;
cada flecha es un tipo de evento. El Orchestrator sigue sin lógica de negocio.

---

## 2. Los cinco pilares

### 2.1 Action Executor (`brain/executor.py`) — PRIORIDAD 1

**Por qué primero:** completa la ejecución de planes que hoy está a medias, y
es la pieza de la que dependen los demás pilares ("recuérdame X" = acción sobre
el scheduler; "apréndete Y" = acción sobre la memoria; "modo profesor" = acción
sobre el estado cognitivo).

**Concepto:** el Planner deja de emitir `tool.requested`/`llm.requested`
directamente. Produce un `Plan` (lista de `Action`) y emite `plan.created`.
El Executor lo ejecuta **paso a paso**, avanzando cuando llega el resultado de
cada paso (encadenado por `correlation_id`), y pasando el output de un paso
como input del siguiente.

**Tipos de acción (v2, reemplaza a `StepKind`):**

| ActionKind | Destino | Ejemplo |
|---|---|---|
| `use_tool` | ToolManager | "¿qué hora es?" |
| `call_llm` | LLMModule | redactar respuesta con contexto |
| `remember` | Memory | "el usuario parece cansado" |
| `schedule` | Scheduler | "recuérdame llamar a mamá en 2 horas" |
| `set_state` | CognitiveState | "cambia a modo profesor" |
| `respond` | canal de salida (evento `response.ready`) | entregar la respuesta al usuario |

**Contrato (conceptual):**

```python
class Action(BaseModel):
    kind: ActionKind
    target: str            # "datetime", "llm", "episodic", "scheduler"...
    params: dict[str, Any]

class Plan(BaseModel):
    goal: str              # qué se quiere lograr (para logs y thoughts)
    actions: list[Action]  # ejecutadas EN ORDEN por el Executor
    rule: str
```

**Reglas:**
- El Executor mantiene los planes en curso indexados por `correlation_id`.
- Un paso que falla aborta el plan y emite `plan.failed` (con el error); un
  plan completado emite `plan.completed`.
- Timeout por plan completo (no solo por tool) para que un plan nunca quede
  colgado en memoria.
- El Executor NO decide nada: ejecuta lo que el Planner decidió. La
  inteligencia sigue en el Planner; la ejecución, en el Executor.

### 2.2 CognitiveState (`brain/cognition.py`) — PRIORIDAD 3

**Concepto:** el estado mental de Alice. **Solo datos, cero lógica de
decisión.** Se actualiza escuchando eventos; se lee de forma síncrona (es el
único módulo con lectura directa permitida, igual que la memoria, porque el
Planner lo necesita en el instante de decidir).

```python
class CognitiveState(BaseModel):
    attention: str | None          # a qué está atendiendo ahora
    conversation: ConversationInfo | None   # con quién habla, desde cuándo
    current_goal: str | None       # objetivo activo (del último plan)
    active_user: str | None        # usuario presente identificado
    environment: dict[str, Any]    # snapshot: hay gente, hora local, etc.
    mode: str                      # "normal" | "profesor" | ... (set_state)
    drives: Drives                 # ver 2.5
    last_seen: dict[str, datetime] # persona -> última vez vista
    last_heard: datetime | None    # última vez que oyó algo
```

**Reglas:**
- Se actualiza SOLO por eventos (`person.detected` → `last_seen`;
  `conversation.started` → `conversation`; `plan.created` → `current_goal`...).
- Los consumidores (Planner, Attention) reciben una **vista de solo lectura**.
- Prohibido meter condicionales de negocio aquí. "¿Hace cuánto no veo a X y
  debería saludar?" es una regla del Planner que LEE `last_seen`; no un método
  de CognitiveState.
- El escenario guía: `person.detected` → el Planner pregunta al estado
  (¿conversación activa? no; ¿conozco a la persona? sí; ¿hace cuánto? 3h) →
  plan: saludar. Todo eso son reglas del Planner con el estado como entrada.

### 2.3 Attention Manager (`brain/attention.py`) — PRIORIDAD 4

**Concepto:** filtro entre la percepción cruda y la cognición. Sin él, cuando
existan voz+visión+sensores, cada ruido dispararía al Planner.

**Mecánica:** se suscribe a los eventos de percepción (`speech.detected`,
`person.detected`, `hand.detected`, futuros sensores). Calcula una **salience**
(relevancia) y decide: ignorar, actualizar solo el estado, o promover a
`attention.focused` (que es lo que el Planner escucha en lugar de la
percepción cruda).

**Score de salience (v1 del algoritmo, todo configurable):**

```
salience = novedad          (¿cuánto hace que no ocurre este tipo de evento?)
         + relevancia_goal  (¿tiene que ver con current_goal?)
         + presencia        (¿hay un usuario activo esperando algo?)
         + prioridad_evento (CRITICAL siempre pasa)
         + drives           (curiosity sube la novedad; urgency sube todo)
```

- Por debajo del umbral: el evento solo actualiza CognitiveState (Alice "lo ve"
  pero no "le presta atención").
- Por encima: emite `attention.focused` con el evento original embebido.
- `command.received` (el usuario le habla directamente) **siempre** pasa.

**Regla de honestidad de ingeniería:** este módulo NO se implementa hasta que
exista al menos una fuente de percepción real (consola cuenta como mínimo;
visión/voz es lo ideal). Se define el contrato ahora; se construye cuando haya
algo real que filtrar. Un filtro sin tráfico es código muerto no testeable.

### 2.4 Internal Thoughts — PRIORIDAD 5

**Concepto:** conocimiento que Alice genera y guarda pero nunca dice. No es un
módulo grande: es un tipo de evento + persistencia.

- Nuevo evento `thought.created` con payload
  `Thought(text, about, kind, confidence)` — `kind`: observación, hipótesis,
  intención de seguimiento.
- Lo emiten el Planner (al decidir) y el Executor (al completar/fallar planes).
  Ejemplo: usuario dice "estoy muy cansado" → plan: responder con empatía +
  `remember` → thought: "El usuario parece cansado. Observar si continúa."
- Los thoughts van a **memoria episódica** vía una acción `remember` implícita.
- **Regla dura:** ningún camino de código lleva un thought al canal de salida.
  Los thoughts solo son legibles por el Planner (contexto), el LLM (prompt de
  contexto futuro) y los logs.

**Dependencia:** requiere memoria episódica persistente (hoy es no-op). Por eso
la persistencia SQLite entra en el roadmap ANTES que los thoughts.

### 2.5 Drives (`drives`, dentro de CognitiveState) — PRIORIDAD 6

Lo que llamabas `emotion.py`, renombrado a lo que realmente es: **pesos de
planificación**, no emociones.

```python
class Drives(BaseModel):
    curiosity: float = 0.5    # sube la salience de lo novedoso
    urgency: float = 0.0      # acorta planes, sube prioridades de eventos
    confidence: float = 0.5   # bajo → el planner prefiere preguntar antes de actuar
    importance: float = 0.5   # peso del goal actual frente a interrupciones
```

- Viven dentro de CognitiveState (no módulo aparte todavía).
- Los leen el Attention Manager (modulan salience) y el Planner (modulan
  reglas). Los modifican eventos y acciones `set_state`.
- Se promocionan a `emotion.py` propio SOLO si algún día tienen dinámica
  interna (decaimiento temporal, interacciones entre drives). No antes.

---

## 3. Prerequisitos que no son pilares (pero sin ellos nada se puede probar)

1. **Console plugin (`plugins/console/`)** — la boca y el oído mínimos:
   lee stdin → publica `command.received`; escucha `response.ready` → imprime.
   Convierte a Alice en algo con lo que se puede interactuar HOY. Es un plugin
   normal: cero cambios en el núcleo (valida la arquitectura de plugins).
2. **Persistencia de memoria (SQLite)** — implementar `EpisodicMemory` y
   `LongTermMemory` detrás de los Protocols ya definidos. SQLite stdlib
   (`sqlite3`), sin dependencias nuevas, sin vectores todavía.
3. **Primer LLMProvider real (Ollama o LM Studio, HTTP local)** — para que
   `call_llm` produzca respuestas de verdad. Detrás de la interfaz existente;
   el resto del sistema no se entera.

---

## 4. Nuevos eventos del catálogo

```
plan.completed        # el Executor terminó todos los pasos de un plan
plan.failed           # un paso falló; payload incluye paso y error
response.ready        # hay una respuesta para el usuario (la consume el canal de salida)
attention.focused     # el Attention Manager promovió un evento de percepción
thought.created       # pensamiento interno (nunca sale al usuario)
goal.set / goal.done  # ciclo de vida del objetivo actual
state.changed         # CognitiveState cambió (mode, drives, active_user)
```

---

## 5. Roadmap por versiones (orden de implementación)

El orden optimiza por **testabilidad**: cada versión produce algo que se puede
ejecutar y sentir, y ninguna construye sobre humo.

### v1.1 — El ciclo se cierra (Action Executor + consola)
- Action Executor con los 6 ActionKind; el Planner produce `Plan` v2 y deja de
  emitir tool/llm directamente; avance de pasos por `correlation_id`.
- Console plugin (entrada/salida por terminal).
- **Aceptación:** escribes "¿qué hora es?" en la terminal y Alice responde la
  hora, sin LLM, con el flujo completo trazado. "Recuérdame estirar en 10
  segundos" crea una acción `schedule` que dispara a los 10s.

### v1.2 — Alice recuerda y habla de verdad (persistencia + LLM real)
- `SqliteEpisodicMemory` y `SqliteLongTermMemory` detrás de los Protocols.
- `OllamaProvider` (o LM Studio) detrás de `LLMProvider`.
- **Aceptación:** matas el proceso, lo relanzas, y Alice conserva lo aprendido.
  Un comando sin regla ("cuéntame un chiste") obtiene respuesta real del LLM.

### v1.3 — Alice tiene estado mental (CognitiveState + Attention)
- `brain/cognition.py` con vista de solo lectura para el Planner.
- Planner v2: reglas que consultan el estado (conversación activa, last_seen,
  mode) — aquí entra el escenario "llegaste → salúdame si hace 3h que no te veo"
  (con la consola simulando `person.detected` hasta que exista visión).
- Attention Manager con salience v1, filtrando percepción simulada.
- **Aceptación:** el mismo evento produce planes distintos según el estado
  (con conversación activa vs. sin ella). Eventos LOW repetidos no llegan al
  Planner; un `command.received` siempre llega.

### v1.4 — Alice reflexiona (Thoughts + Drives)
- `thought.created` emitido por Planner y Executor; persistido en episódica.
- Drives dentro de CognitiveState, modulando salience y reglas.
- **Aceptación:** "estoy muy cansado" → respuesta empática + thought guardado
  que NUNCA aparece en el canal de salida; consultable en la memoria episódica.
  Con curiosity alta, un evento novedoso pasa el filtro; con baja, no.

### Después (v2.x, fuera de esta spec)
Voz (Whisper como plugin), visión (OpenCV como plugin), memoria vectorial,
LLMPlanningStrategy (el LLM como planner con las reglas como fast-path),
multi-usuario, seguridad de tools con confirmación.

---

## 6. Guardarraíles (lo que esta spec prohíbe)

1. **CognitiveState sin lógica:** si un `if` de negocio aparece en
   `cognition.py`, va al Planner. El estado se lee, no opina.
2. **Thoughts nunca salen:** ningún camino de `thought.*` puede terminar en
   `response.ready`. Se valida con un test dedicado.
3. **El Executor no decide:** ejecuta planes; no los modifica ni los crea.
4. **Todo sigue siendo eventos:** los pilares nuevos no ganan imports directos
   entre sí. Las dos únicas lecturas síncronas permitidas: CognitiveState
   (vista read-only) y Memory. Todo lo demás, por el bus.
5. **Nada se implementa sin poder probarse:** Attention espera a tener
   percepción; Thoughts esperan a tener persistencia. El orden del roadmap no
   es negociable por entusiasmo.
6. **Dependencias:** v1.1–v1.4 añaden como mucho UNA dependencia (el cliente
   HTTP para Ollama, si `urllib` no basta). SQLite es stdlib.

---

## 7. Riesgos conocidos

| Riesgo | Mitigación |
|---|---|
| CognitiveState se convierte en objeto dios | Guardarraíl 1 + revisión de imports en cada PR |
| Salience mal calibrada (Alice ignora todo o atiende todo) | Umbrales en `alice.toml`, logs de cada decisión de atención con su score |
| Planes colgados esperando un `tool.finished` que no llega | Timeout por plan en el Executor + `plan.failed` |
| El LLM real es lento y bloquea la sensación de vida | El bus ya es async; `call_llm` no bloquea otros planes; prioridades del bus |
| Sobre-diseño cognitivo antes de tener usuarios del sistema | El roadmap mete consola+LLM+memoria (valor tangible) antes que atención/drives |
```
