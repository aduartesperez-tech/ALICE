# Prompt para Opus — Implementar Alice Core v1.0

> Copia desde aquí hacia abajo y pégalo como prompt. Requiere que
> `PLAN_ALICE_CORE.md` esté en la raíz del proyecto (ya lo está).

---

Implementa **Alice Core v1.0** siguiendo al pie de la letra el documento
`PLAN_ALICE_CORE.md` que está en la raíz de este proyecto. Léelo completo antes
de escribir la primera línea de código: contiene las decisiones de arquitectura
ya cerradas, la estructura de archivos, los contratos de cada componente, las
fases de implementación y los criterios de aceptación. No re-decidas nada que
el plan ya decide.

## Qué es

El núcleo (sistema nervioso) de una IA modular. **No es un chatbot.** Es una
plataforma asíncrona que recibe eventos de módulos (voz, visión, herramientas,
LLM...), los distribuye mediante un EventBus propio y decide con un Planner
basado en reglas cuándo usar herramientas y cuándo (minimizándolo) un LLM.
En esta versión NO se implementa ninguna IA real, ni voz, ni visión, ni
herramientas peligrosas: solo el núcleo, con interfaces preparadas.

## Reglas de trabajo

1. **Python 3.12+, tipado completo.** `mypy --strict` debe pasar sobre todo el
   paquete. Pydantic v2 para todos los modelos de datos. Eventos inmutables.
2. **Implementa por fases, en el orden de la sección 4 del plan** (Fase 0 a
   Fase 7). Al terminar cada fase ejecuta `pytest`, `mypy --strict` y
   `ruff check`; no avances a la siguiente fase con algo en rojo.
3. **Desacoplamiento estricto:** los módulos se comunican SOLO por eventos.
   Ningún plugin importa otro plugin; `brain/` y `tools/` solo importan
   contratos de `core/`. El Orchestrator es la única composition root y no
   contiene lógica de negocio. Trátalo como regla de revisión: si un import
   viola esto, es un bug.
4. **Dependencias mínimas:** solo `pydantic`, `pydantic-settings`, `pytest`,
   `pytest-asyncio`, `ruff`, `mypy`. Nada más sin justificación.
5. **Archivos pequeños, una responsabilidad por módulo.** Si un archivo pasa
   de ~300 líneas, probablemente hay que dividirlo.
6. **Logging estructurado JSON:** cada evento publicado y cada decisión del
   planner (comando, regla aplicada, plan resultante) quedan registrados, con
   `correlation_id` para seguir un flujo completo.
7. **Tests significativos**, no de relleno: prioridades del bus, handler que
   lanza excepción sin tumbar el bus, plugin roto que no impide el arranque,
   validación de parámetros y permisos en ToolManager, decisiones del planner
   ("¿qué hora es?" → tool datetime sin LLM; "busca en internet X" → tool
   internet + LLM), y el test de integración end-to-end del plan.
8. Comentarios y docstrings en español, escuetos; el código se explica solo.

## Definición de terminado

Los 6 criterios de aceptación de la sección 5 del plan se cumplen. En
particular: `python main.py` arranca y se apaga limpio con Ctrl+C; el plugin
de ejemplo `plugins/echo/` se carga por descubrimiento automático; el flujo
comando → planner → ToolManager → resultado funciona de punta a punta sin
ningún LLM real; y `pytest`, `mypy --strict` y `ruff check` pasan sin errores.
Termina con un `README.md` que explique cómo arrancar el sistema y cómo crear
un plugin nuevo (carpeta + manifest + clase).

Cuando termines, entrega un resumen de: estructura creada, cómo correr los
tests, y qué quedó como interfaz preparada (LLM, memoria episódica/larga,
herramientas futuras) para las siguientes versiones.
