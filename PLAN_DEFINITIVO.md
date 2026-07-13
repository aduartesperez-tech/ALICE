# Alice — Plan definitivo: de asistente reactivo a agente autónomo

> Actualizado el 2026-07-13. Es la **única hoja de ruta vigente** (los documentos
> de versiones entregadas están en `docs/historico/`). Parte de una revisión del
> código real, no de las notas.
>
> **La meta:** que Alice funcione como un agente de IA completo — consciente de
> su entorno (cámara, micrófono, estado del sistema), consciente de sus propias
> capacidades, capaz de ejecutar comandos, scripts y código, y **autónoma**: que
> no solo responda, sino que perciba, decida y actúe por iniciativa propia,
> dentro de límites de seguridad explícitos.

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

**Estado de los tests (verificado 2026-07-13):** todos los archivos pasan y salen
limpios **excepto `tests/test_orchestrator.py`, que se cuelga** (timeout). Causa
raíz: ese test arranca el directorio `plugins/` **real**, y `start_all()` arranca
*todos* los plugins descubiertos — incluidos `vision` (webcam + InsightFace) y
`voice_web` (servidor web + Whisper). Regresión de aislamiento de tests, no bug
del núcleo. → se salda en la Fase 0.

**Deuda técnica conocida:** (1) tests acoplados a hardware; (2) no hay forma de
desactivar un plugin (`start_all()` arranca todo, sin flag `enabled` ni allowlist);
(3) reglas regex muertas `internet`/`shell` en el planner apuntan a tools no
implementadas → si disparan, el plan falla; (4) hecho basura en la BD (`episodes`
id 36, "trabajo de noche"); (5) el tool-calling es de **una sola vuelta**, no un
bucle agente; (6) mypy no cubre `plugins/`.

---

## 1. El marco: qué hace de Alice un *agente autónomo*

Un agente de IA no es un chatbot con herramientas. Es un sistema que **percibe su
entorno, razona en bucle, actúa sobre el mundo, observa el resultado y repite**,
con memoria, conocimiento de sí mismo y — en su forma completa — **iniciativa
propia**: no espera siempre a que le hablen. Las diez propiedades de un agente
completo y dónde está Alice en cada una:

| # | Propiedad | Estado en Alice | Fase |
|---|---|---|---|
| 1 | **Percepción** (ver, oír, sentir el sistema) | ⚠️ voz y visión sí; oído ambiente y estado del sistema no | 4 |
| 2 | **Bucle agente** (razonar→actuar→observar→repetir) | ❌ selección de una sola vuelta | **1** |
| 3 | **Manos** (comandos, scripts, código) | ❌ ninguna tool de ejecución | **2** |
| 4 | **Control del entorno** (apps, ventanas, sistema) | ❌ | 3 |
| 5 | **Conciencia de sí misma** (sabe qué puede hacer) | ⚠️ el catálogo existe pero Alice no razona sobre él | 5 |
| 6 | **Modelo del mundo / estado mental** | ❌ `CognitiveState` no existe | 5 |
| 7 | **Memoria** (recuperación semántica, consolidación) | ⚠️ SQLite sí; semántica no | 7 |
| 8 | **Autonomía** (metas propias, iniciativa, agenda) | ❌ solo reacciones push (saludo) | **6** |
| 9 | **Seguridad** (permisos, confirmación, auditoría) | ⚠️ enum de permisos sí; confirmación/auditoría no | 2 (transversal) |
| 10 | **Reflexión** (aprende de lo que hizo) | ❌ `Thoughts` no existe | 5 |

**Las tres brechas críticas**, en orden: el **bucle agente** (#2 — sin él nada
compone), las **manos** (#3 — sin ejecutar, no hay agencia real), y la
**autonomía** (#8 — la diferencia entre un asistente y un agente). Todo lo demás
alimenta a esas tres.

### La escala de autonomía (cómo se llega sin saltos de fe)

La autonomía no es un interruptor: es una escala que se sube nivel a nivel, y
cada nivel exige que el anterior esté probado. Este plan la recorre entera:

- **A0 — Reactiva** *(hoy)*: responde cuando le hablan; reacciones push triviales.
- **A1 — Iterativa** *(Fase 1)*: dentro de un turno, encadena tools y razona
  sobre resultados hasta cumplir la petición.
- **A2 — Ejecutora** *(Fases 2–3)*: actúa sobre el mundo real (scripts, código,
  PC) con confirmación para lo peligroso.
- **A3 — Situada** *(Fases 4–5)*: percibe entorno y estado continuamente; sus
  decisiones dependen de contexto (quién está, qué se oye, qué corre en el PC).
- **A4 — Proactiva** *(Fase 6)*: tiene metas y agenda propias; inicia acciones
  sin que se le pida, dentro de un presupuesto y límites configurados.

---

## 2. Plan por fases

Orden: **0 → 1 → 2 → 3 → 4 → 5 → 6 → 7**. La 0 sanea; la 1 es el salto
conceptual; 2–3 son las manos; 4–5 la conciencia; 6 la autonomía; 7 el pulido.
Cada fase termina con tests + ruff + mypy en verde y una prueba en vivo.

### Fase 0 — Sanear la base (bloqueante, pequeña)

**Objetivo:** suite verde y determinista sin hardware; deuda muerta fuera.

- Aislar `test_orchestrator` del hardware: `plugins_dir` de test apunta a un
  fixture con solo `echo`/`console`.
- **Plugins activables**: flag `enabled` en `manifest.toml` y/o allowlist
  `plugins.enabled` en `alice.toml`; `start_all()` lo respeta. (Necesario también
  para correr Alice sin webcam/micrófono.)
- Quitar las reglas regex muertas `internet`/`shell` del planner.
- Limpiar la BD (`episodes` id 36) y extender mypy a `plugins/`.

**Aceptación:** `pytest -q` verde y **sale solo**, sin abrir webcam ni cargar
Whisper; `ruff` y `mypy` (incl. `plugins/`) en verde.

### Fase 1 — El bucle agente (el corazón) — autonomía A1

**Objetivo:** que Alice encadene **tool → observar → decidir el siguiente paso →
… → responder**, en varias vueltas, no una selección única.

- **`AgentLoopStrategy`** (evolución de `ToolCallingStrategy`): conversación
  iterativa con el LLM donde en cada vuelta el modelo (a) pide una o más tools, o
  (b) responde. El resultado de cada tool vuelve como mensaje `tool` y el bucle
  sigue hasta respuesta final o `max_iters` (p.ej. 6, cortafuegos anti-bucle).
- Encaja en el diseño actual: el Executor ya trae `observations` de vuelta; se
  generaliza para **re-preguntar al LLM** en vez de solo narrar y cerrar. El
  bucle (decisión) vive en la estrategia; el Executor sigue solo ejecutando.
- **Presupuesto por turno**: `max_iters` + tope de tiempo total; al agotarse,
  Alice responde con lo que tenga y lo dice ("no me dio tiempo a X").
- El **system prompt del bucle** incluye identidad + capacidades (semilla de la
  conciencia de sí misma de la Fase 5).
- Config: `planner.max_agent_iters`. Regex sigue primero para lo trivial.
  Recomendado: `glm-4.7-flash` en LM Studio (llama-3.2-3b es ruidoso en tool-use).

**Aceptación:** una tarea que necesita el resultado de la tool A para elegir la
tool B ("mira quién hay y salúdalo por su nombre") encadena A→B en el mismo turno
sin tocar ningún prompt.

### Fase 2 — Las manos: comandos, scripts y código — autonomía A2

**Objetivo:** "Alice, corre el backup", "ejecuta este código", "¿cuánto ocupa la
carpeta X?" — con seguridad por diseño, no por suerte.

**Primero la barandilla, luego las manos.**

1. **Flujo de confirmación** (prerequisito de todo lo que muta): eventos
   `confirmation.requested` / `confirmation.granted`. Alice dice "¿ejecuto X?
   sí/no" y **espera** la respuesta (voz o web) antes de correr nada peligroso.
   Con timeout: sin respuesta = no.
2. **Auditoría**: toda ejecución (comando, script, código) deja un registro
   episódico (`kind: execution`: qué, cuándo, por qué — el goal del plan, salida,
   exit code). Alice puede responder "¿qué has ejecutado hoy?" con `recall`.
3. **`ScriptTool`** — catálogo curado (el 80% del valor, 20% del riesgo):
   carpeta `scripts/` con manifest por script (nombre, descripción, ¿confirma?).
   Alice los lista y ejecuta por nombre, con timeout y captura de salida →
   observación → narración.
4. **`ShellTool`** — comandos directos: allowlist de binarios de solo-lectura
   que corren libres (`dir`, `type`, `systeminfo`, `tasklist`, `ping`…) +
   denylist dura (format, del /s, rm -rf, regedit, diskpart…) + **todo lo demás
   pide confirmación**. Timeout y captura de stdout/stderr.
5. **`PythonTool`** — ejecutar código que el propio LLM escribe: proceso
   separado (`subprocess` con el venv), timeout duro, sin red por defecto,
   directorio de trabajo jaula (`data/sandbox/`), stdout/stderr como observación.
   Siempre con confirmación al principio; relajable por config cuando se gane
   confianza. Es la herramienta más poderosa del plan: convierte "no tengo una
   tool para eso" en "escribo una al vuelo".

Permisos: activa `shell` en `alice.toml` (ya existe en el enum, hoy no otorgado).

**Aceptación:** "ejecuta el script de limpieza" corre y narra; "borra system32"
se rechaza en seco; "calcula el hash de este archivo" → Alice escribe y ejecuta
el código y da el resultado; un comando no listado pide confirmación; "¿qué has
ejecutado hoy?" responde con la auditoría.

### Fase 3 — Control de la computadora — autonomía A2

**Objetivo:** Alice como manos y voz del PC. Tools normales → con el bucle de la
Fase 1 el LLM las combina solo. De menos a más riesgo:

1. **`app_launcher`** (bajo): abrir/cerrar apps ("abre el navegador").
2. **`system_control`** (medio): volumen/silencio (pycaw), brillo, bloquear
   pantalla, capturas. Apagar/reiniciar/suspender **siempre con confirmación**.
3. **`window_manager`** (medio): listar/enfocar/minimizar ventanas (pygetwindow).
4. **`keyboard_mouse`** (alto, al final): pyautogui solo dentro de un "modo
   control" que se activa/desactiva explícitamente por voz, con failsafe de
   esquina de pantalla.

**Antes de codificar:** verificar compatibilidad de pycaw/pygetwindow/pyautogui
con Python 3.14.

**Aceptación:** "sube el volumen y abre Spotify" por voz; "apaga el PC" pide
confirmación y solo procede con un "sí".

### Fase 4 — Conciencia del entorno: ver, oír y sentir el sistema — autonomía A3

**Objetivo:** que Alice perciba **todo su entorno**, no solo cuando se le
pregunta. Tres sentidos:

1. **Sentir el sistema — `system_state` tool + eventos**: CPU/RAM/disco,
   batería, red (¿hay internet?), procesos principales, uptime (psutil).
   Como tool ("¿cómo está el PC?") **y** como percepción: el plugin publica
   eventos umbral (`system.alert`: disco lleno, CPU sostenida, sin red) que
   alimentan la proactividad de la Fase 6.
2. **Oír de verdad — evolución del plugin de voz**:
   - **Wake word** local ("oye Alice") para hablarle sin tocar nada — micrófono
     siempre abierto en un hilo, detección ligera en CPU (p.ej. openWakeWord).
   - **Eventos de sonido ambiente** (`sound.detected`): silencio→ruido, música,
     voz de fondo. No transcribe todo (privacidad y CPU): clasifica.
   - El micrófono deja de depender del navegador: captura local continua,
     Whisper solo se dispara tras el wake word.
3. **Ver mejor — visión ya existente + escena multimodal**:
   - **`describe_scene`**: frame de la webcam → modelo multimodal en LM Studio
     (`qwen2-vl-2b-instruct`, ya descargado) → "¿qué estoy sosteniendo?".
   - **Enrolamiento conversacional**: ve a un desconocido → "no te conozco,
     ¿cómo te llamas?" → enrola sola.
   - Memoria de escena: "¿quién vino hoy?" cruzando eventos de visión con la
     memoria episódica.

Todo son **tools + eventos** (consultable con el bucle, percibible para la
autonomía). Ninguna percepción llama a otra: todo por el bus.

**Aceptación:** "¿cómo va el PC?" responde con datos reales; "oye Alice, ¿qué
hora es?" funciona sin tocar nada; "¿qué tengo en la mano?" usa el modelo
multimodal; al quedarse sin internet, Alice lo nota (evento) y lo dice.

### Fase 5 — Conciencia de sí misma: estado mental e introspección — autonomía A3

**Objetivo:** que Alice sepa **qué puede hacer**, **qué está pasando** y **qué
aprendió** — los pilares cognitivos pendientes de la SPEC (contratos cerrados en
el **Anexo A**) más la introspección de capacidades.

- **Introspección de capacidades — `capabilities` tool + prompt vivo**: el
  catálogo de tools, scripts y plugins activos se inyecta resumido en el system
  prompt (Alice *sabe* que tiene manos, ojos y oídos) y es consultable ("¿qué
  sabes hacer?" → respuesta honesta generada del catálogo real, no inventada).
  Incluye lo que NO puede: permisos no otorgados, plugins apagados.
- **`CognitiveState`** (Anexo A.1): estado mental como datos — ¿conversación
  activa?, ¿quién está presente?, ¿qué se oye?, ¿qué goal corre?, `last_seen`.
  Se actualiza solo por eventos; el Planner lo LEE para decidir con contexto.
  La acción `SET_STATE` del Executor (hoy stub) se cablea aquí.
- **`AttentionManager`** (Anexo A.2): con oído+visión+sistema emitiendo eventos
  continuos (Fase 4), hace falta el filtro de salience antes del Planner. Ya hay
  percepción real que filtrar: ahora sí se construye.
- **`Thoughts`** (Anexo A.3): Alice registra lo que piensa/aprende (`kind:
  thought` en episódica). Nunca salen al canal de salida (test dedicado).
- **`Drives`** (Anexo A.4): pesos de planificación (curiosity, urgency,
  confidence, importance) dentro de CognitiveState.

**Aceptación:** "¿qué sabes hacer?" responde del catálogo real; el mismo evento
produce planes distintos según el estado (con/sin conversación activa); un ruido
irrelevante actualiza el estado pero NO despierta al Planner.

### Fase 6 — Autonomía plena: metas, agenda e iniciativa — autonomía A4

**Objetivo:** el salto final — que Alice **haga cosas sin que se le pida**,
dentro de límites explícitos. Es la fase que convierte todo lo anterior en un
agente de verdad.

1. **Rutinas proactivas** (lo seguro primero): "buenos días" al verte la primera
   vez del día (visión + CognitiveState), resumen diario (el stub
   `daily_summary` por fin se implementa: episodios del día → LLM → narración),
   recordatorios inteligentes ("dijiste que ibas a estirar y llevas 3 h sentado").
2. **Metas persistentes** (`goal.set` / `goal.done`): "Alice, vigila que el disco
   no se llene" crea una meta que sobrevive reinicios (SQLite), revisada por el
   scheduler. Cada meta define: disparador (evento o intervalo), acción (plan),
   y **presupuesto** (cuántas veces/día, qué tools puede usar).
3. **Bucle cognitivo de fondo** (el corazón de A4): un "tick" periódico de baja
   frecuencia (config, p.ej. cada 5–15 min) donde Alice revisa CognitiveState +
   metas + alertas del sistema y decide — con el mismo bucle agente de la Fase 1 —
   si hay algo que valga la pena hacer o decir. Casi siempre la respuesta es
   "nada" (y no gasta LLM: pre-filtros baratos deciden si despertar al modelo).
4. **Límites duros de la autonomía** (no negociables, en config):
   - Presupuesto: máx. acciones autónomas/hora y máx. llamadas LLM/hora.
   - Las acciones autónomas solo usan tools de **lectura** por defecto; ejecutar
     algo que muta (script, shell, apagar) **siempre** requiere confirmación
     humana, aunque venga de una meta.
   - Todo lo autónomo queda auditado (Fase 2) y es consultable ("¿qué has hecho
     mientras no estaba?").
   - **Interruptor maestro**: `autonomy.enabled = false` en config y un comando
     de voz ("Alice, modo pasivo") la devuelven a A0 al instante.
   - Horario: franjas de silencio configurables (no hablar de madrugada).

**Aceptación:** al verte por primera vez en el día te saluda y te da el resumen
pendiente; "vigila el disco" crea una meta que avisa al llegar al umbral días
después; con `autonomy.enabled=false` no ocurre NADA proactivo; "¿qué has hecho
hoy?" lista sus acciones autónomas.

### Fase 7 — Memoria avanzada y pulido

- **Memoria semántica**: embeddings + búsqueda por similitud sobre la episódica
  (en vez de solo lectura por `kind`); consolidación: resumir episodios viejos en
  hechos de largo plazo (puede correr en el tick de fondo de la Fase 6).
- **Multi-usuario**: perfiles de memoria y preferencias por cara reconocida;
  permisos por persona (invitados no pueden pedir ejecución).
- **Streaming de respuestas**: el TTS empieza a hablar antes de que el LLM
  termine (sensación de vida).
- **Panel único**: una web que unifique voz (:8756) + visión (:8757) + estado +
  auditoría + metas.

---

## 3. Decisiones de arquitectura que este plan respeta

- **Todo pasa por el bus**: cada capacidad nueva es *tools + eventos*, nunca
  imports cruzados. Percepción publica, cognición decide, ejecución actúa.
- **Las tools no producen prosa**: devuelven observaciones estructuradas; el LLM
  narra. La IA es la única capa de presentación.
- **El Planner decide, el Executor actúa, el Orchestrator solo compone.**
- **Permisos explícitos + confirmación + auditoría**: nada con `shell`/
  `write_system` corre sin activarlo en config; lo destructivo pide confirmación
  en el momento; todo lo ejecutado queda registrado. La autonomía (Fase 6) nunca
  salta la confirmación humana para acciones que mutan.
- **Degradación**: si un modelo, tool o sensor falla, Alice responde igual
  (aunque sea en crudo) y lo dice. Nunca se queda muda.
- **Nada se construye sin poder probarse**: cada fase tiene aceptación en vivo;
  el AttentionManager espera a que exista percepción continua (Fase 4) — un
  filtro sin tráfico es código muerto.

---

## 4. Orden recomendado (resumen accionable)

1. **Fase 0** ya — media hora, suite verde que sale sola, plugins activables.
2. **Fase 1** — el bucle agente. Desbloquea todo lo demás.
3. **Fase 2** — confirmación + auditoría + scripts/shell/código. Las manos.
4. **Fases 3–4 en paralelo si se quiere** (control del PC es independiente de
   los sentidos nuevos).
5. **Fase 5** cuando los sentidos emitan de verdad; **Fase 6** al final, cuando
   haya manos probadas, sentidos y estado mental sobre los que ser autónoma.

---

## Anexo A — Contratos de los pilares cognitivos (detalle de la Fase 5)

> Rescatado de `SPEC_ALICE_KERNEL_V2.md` (archivada). El pilar **Action Executor**
> ya está implementado; lo que sigue son los **cuatro pilares que faltan**, con su
> contrato cerrado, para no rediseñarlos cuando llegue la Fase 5.

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

Con voz+visión+sistema, cada ruido dispararía al Planner. El AttentionManager
calcula una **salience** y decide: ignorar, solo actualizar estado, o promover a
`attention.focused` (lo que el Planner escucha en vez de la percepción cruda).

```
salience = novedad + relevancia_goal + presencia + prioridad_evento + drives
```

Bajo umbral → solo actualiza CognitiveState (Alice "lo ve" pero no "le atiende").
Sobre umbral → emite `attention.focused` con el evento embebido. `command.received`
**siempre** pasa. Umbrales en `alice.toml`; cada decisión se loguea con su score.

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

### A.5 Eventos futuros del catálogo (por fase)

```
confirmation.requested / confirmation.granted   # confirmación humana (Fase 2)
execution.logged      # auditoría de comandos/scripts/código (Fase 2)
system.alert          # umbral del sistema: disco, CPU, red (Fase 4)
sound.detected        # evento de sonido ambiente clasificado (Fase 4)
wake_word.detected    # "oye Alice" (Fase 4)
attention.focused     # el AttentionManager promovió una percepción (Fase 5)
thought.created       # pensamiento interno, nunca sale al usuario (Fase 5)
state.changed         # CognitiveState cambió: mode, drives, active_user (Fase 5)
goal.set / goal.done  # ciclo de vida de metas persistentes (Fase 6)
autonomy.tick         # tick del bucle cognitivo de fondo (Fase 6)
```

(Ya existen y funcionan: `plan.completed`, `plan.failed`, `response.ready`.)

---

## Anexo B — Documentos históricos (consolidados aquí)

Estos documentos describían trabajo **ya terminado**; están archivados en
`docs/historico/` (preservados en git) y su contenido vigente vive ahora en este
plan o en el `README.md`:

- **`PLAN_ALICE_CORE.md`** + **`PROMPT_OPUS.md`** — plan y prompt de la v1.0
  (núcleo por eventos). Implementado; los contratos viven en el código y el README.
- **`PLAN_V1_2.md`** — persistencia SQLite + primer LLM real. Implementado.
- **`SPEC_ALICE_KERNEL_V2.md`** — los 5 pilares cognitivos. El Action Executor está
  hecho; los otros 4 pilares se rescataron al **Anexo A** de este documento.
- **`ROADMAP.md`** — diagnóstico y fases 1-6 originales. Superado por este plan
  (sus fases 1-2 ya están hechas; el resto se reabsorbió y amplió aquí).
