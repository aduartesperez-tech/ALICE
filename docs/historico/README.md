# Documentos históricos

Documentos de planeación de versiones **ya entregadas**. Se conservan por su
valor de contexto (decisiones de diseño y su porqué), pero **no son la hoja de
ruta vigente**: esa es [`PLAN_DEFINITIVO.md`](../../PLAN_DEFINITIVO.md) en la raíz.

- **`PLAN_ALICE_CORE.md`** — plan técnico de la v1.0: el núcleo por eventos
  (EventBus, Orchestrator, Scheduler, Planner de reglas, ToolManager, interfaces
  de LLM y memoria). Implementado; los contratos viven hoy en el código.
- **`PROMPT_OPUS.md`** — el prompt que se usó para implementar la v1.0 a partir
  del plan anterior. Artefacto de proceso.
- **`PLAN_V1_2.md`** — v1.2: memoria persistente (SQLite) + primer proveedor LLM
  real (OpenAI-compatible). Implementado.
- **`SPEC_ALICE_KERNEL_V2.md`** — arquitectura de los 5 pilares cognitivos. El
  Action Executor está implementado; los otros cuatro (CognitiveState, Attention,
  Thoughts, Drives) siguen pendientes y su contrato se rescató al **Anexo A** de
  `PLAN_DEFINITIVO.md`.
- **`ROADMAP.md`** — diagnóstico de los dos problemas observados (cámara no
  consultable, tools no usadas) y fases 1-6. Superado por `PLAN_DEFINITIVO.md`
  (las fases 1 y 2 ya están hechas).
