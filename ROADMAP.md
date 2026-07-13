# ROADMAP de Alice — diagnóstico y plan a futuro

> Generado el 2026-07-09. Documento de planificación: **nada de aquí está implementado aún**,
> salvo lo listado en "Estado actual". Cada fase incluye un *prompt de arranque* listo para
> pegar en Claude Code cuando quieras ejecutarla.

---

## 1. Estado actual (lo que ya funciona)

- **Núcleo por eventos**: EventBus, Orchestrator, Scheduler, StateManager. Ningún módulo
  importa a otro; todo fluye por eventos con `correlation_id`.
- **Planner híbrido**: regex primero (gratis), y si nada aplica, el LLM clasifica la
  intención (`datetime` / `reminder` / `remember` / `recall` / `chat`).
- **Tools**: `datetime` y `recall_memory`, vía ToolManager con permisos y timeout.
- **Memoria**: SQLite (episódica + largo plazo) y short-term en RAM para el contexto.
- **LLM**: LM Studio (`127.0.0.1:1234`), el modelo se elige desde su GUI.
- **Voz** (`voice_web`, :8756): chat web, micrófono → faster-whisper (CPU), TTS del navegador.
- **Visión** (`vision`, :8757): reconoce caras (InsightFace), presencia y gestos (MediaPipe);
  enrolamiento web; saluda por tu nombre al verte.
- **Calidad**: 87 tests, ruff y mypy estricto en verde. Python 3.14, venv en `.venv`.

### Deuda técnica conocida (arrastrada, no urgente)

- Las reglas regex `internet` y `shell` del planner apuntan a tools que **no existen** →
  si disparan, el plan falla. Habría que quitarlas o implementar las tools (fases 3–4).
- Queda un hecho basura en la BD (`trabajo de noche`, alucinado en una prueba temprana):
  limpiar `data/alice.db` (tabla `episodes`, kind `user_fact`).
- `narrate = true` + clasificador = 2 llamadas al LLM por turno → latencia doble con
  modelos lentos. Aceptable hoy; el tool-calling de la fase 2 lo resuelve de raíz.
- mypy no cubre `plugins/` (solo `alice/`).

---

## 2. Diagnóstico de los dos problemas observados

### A) "El modelo no usa la cámara cuando hablo con él"

**Correcto, y es un límite de diseño actual**: la visión es *push-only*. El plugin
publica eventos (`person.detected`, `face.recognized`, `hand.detected`) y reacciona
(saluda), pero **nadie puede preguntarle**. Cuando le dices a Alice "¿me ves?" o
"¿quién está frente a ti?":

1. No existe una tool `camera`/`vision` que el planner pueda invocar.
2. El estado de visión (`VisionResult`: caras, nombres, gesto) vive **dentro** del
   plugin, inaccesible para el resto del sistema.
3. El clasificador de intenciones no tiene una intención "vision".

Resultado: la pregunta cae en `chat`, y el LLM (que no ve nada) contesta que no puede.
**Se arregla en la Fase 1** (percepción consultable).

### B) "No veo que use las tools"

**Correcto, y la causa es estructural**: el sistema actual es *clasificación de
intenciones con catálogo cerrado* (5 intenciones), no *tool-calling* real:

1. El LLM del chat **nunca ve la lista de tools**; no puede decidir usarlas a mitad
   de una respuesta. Solo el clasificador enruta, y solo hacia 5 destinos fijos.
2. Solo hay 2 tools reales; casi todo acaba en `chat`.
3. El modelo cargado (llama-3.2-3b) es débil para tool-use; con `glm-4.7-flash`
   (ya lo tienes en LM Studio) el enrutado y las respuestas mejorarían bastante.

**Se arregla en la Fase 2** (tool-calling nativo), que además es el prerequisito para
escalar a "ejecutar scripts" y "controlar el PC" sin reescribir el planner cada vez.

---

## 3. Plan por fases

> Orden recomendado: 1 → 2 → 3 → 4 → 5. Las fases 1 y 2 son la base; 3 y 4 son lo
> que pediste (scripts + control del PC); 5 es visión avanzada.

### Fase 1 — Percepción consultable (la cámara responde preguntas) — ✅ HECHO

> Implementado 2026-07-09. Endpoint `GET /state` en el plugin vision, tool `camera`
> (consulta la escena por HTTP, degrada si la visión está apagada), intención
> `vision` en el clasificador + regla regex. Verificado en vivo: "¿me ves?" ejecuta
> la tool y Alice responde con lo que ve.

**Objetivo**: poder preguntar "¿me ves?", "¿quién está ahí?", "¿qué gesto hago?".

- Nuevo endpoint `GET /state` en el plugin de visión: JSON con el último
  `VisionResult` (caras+nombres+similitud, gesto, timestamp del frame).
- Nueva tool `camera` (permiso `read_system`) que consulta ese endpoint y devuelve
  la escena como observación estructurada → el LLM la narra.
- Nueva intención `vision` en el clasificador + regla regex ("me ves", "quién está",
  "qué estoy haciendo", "mira").
- Extra opcional: la tool incluye cuánto hace que se vio a cada persona ("te vi hace
  2 minutos") usando el historial de eventos.

**Criterio de aceptación**: "¿me ves?" → Alice responde con tu nombre y qué ve,
usando la cámara de verdad.

**Prompt de arranque**:
> Implementa la Fase 1 del ROADMAP.md: percepción consultable. Endpoint /state en el
> plugin vision, tool `camera`, intención `vision` en el clasificador y regla regex.
> Con tests y prueba en vivo.

### Fase 2 — Tool-calling nativo (el LLM decide usar herramientas) — ✅ HECHO

> Implementado 2026-07-09. `select_tools` (function calling) en el proveedor,
> catálogo generado de las `ToolDefinition` (`tool_catalog.py`), `ToolCallingStrategy`
> (regex primero; si nada aplica, el LLM elige tools del catálogo real, una vuelta,
> soporta varias tools por turno). Config `planner.strategy = "tool_calling"` (ya
> activa). Verificado en vivo: el modelo elige `datetime`/`camera` por function
> calling. NOTA: llama-3.2-3b es ruidoso (a veces sobre/sub-selecciona); el regex
> cubre lo habitual y con glm-4.7-flash la selección es fiable. Falta (mejora
> futura): bucle agente multi-vuelta (hoy es selección de una sola vuelta).

**Objetivo**: que el LLM vea el catálogo de tools y las invoque él mismo, en bucle
(estilo agente), en lugar del clasificador de 5 intenciones.

- Nueva `ToolCallingStrategy` (el Planner ya acepta estrategias intercambiables):
  - Genera el catálogo de funciones desde las `ToolDefinition` existentes (los schemas
    Pydantic ya están: se convierten a JSON Schema con `model_json_schema()`).
  - Usa el API de function calling de LM Studio (`tools=[...]` en `/chat/completions`).
  - Bucle: LLM pide tool → executor la corre → resultado vuelve al LLM → respuesta final.
- Mantener el clasificador actual como fallback para modelos sin tool-use.
- Config: `planner.strategy = "tool_calling" | "hybrid" | "rules"`.
- Recomendación: cargar `glm-4.7-flash` en LM Studio para esta fase.

**Criterio de aceptación**: con 5+ tools registradas, "qué hora es y qué sabes de mí"
ejecuta `datetime` + `recall_memory` en un solo turno, sin tocar el prompt del clasificador.

**Prompt de arranque**:
> Implementa la Fase 2 del ROADMAP.md: ToolCallingStrategy con function calling de
> LM Studio, catálogo generado de las ToolDefinition, bucle agente en el executor,
> y config planner.strategy. Con tests (proveedor falso) y prueba en vivo.

### Fase 3 — Ejecutar scripts (con seguridad por diseño)

**Objetivo**: "Alice, corre el script de backup" — sin abrirle la puerta a comandos
arbitrarios destructivos.

**Principio**: catálogo curado antes que shell libre. Dos niveles:

1. **`ScriptTool` (catálogo curado — el 80% del valor, 20% del riesgo)**:
   - Carpeta `scripts/` del proyecto; cada script (.ps1/.py/.bat) con un pequeño
     manifest (nombre, descripción, ¿requiere confirmación?).
   - Alice puede **listarlos** y **ejecutarlos por nombre**, con timeout y captura
     de salida (stdout → observación → narración).
   - Permiso nuevo efectivo: `shell` (ya existe en el enum `Permission`, hoy no
     otorgado en `alice.toml` — se activa explícitamente).
2. **`ShellTool` (comandos directos — opcional, tras probar el nivel 1)**:
   - Allowlist de binarios permitidos + denylist dura (formato, rm -rf, regedit...).
   - **Flujo de confirmación**: nuevo par de eventos `confirmation.requested` /
     `confirmation.granted`. Alice dice "¿Ejecuto X? di sí o no" y espera tu
     respuesta (por voz o web) antes de correr nada que mute el sistema.

**Criterio de aceptación**: "ejecuta el script de limpieza" lo corre y narra el
resultado; "borra system32" es rechazado; un comando no listado pide confirmación.

**Prompt de arranque**:
> Implementa la Fase 3 del ROADMAP.md: ScriptTool con catálogo en scripts/ y manifest,
> permiso shell activable en config, flujo de confirmación por eventos, y (si decido
> el nivel 2) ShellTool con allowlist. Seguridad primero. Tests incluidos.

### Fase 4 — Control de la computadora (gradual, por nivel de riesgo)

**Objetivo**: Alice como control por voz del PC. Ordenado de menos a más riesgo:

1. **`app_launcher`** (riesgo bajo): abrir/cerrar aplicaciones ("abre el navegador",
   "pon el bloc de notas"). Catálogo de apps conocidas + `start` de Windows.
2. **`system_control`** (riesgo medio): volumen y silencio (pycaw), brillo, bloquear
   pantalla, capturas. Apagar/reiniciar/suspender **siempre con confirmación** (usa el
   flujo de la Fase 3).
3. **`window_manager`** (riesgo medio): listar/enfocar/minimizar ventanas (pygetwindow).
4. **`keyboard_mouse`** (riesgo alto — al final): teclado y ratón (pyautogui), solo
   dentro de un "modo control" que se activa y desactiva explícitamente por voz, con
   failsafe de esquina de pantalla.

Todos como tools normales → con la Fase 2 hecha, el LLM los usa solo, conversacionalmente.

**Criterio de aceptación**: "sube el volumen y abre Spotify" funciona por voz;
"apaga el PC" pide confirmación y solo procede con un "sí".

**Prompt de arranque**:
> Implementa la Fase 4 del ROADMAP.md empezando por app_launcher y system_control
> (volumen/bloqueo/captura), con confirmación para acciones destructivas. Verifica
> compatibilidad de pycaw/pygetwindow con Python 3.14 antes de codificar.

### Fase 5 — Visión avanzada (describir la escena)

**Objetivo**: "¿qué estoy sosteniendo?" / "describe lo que ves".

- LM Studio ya soporta modelos multimodales (tienes `qwen2-vl-2b-instruct` descargado).
- Tool `describe_scene`: captura el frame actual → lo manda como imagen al endpoint
  `/v1/chat/completions` → descripción en texto → narración.
- Opcional: memoria de escena ("¿quién vino hoy?") cruzando eventos de visión con
  la memoria episódica.
- Opcional: enrolamiento conversacional — Alice ve a un desconocido y pregunta
  "no te conozco, ¿cómo te llamas?" y lo enrola sola.

**Prompt de arranque**:
> Implementa la Fase 5 del ROADMAP.md: tool describe_scene usando un modelo
> multimodal en LM Studio (qwen2-vl), captura del frame desde el plugin vision,
> y opcionalmente el enrolamiento conversacional de desconocidos.

### Fase 6 — Ideas posteriores (sin orden)

- **Wake word** ("oye Alice") con detección local, para hablarle sin tocar nada.
- **Proactividad**: resumen diario (el stub `daily_summary` ya existe), recordatorios
  inteligentes, "buenos días" al detectarte la primera vez del día.
- **Multi-usuario**: perfiles por cara reconocida (memoria y preferencias por persona).
- **Streaming de respuestas** (el TTS empieza a hablar antes de que el LLM termine).
- Limpiar la deuda técnica listada arriba.

---

## 4. Decisiones de arquitectura que este plan respeta

- **Todo pasa por el bus**: las nuevas capacidades son tools + eventos, nunca imports
  cruzados entre módulos.
- **Las tools no producen prosa**: devuelven observaciones estructuradas; el LLM narra.
- **Permisos explícitos**: nada con `shell`/`write_system` corre sin activarlo en
  `alice.toml`, y lo destructivo pide confirmación en el momento.
- **Degradación**: si un modelo/tool falla, Alice responde igual (aunque sea en crudo).
