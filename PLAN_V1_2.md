# Alice Kernel — Plan v1.2: "Alice recuerda y habla de verdad"

> Documento de planeación. No es código: cierra las decisiones para implementar
> la v1.2 de la spec (`SPEC_ALICE_KERNEL_V2.md`, roadmap §5): **memoria
> persistente (SQLite) + primer proveedor LLM real**. Al terminar, Alice
> conserva lo aprendido entre reinicios y sus respuestas las narra una IA real.

---

## 0. Estado de partida (v1.1, verificado)

Funciona por terminal: Planner → Action Executor → tools/scheduler → consola.
40 tests, mypy strict, ruff. Los datos viajan estructurados (`Observation`);
las respuestas salen en crudo porque el LLM es `NullLLMProvider`. Los
`ActionKind` `remember` y `set_state` son stubs. La memoria episódica y de
largo plazo son no-op; la short-term existe pero **no está cableada a nada**.

**Lo que v1.2 entrega, en una frase:** matas el proceso, lo relanzas, Alice
recuerda; y escribe "¿qué hora es?" → responde *"Son las 8:51 de la noche"* en
lugar de `[datetime] iso=...`.

---

## 1. Decisiones de diseño (cerradas)

| # | Tema | Decisión | Justificación |
|---|---|---|---|
| 1 | Proveedor LLM | **Un solo provider "OpenAI-compatible"** apuntando a un endpoint local (`/v1/chat/completions`) | LM Studio y Ollama exponen ambos esa API → un provider sirve para los dos backends; se elige por `base_url` en config, cero código nuevo por backend |
| 2 | Cliente HTTP | **`httpx`** (única dependencia nueva de v1.2) | Async nativo (urllib bloquearía el event loop), timeouts de primera clase. La spec permitía exactamente una dependencia para esto |
| 3 | Persistencia | **`sqlite3` de la stdlib**, modo WAL, un archivo `data/alice.db` | Cero dependencias; escrituras sub-milisegundo (no bloquean el loop de forma apreciable); vectores quedan para v2.x |
| 4 | Escrituras a memoria | Vía evento **`memory.store_requested`** (fire-and-forget); **lecturas** síncronas directas (permitido por guardarraíl 4 de la spec) | El Executor no gana referencia a la memoria; `remember` publica y avanza sin esperar |
| 5 | Narración de resultados de tools | El Planner añade `call_llm` antes de `respond` en los planes con tool **si `llm.narrate = true` en config** | Es la visión del proyecto ("la IA depura los datos y los dice bonitos") sin romper la filosofía de minimizar LLM: es un flag, y con `NullProvider` se apaga solo |
| 6 | LLM caído (Ollama apagado, modelo no descargado) | **Degradación, no fallo**: el LLMModule emite `llm.finished` con `success=false`; el Executor responde igualmente con las observaciones en crudo (modo v1.1) | Alice nunca se queda muda por culpa del LLM; el usuario ve los datos aunque no haya prosa |
| 7 | Contexto conversacional | La short-term memory por fin se cablea: un **MemoryModule** graba cada turno (`command.received`, `response.ready`) y el prompt del LLM gana un bloque `[HISTORY]` | "Cuéntame otro chiste" funciona; sin esto el LLM real parecería amnésico y la v1.2 se sentiría rota |
| 8 | Resumen diario (`daily_summary`) | Queda **stub documentado** (necesita LLM + scheduler job; es una feature de v1.3+) | No mezclar alcance: v1.2 es persistencia + narración |

---

## 2. Alcance — las 4 piezas

### 2.1 Memoria persistente (`brain/memory.py` + nuevo `brain/memory_sqlite.py`)

Implementar los Protocols ya existentes, sin cambiarlos:

- **`SqliteEpisodicMemory`** — episodios con timestamp: turnos de conversación,
  eventos importantes, (futuro) thoughts.
- **`SqliteLongTermMemory`** — hechos clave→valor: preferencias, personas,
  proyectos.

**Esquema (un archivo `data/alice.db`, WAL):**

```sql
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,      -- ISO 8601 UTC
    kind        TEXT NOT NULL,      -- "user_turn" | "alice_turn" | "event" | "thought"
    content     TEXT NOT NULL,      -- JSON del MemoryItem.content
    correlation_id TEXT             -- encadena con los logs
);
CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(timestamp);

CREATE TABLE IF NOT EXISTS facts (
    key         TEXT PRIMARY KEY,   -- p.ej. "user.nombre", "pref.idioma"
    value       TEXT NOT NULL,      -- JSON
    updated_at  TEXT NOT NULL
);
```

Reglas:
- La ruta del archivo va en config (`[memory] db_path`); los tests usan
  `tmp_path`. La carpeta `data/` entra en `.gitignore`.
- `query_by_date(day)` devuelve los episodios de ese día; `daily_summary`
  devuelve `None` (stub documentado, decisión 8).
- Conexión propia del módulo, `check_same_thread=False` no es necesario si
  todo corre en el loop; escrituras con `INSERT` simples y `commit` inmediato.

### 2.2 MemoryModule (`brain/memory_module.py`) — nuevo CoreModule

El pegamento entre el bus y los stores. Es la pieza que faltaba para que
`remember` deje de ser stub:

- **Escucha `memory.store_requested`** (evento nuevo) → escribe en el store
  indicado (`episodic` | `long_term`) y emite `memory.stored`.
- **Graba turnos automáticamente**: `command.received` → episodio `user_turn`;
  `response.ready` → episodio `alice_turn`. Además alimenta la short-term
  (deque en RAM) con los mismos turnos.
- **Expone lectura síncrona** para quien compone (`main.py` inyecta la vista
  de lectura al LLMModule para el bloque `[HISTORY]`).

En el **Executor**, la acción `remember` deja de ser stub: publica
`memory.store_requested` con `{store, kind, content}` y avanza sin esperar
(decisión 4). `set_state` sigue siendo stub (v1.3).

### 2.3 Proveedor LLM real (`brain/llm_openai_compat.py`)

**`OpenAICompatProvider(LLMProvider)`** — habla con cualquier servidor local
que exponga `/v1/chat/completions`:

| Backend | base_url típica | Nota |
|---|---|---|
| LM Studio | `http://localhost:1234/v1` | API OpenAI-compatible nativa |
| Ollama | `http://localhost:11434/v1` | Compatible desde 0.1.24+ |

- Config: `base_url`, `model`, `temperature`, `max_tokens`,
  `timeout_seconds` (default 60: los modelos locales son lentos).
- `generate()`: arma `messages` desde `LLMRequest` (system + history + user),
  hace POST con httpx async, mapea la respuesta a `LLMResponse` (text, model,
  usage). **Ningún tipo de OpenAI sale del archivo** (guardarraíl existente).
- `health_check()`: GET a `/v1/models`; se llama al arrancar el LLMModule y
  se loguea el resultado (`llm.provider_healthy` / `llm.provider_down`).
- Errores (conexión rechazada, timeout, 404 de modelo): se capturan y suben
  como fallo controlado, nunca como excepción que mate el módulo.

**Cambios de contrato mínimos:**
- `LLMFinishedPayload` gana `success: bool = True` y `error: str | None`.
- El Executor, al recibir `llm.finished` con `success=false`: loguea, **no
  aborta el plan** — continúa hacia `respond` sin `text` (el usuario recibe
  las observaciones en crudo, modo degradado, decisión 6).
- `main.py` elige provider por config: `[llm] provider = "openai_compat"` →
  `OpenAICompatProvider`; `"null"` → `NullLLMProvider` (default si no hay
  config, para que los tests y el arranque sin servidor sigan funcionando).

### 2.4 Narración y contexto (`brain/context.py`, `brain/planner.py`)

- `render_for_llm` gana el bloque `[HISTORY]` (últimos N turnos de la
  short-term, N en config) y un `[INSTRUCTION]` afinado: *"Responde en el
  idioma del usuario, breve y natural, usando SOLO los datos de OBSERVATIONS"*.
- El Planner lee `narrate: bool` (se lo pasa `main.py` desde config): si es
  `true`, los planes de tools (`datetime`, `internet`, `shell`, `reminder`)
  intercalan `call_llm` antes de `respond`. Si es `false`, quedan como v1.1.
- El system prompt del LLM vive en config (`[llm] system_prompt`) con un
  default sensato: identidad de Alice, tono, prohibición de inventar datos.

---

## 3. Config nueva (`config/alice.toml`)

```toml
[llm]
provider = "openai_compat"        # "openai_compat" | "null"
base_url = "http://localhost:1234/v1"   # LM Studio; Ollama: :11434/v1
model = "qwen2.5-7b-instruct"     # el que tengas cargado/descargado
temperature = 0.7
max_tokens = 512
timeout_seconds = 60.0
narrate = true                    # las tools pasan por el LLM para narrarse
history_turns = 6                 # turnos de contexto en [HISTORY]
system_prompt = "Eres Alice..."   # identidad; default en código

[memory]
db_path = "data/alice.db"
short_term_max_items = 200
```

Regla: con `provider = "null"`, `narrate` se fuerza a `false` (narrar con el
NullProvider no tiene sentido y rompería los flujos sin-LLM).

---

## 4. Eventos nuevos

```
memory.store_requested   # petición de escritura {store, kind, content} (fire-and-forget)
memory.stored            # confirmación (para logs/tests; nadie lo espera para avanzar)
```

(`llm.finished` no es nuevo pero cambia su payload: + success, + error.)

---

## 5. Orden de implementación (fases)

Cada fase termina con pytest + mypy strict + ruff en verde.

1. **F0 — Config y dependencia**: settings nuevos (`LLMSettings`,
   `MemorySettings`), `httpx` al pyproject, `data/` al gitignore.
2. **F1 — SQLite stores**: `SqliteEpisodicMemory` + `SqliteLongTermMemory` +
   tests con `tmp_path` (escribir/leer, query_by_date, persistencia entre
   conexiones, facts upsert).
3. **F2 — MemoryModule + remember**: evento `memory.store_requested`, grabación
   automática de turnos, cableo de la acción `remember` en el Executor, tests
   (turno grabado al conversar; `remember` persiste; short-term acotada).
4. **F3 — Provider real**: `OpenAICompatProvider` con httpx, health check,
   payload de `llm.finished` con success/error, degradación en el Executor,
   tests con **servidor mock** (httpx MockTransport): éxito, timeout, conexión
   rechazada, modelo inexistente.
5. **F4 — Narración + contexto**: `[HISTORY]` en el prompt, flag `narrate` en
   el Planner, system prompt configurable, tests de integración (plan datetime
   con narrate incluye call_llm; con provider caído responde en crudo).
6. **F5 — Verificación real**: suite completa + prueba manual end-to-end por
   terminal contra LM Studio u Ollama de verdad (ver §6.2) + README
   actualizado (cómo conectar cada backend).

---

## 6. Criterios de aceptación

### 6.1 Automáticos (tests)
1. **Persistencia:** se escriben episodios y facts, se cierra la conexión, se
   reabre el mismo archivo → los datos están. `query_by_date` filtra bien.
2. **Turnos automáticos:** tras un `command.received` + `response.ready`, la
   episódica contiene `user_turn` y `alice_turn` con el mismo
   `correlation_id`.
3. **`remember` real:** un plan con acción `remember` deja el dato en SQLite y
   el plan completa sin esperar confirmación.
4. **Provider (mock):** request bien formada (messages con system + history +
   user), respuesta mapeada a `LLMResponse`; en timeout/conexión rechazada
   emite `llm.finished` con `success=false` y el Executor responde en crudo
   (el plan NO falla).
5. **Narración:** con `narrate=true` el plan datetime incluye `call_llm` y la
   respuesta lleva `text`; con `narrate=false` sale como v1.1. Con
   `provider="null"`, narrate queda forzado a false.
6. `pytest`, `mypy --strict`, `ruff check` sin errores.

### 6.2 Manual (contra backend real, LM Studio u Ollama)
1. `python main.py` → "¿qué hora es?" → respuesta **en prosa natural** con la
   hora correcta (el dato viene de la tool, la redacción del LLM).
2. "Me llamo Adrián, recuérdalo" → fallback LLM responde; el episodio queda en
   `data/alice.db`. Reinicias el proceso → el turno sigue en la base.
3. "Cuéntame un chiste" y luego "cuéntame otro" → el segundo usa `[HISTORY]`
   (se nota en la respuesta o en el log del prompt).
4. Apagas LM Studio/Ollama → "¿qué hora es?" sigue respondiendo (en crudo) y
   el log muestra `llm.provider_down`. Nada crashea.

---

## 7. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Modelo local lento → timeout de plan (30s) se queda corto con narración | El timeout del plan pasa a config y sube a 90s cuando el plan incluye `call_llm`; el del provider es independiente (60s) |
| Ollama/LM Studio no instalado al probar | La degradación (decisión 6) es parte del diseño y tiene test; la prueba manual documenta cómo instalar/arrancar cada backend |
| SQLite bloqueando el loop en ráfagas | WAL + escrituras mínimas; si un test de carga lo desmiente, mover escrituras a `asyncio.to_thread` (cambio local al módulo, no de arquitectura) |
| El LLM inventa datos que no están en OBSERVATIONS | Instrucción explícita + temperatura moderada; la fuente de verdad sigue siendo la Observation (se muestra en logs); validación dura queda para v2.x |
| Historia larga infla el prompt | `history_turns` acotado en config (default 6) |

---

## 8. Fuera de alcance (explícito)

CognitiveState y `set_state` (v1.3), Attention Manager (v1.3), Thoughts y
Drives (v1.4), memoria vectorial/embeddings, resumen diario automático,
streaming de tokens del LLM, más herramientas (internet/shell siguen sin
existir), voz y visión.
