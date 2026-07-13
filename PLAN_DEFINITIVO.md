# Alice — Plan definitivo (agente de IA local)

> Generado el 2026-07-13. Consolida y reemplaza como hoja de ruta operativa a
> `ROADMAP.md`, `SPEC_ALICE_KERNEL_V2.md` y `PLAN_V1_2.md` (que quedan como
> archivo histórico). Parte de una revisión del código real, no de las notas.
> Framework: **qué convierte a Alice en un agente**, no en un enrutador de chat.

---

## 0. Estado real hoy (verificado contra el código, 2026-07-13)

**Funciona:**

- **Núcleo por eventos**: EventBus con prioridades, Orchestrator (composition root
  sin lógica), Scheduler, StateManager. Ningún módulo importa a otro.
- **Ciclo de plan cerrado**: `command.received` → Planner → `plan.created` →
  ActionExecutor (avanza acción a acción, encadena por `correlation_id`) →
  `response.ready`. El Executor no decide; solo actúa.
- **Planner con 3 estrategias** (regex primero en todas): `rules`, `hybrid`
  (clasificador de intención cerrado), `tool_calling` (**activa** — el LLM elige
  del catálogo real de tools por function calling). Añadir una tool la hace usable
  sin tocar el planner.
- **Tools reales (3)**: `datetime`, `recall_memory`, `camera` (consulta el
  `/state` del plugin de visión por HTTP; degrada si la visión está apagada).
- **Memoria**: SQLite (`data/alice.db`) episódica + largo plazo; short-term en RAM
  cableada al contexto del LLM.
- **LLM**: proveedor OpenAI-compatible → LM Studio (`127.0.0.1:1234`). Si el
  servidor está caído, degrada a respuesta en crudo (no falla).
- **Voz** (`voice_web`, :8756): web + micrófono (faster-whisper CPU) + TTS del navegador.
- **Visión** (`vision`, :8757): caras (InsightFace), presencia, gestos (MediaPipe);
  hilo daemon con stream MJPEG; saluda por tu nombre al reconocerte.

**Estado de los tests (verificado hoy):** todos los archivos de test pasan y
salen limpios **excepto `tests/test_orchestrator.py`, que se cuelga** (timeout).
Causa raíz: ese test arranca el directorio `plugins/` **real**, y `start_all()`
arranca *todos* los plugins descubiertos — incluidos `vision` (abre la webcam en
un hilo daemon + carga InsightFace) y `voice_web` (servidor web + Whisper). Esos
plugins de hardware bloquean el arranque/apagado durante un test unitario. Cuando
la nota de "87 tests en verde" se escribió, esos plugins pesados no existían o no
se auto-arrancaban. **Es una regresión de aislamiento de tests, no un bug del
núcleo.** → se salda en la Fase 0.

### Deuda técnica conocida

1. **Tests acoplados a hardware** (arriba): `test_orchestrator` boota vision+voz.
2. **No hay forma de desactivar un plugin**: `start_all()` arranca todo lo que
   descubre; no hay flag `enabled` en el manifest ni allowlist en config.
3. **Reglas regex muertas**: el planner tiene reglas `internet` y `shell`
   (`planner.py:216-238`) que apuntan a tools **no implementadas** → si disparan,
   el plan falla. Quitar o implementar.
4. **Hecho basura en la BD**: `episodes` id 36, `user_fact` `{"text":"trabajo de
   noche"}` (alucinación de una prueba temprana). Borrar.
5. **`tool_calling` es de una sola vuelta**, no un bucle agente (ver Fase 1).
6. **mypy no cubre `plugins/`** (solo `alice/`).

---

## 1. El marco: qué hace de Alice un *agente*

Un agente de IA no es un chatbot con herramientas: es un sistema que **percibe,
razona en bucle, actúa sobre el mundo, observa el resultado y repite hasta cumplir
un objetivo**, con memoria y criterio propio de cuándo parar. Estas son las nueve
propiedades de un agente y dónde está Alice en cada una:

| # | Propiedad de un agente | Estado en Alice | Se aborda en |
|---|---|---|---|
| 1 | **Percepción** (entradas del entorno) | ✅ voz, visión, consola | — |
| 2 | **Bucle agente** (percibir→razonar→actuar→observar→repetir) | ❌ hoy es *selección de una sola vuelta* | **Fase 1** |
| 3 | **Uso de herramientas** reales | ⚠️ solo 3 tools | Fases 2–3 |
| 4 | **Memoria** (corto/largo plazo, recuperación) | ⚠️ SQLite sí; recuperación semántica y consolidación no | Fase 5 |
| 5 | **Planificación / descomposición de objetivos** | ⚠️ planner reactivo, no descompone metas | Fase 1 (implícito en el bucle) |
| 6 | **Modelo del mundo / estado mental** | ❌ `CognitiveState` no existe | **Fase 4** |
| 7 | **Autonomía / proactividad** (actuar sin que se lo pidan) | ❌ solo reacciones push (saludo) | Fase 4 |
| 8 | **Seguridad / barandillas** (permisos, confirmación) | ⚠️ enum de permisos sí; flujo de confirmación no | Fase 2 |
| 9 | **Reflexión / aprendizaje** (pensar sobre lo hecho) | ❌ `Thoughts` no existe | Fase 4 |

**Conclusión del marco:** la brecha más grande entre "Alice hoy" y "un agente"
es la **#2, el bucle agente**. Hoy el `ToolCallingStrategy` elige tools **una
vez**, las ejecuta y narra. Un agente real *itera*: mira el resultado de una tool,
decide el siguiente paso (quizá otra tool, quizá terminar), y sigue hasta cumplir
el objetivo. Por eso el bucle agente es la Fase 1: desbloquea todo lo demás
(scripts, control del PC, visión avanzada) sin reescribir el planner cada vez.

---

## 2. Plan definitivo por fases

Orden: **0 → 1 → 2 → 3 → 4 → 5 → 6**. La Fase 0 sanea la base (obligatoria antes
de construir encima). La Fase 1 es el salto conceptual a "agente". Las 2–3 son
capacidades (lo que pediste: scripts + control del PC). La 4 es la mente (pilares
de la SPEC). La 5–6 es percepción avanzada y pulido.

### Fase 0 — Sanear la base (bloqueante) — *pequeña, hazla ya*

**Objetivo:** suite de tests verde y determinista, sin hardware; deuda muerta fuera.

- **Aislar los tests del hardware**: `test_orchestrator` debe apuntar
  `plugins_dir` a un directorio de fixture con solo `echo`/`console`, **no** al
  `plugins/` real. (O introducir el flag de plugins de abajo y desactivar
  vision/voice en el entorno de test.)
- **Flag de plugins activables** (también arregla la deuda #2): campo
  `enabled = true|false` en cada `manifest.toml` **y/o** allowlist
  `plugins.enabled = [...]` en `alice.toml`; `start_all()` respeta el flag. Esto
  permite correr Alice sin webcam ni micrófono cuando toque.
- **Quitar las reglas regex muertas** `internet`/`shell` del planner (o dejarlas
  detrás de "la tool existe"), para que ningún plan falle por una tool ausente.
- **Limpiar la BD**: borrar `episodes` id 36 (`user_fact` "trabajo de noche").
- **Extender mypy a `plugins/`** (deuda #6).

**Criterio de aceptación:** `pytest -q` termina en verde y **sale solo** (sin
cuelgue), sin abrir webcam ni cargar Whisper. `ruff` y `mypy` (incl. `plugins/`)
en verde.

### Fase 1 — Bucle agente (el LLM razona en varias vueltas) — *el corazón*

**Objetivo:** que Alice encadene **tool → observar resultado → decidir siguiente
paso → … → responder**, no una sola selección. Es lo que la convierte en agente.

- Nueva estrategia/ejecución **`AgentLoopStrategy`** (o evolución de
  `ToolCallingStrategy`): mantiene una conversación con el LLM en la que, en cada
  vuelta, el modelo puede (a) pedir una o más tools, o (b) dar la respuesta final.
  El resultado de cada tool vuelve al LLM como mensaje `tool`/`observation`, y el
  bucle continúa hasta que el modelo responde sin pedir tools o se alcanza
  `max_iters` (p.ej. 6, cortafuegos anti-bucle-infinito).
- Encaja en el diseño actual: el `ActionExecutor` ya avanza planes por eventos y
  ya trae `observations` de vuelta; se generaliza para **re-preguntar al LLM** con
  las observaciones acumuladas en lugar de solo narrar y cerrar. El bucle vive en
  el planner/estrategia (decisión) o en un mini-orquestador de turno; el Executor
  sigue solo ejecutando.
- Config: `planner.max_agent_iters`. Regex sigue primero (gratis) para lo trivial.
- Recomendado: `glm-4.7-flash` en LM Studio (llama-3.2-3b es ruidoso en tool-use).

**Criterio de aceptación:** "¿qué hora es y qué sabes de mí?" ejecuta `datetime` +
`recall_memory` y **narra ambos** en una respuesta; una tarea que necesita el
resultado de la tool A para elegir la tool B (p.ej. "mira quién hay y salúdalo por
su nombre") encadena A→B en el mismo turno sin tocar el prompt.

### Fase 2 — Ejecutar scripts, con seguridad por diseño

**Objetivo:** "Alice, corre el backup" — sin abrir la puerta a comandos destructivos.

**Principio: catálogo curado antes que shell libre.**

- **`ScriptTool`**: carpeta `scripts/` con manifests por script (nombre,
  descripción, ¿requiere confirmación?). Alice los **lista** y los **ejecuta por
  nombre**, con timeout y captura de salida → observación → narración. Activa el
  permiso `shell` (ya en el enum `Permission`, hoy no otorgado en `alice.toml`).
- **Flujo de confirmación** (barandilla #8, reutilizable en la Fase 3): par de
  eventos nuevo `confirmation.requested` / `confirmation.granted`. Para acciones
  que mutan el sistema, Alice dice "¿ejecuto X? sí/no" y **espera** tu respuesta
  (voz o web) antes de correr nada.
- **Opcional** `ShellTool`: allowlist de binarios + denylist dura (format, rm -rf,
  regedit…), siempre tras confirmación. Solo después de probar el nivel curado.

**Criterio de aceptación:** "ejecuta el script de limpieza" lo corre y narra el
resultado; "borra system32" se rechaza; un comando no listado pide confirmación.

### Fase 3 — Control de la computadora (gradual, por riesgo)

**Objetivo:** Alice como control por voz del PC. Todo son tools normales → con el
bucle agente (Fase 1) el LLM las usa solo, conversacionalmente. De menos a más riesgo:

1. **`app_launcher`** (bajo): abrir/cerrar apps ("abre el navegador").
2. **`system_control`** (medio): volumen/silencio (pycaw), brillo, bloquear
   pantalla, capturas. Apagar/reiniciar/suspender **siempre con confirmación**
   (flujo de la Fase 2).
3. **`window_manager`** (medio): listar/enfocar/minimizar ventanas (pygetwindow).
4. **`keyboard_mouse`** (alto, al final): teclado/ratón (pyautogui) solo dentro de
   un "modo control" explícito, con failsafe de esquina.

**Antes de codificar:** verificar compatibilidad de pycaw/pygetwindow/pyautogui
con **Python 3.14**.

**Criterio de aceptación:** "sube el volumen y abre Spotify" funciona por voz;
"apaga el PC" pide confirmación y solo procede con un "sí".

### Fase 4 — La mente: estado, atención, reflexión, impulsos (pilares SPEC v2)

**Objetivo:** que Alice deje de ser puramente reactiva y tenga modelo del mundo y
proactividad. Son los pilares que faltan de `SPEC_ALICE_KERNEL_V2.md`.

- **`CognitiveState`** (`brain/cognition.py`) — propiedad de agente #6: estado
  mental como datos (¿conversación activa?, ¿quién está presente?, ¿qué pasó hace
  un minuto?). El Planner lo lee para decidir con contexto. La acción `SET_STATE`
  del Executor hoy es un stub esperando esto (`executor.py:135`).
- **`AttentionManager`** (`brain/attention.py`): filtra/prioriza percepciones antes
  del Planner (con voz+visión, *todo* evento llega hoy sin filtro).
- **`Internal Thoughts`** — propiedad #9: Alice registra lo que "piensa/aprende",
  no solo lo que hace → a memoria episódica (`kind: thought`).
- **`Drives`** + **proactividad** — propiedad #7: "buenos días" al detectarte la
  primera vez del día, resumen diario (el stub `daily_summary` ya existe),
  recordatorios inteligentes.

### Fase 5 — Percepción y memoria avanzadas

- **`describe_scene`**: capturar el frame de la webcam y mandarlo a un modelo
  multimodal en LM Studio (tienes `qwen2-vl-2b-instruct`) → "¿qué estoy
  sosteniendo?". Con el bucle agente, encadena con reconocimiento de caras.
- **Enrolamiento conversacional**: Alice ve a un desconocido y pregunta "no te
  conozco, ¿cómo te llamas?" y lo enrola sola.
- **Recuperación de memoria semántica** (propiedad #4): embeddings + búsqueda por
  similitud sobre la memoria episódica, en vez de solo lectura por `kind`.
  Consolidación/reflexión: resumir episodios viejos en hechos de largo plazo.

### Fase 6 — Pulido

- **Wake word** local ("oye Alice") para hablarle sin tocar nada.
- **Streaming de respuestas**: el TTS empieza a hablar antes de que el LLM termine.
- **Multi-usuario**: perfiles de memoria y preferencias por cara reconocida.

---

## 3. Decisiones de arquitectura que este plan respeta

- **Todo pasa por el bus**: cada capacidad nueva es *tools + eventos*, nunca
  imports cruzados entre módulos.
- **Las tools no producen prosa**: devuelven observaciones estructuradas; el LLM
  narra. La IA es la única capa de presentación.
- **Permisos explícitos**: nada con `shell`/`write_system` corre sin activarlo en
  `alice.toml`; lo destructivo pide confirmación en el momento.
- **Degradación**: si un modelo o tool falla, Alice responde igual (aunque sea en
  crudo). Nunca se queda muda.
- **El Planner decide, el Executor actúa, el Orchestrator solo compone.**

---

## 4. Lo primero que haría (resumen accionable)

1. **Fase 0** ya: aislar `test_orchestrator` del hardware + flag de plugins
   activables → suite verde que sale sola. Es media hora y desbloquea CI/confianza.
2. **Fase 1**: el bucle agente. Es el cambio que hace que "no veas que use las
   tools" desaparezca de verdad y que Alice se sienta un agente, no un router.
3. A partir de ahí, capacidades (2–3) y mente (4) en ese orden.

---

## Anexo A — Contratos de los pilares cognitivos (detalle de la Fase 4)

> Rescatado de `SPEC_ALICE_KERNEL_V2.md` (archivada). El pilar **Action Executor**
> ya está implementado; lo que sigue son los **cuatro pilares que faltan**, con su
> contrato cerrado, para no rediseñarlos cuando llegue la Fase 4.

**Principio rector:** todo sigue por el bus; las dos únicas lecturas síncronas
permitidas son **CognitiveState** (vista de solo lectura) y **Memory**. El
Planner decide, el estado solo aporta datos, los thoughts nunca salen.

### A.1 CognitiveState (`brain/cognition.py`) — el estado mental (solo datos)

Se actualiza SOLO escuchando eventos; se lee síncrono desde el Planner. **Cero
lógica de decisión** dentro (ningún `if` de negocio: eso es del Planner).

```python
class CognitiveState(BaseModel):
    attention: str | None                    # a qué atiende ahora
    conversation: ConversationInfo | None    # con quién habla, desde cuándo
    current_goal: str | None                 # objetivo del último plan
    active_user: str | None                  # usuario presente identificado
    environment: dict[str, Any]              # snapshot: hay gente, hora local...
    mode: str                                # "normal" | "profesor" | ... (set_state)
    drives: Drives                           # ver A.4
    last_seen: dict[str, datetime]           # persona -> última vez vista
    last_heard: datetime | None
```

Actualizaciones por evento: `person.detected`→`last_seen`;
`conversation.started`→`conversation`; `plan.created`→`current_goal`. La acción
`set_state` del Executor (hoy stub, `executor.py:135`) escribe aquí. Escenario
guía: `person.detected` → el Planner LEE el estado (¿conversación activa? ¿le
conozco? ¿hace cuánto?) → decide saludar. La regla vive en el Planner, no aquí.

### A.2 AttentionManager (`brain/attention.py`) — filtro percepción→cognición

Con voz+visión, cada ruido dispararía al Planner. El AttentionManager calcula una
**salience** y decide: ignorar, solo actualizar estado, o promover a
`attention.focused` (lo que el Planner escucha en vez de la percepción cruda).

```
salience = novedad + relevancia_goal + presencia + prioridad_evento + drives
```

Bajo umbral → solo actualiza CognitiveState (Alice "lo ve" pero no "le atiende").
Sobre umbral → emite `attention.focused` con el evento embebido. `command.received`
**siempre** pasa. Umbrales en `alice.toml`; cada decisión se loguea con su score.
**No construir hasta tener percepción real que filtrar** (ya la hay: voz/visión).

### A.3 Internal Thoughts — lo que Alice piensa y nunca dice

Un evento + persistencia, no un módulo grande. Evento `thought.created` con
`Thought(text, about, kind, confidence)` (`kind`: observación | hipótesis |
intención de seguimiento). Lo emiten Planner (al decidir) y Executor (al
completar/fallar). Van a memoria episódica (`kind: thought`) vía un `remember`
implícito. **Regla dura, con test dedicado:** ningún camino de código lleva un
`thought.*` a `response.ready`. Solo los leen el Planner, el LLM (contexto) y los logs.

### A.4 Drives (dentro de CognitiveState) — pesos de planificación, no emociones

```python
class Drives(BaseModel):
    curiosity: float = 0.5    # sube la salience de lo novedoso
    urgency: float = 0.0      # acorta planes, sube prioridades de eventos
    confidence: float = 0.5   # bajo → el Planner prefiere preguntar antes de actuar
    importance: float = 0.5   # peso del goal actual frente a interrupciones
```

Los leen el AttentionManager (modulan salience) y el Planner (modulan reglas); los
modifican eventos y acciones `set_state`. Se sacan a un módulo propio SOLO si algún
día ganan dinámica interna (decaimiento, interacción entre drives). No antes.

### A.5 Eventos futuros del catálogo (para las Fases 1 y 4)

```
attention.focused     # el AttentionManager promovió una percepción (Fase 4)
thought.created       # pensamiento interno, nunca sale al usuario (Fase 4)
goal.set / goal.done  # ciclo de vida del objetivo actual (Fase 4)
state.changed         # CognitiveState cambió: mode, drives, active_user (Fase 4)
confirmation.requested / confirmation.granted   # flujo de confirmación (Fase 2)
```

(Ya existen y funcionan: `plan.completed`, `plan.failed`, `response.ready`.)

---

## Anexo B — Documentos históricos (consolidados aquí)

Estos documentos describían trabajo **ya terminado**; se archivan en
`docs/historico/` (preservados en git) y su contenido vigente vive ahora en este
plan o en el `README.md`:

- **`PLAN_ALICE_CORE.md`** + **`PROMPT_OPUS.md`** — plan y prompt de la v1.0
  (núcleo por eventos). Implementado; los contratos viven en el código y el README.
- **`PLAN_V1_2.md`** — persistencia SQLite + primer LLM real. Implementado.
- **`SPEC_ALICE_KERNEL_V2.md`** — los 5 pilares cognitivos. El Action Executor está
  hecho; los otros 4 pilares se rescataron al **Anexo A** de este documento.
- **`ROADMAP.md`** — diagnóstico y fases 1-6. Superado por este plan (fases 1-2 hechas).
