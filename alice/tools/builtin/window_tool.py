"""WindowManagerTool: listar, enfocar, minimizar y cerrar ventanas. Riesgo medio.

Usa ``pygetwindow`` (extra opcional ``[control]``). Si no está instalado, degrada
con un mensaje claro en vez de romper.

Las ventanas se buscan por coincidencia parcial del título (sin distinguir
mayúsculas), porque el usuario dice "enfoca el navegador", no el título exacto.
Cerrar una ventana pide confirmación: puede haber trabajo sin guardar.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from alice.logging import get_logger
from alice.tools.tool import Permission, Tool, ToolDefinition, ToolResult

_logger = get_logger("alice.tools.window")

_MAX_LISTED = 25


class WindowParams(BaseModel):
    """Parámetros de la tool de ventanas."""

    action: Literal["list", "focus", "minimize", "maximize", "close"] = Field(
        default="list", description="Qué hacer con las ventanas."
    )
    title: str = Field(
        default="", description="Parte del título de la ventana (no hace falta el exacto)."
    )


def _load_gw() -> Any:
    try:
        import pygetwindow
    except ImportError:
        return None
    return pygetwindow


class WindowManagerTool(Tool):
    """Gestiona las ventanas abiertas del escritorio."""

    definition = ToolDefinition(
        name="window",
        description=(
            "Gestiona las ventanas abiertas: action='list' para ver cuáles hay, y "
            "'focus' / 'minimize' / 'maximize' / 'close' indicando parte del título."
        ),
        parameters=WindowParams,
        permissions={Permission.WRITE_SYSTEM},
        audit=True,
    )

    def confirmation_question(self, params: BaseModel) -> str | None:
        assert isinstance(params, WindowParams)
        if params.action != "close":
            return None  # mirar, enfocar o minimizar no destruye nada
        return f"Voy a cerrar la ventana «{params.title}». ¿La cierro? Dime sí o no."

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, WindowParams)
        gw = _load_gw()
        if gw is None:
            return ToolResult(
                success=False,
                error="no puedo gestionar ventanas: falta pygetwindow "
                '(pip install -e ".[control]")',
            )

        if params.action == "list":
            titles = [t for t in gw.getAllTitles() if t.strip()][:_MAX_LISTED]
            return ToolResult(success=True, output={"ventanas": titles})

        if not params.title.strip():
            return ToolResult(success=False, error="dime de qué ventana se trata")
        matches = self._matching_titles(gw, params.title)
        if not matches:
            return ToolResult(
                success=False, error=f"no encontré ninguna ventana que contenga '{params.title}'"
            )
        if len(matches) > 1:
            # Nunca adivinar: actuar sobre la ventana equivocada puede cerrar
            # trabajo sin guardar. Se devuelven las candidatas para concretar.
            return ToolResult(
                success=False,
                output={"candidatas": matches},
                error=(
                    f"hay {len(matches)} ventanas que encajan con '{params.title}'. "
                    "Dime cuál con más detalle del título."
                ),
            )
        window = self._first_window(gw, matches[0])
        if window is None:
            return ToolResult(success=False, error=f"la ventana '{matches[0]}' ya no está")

        try:
            return self._act(window, params.action)
        except Exception as exc:  # noqa: BLE001 - la API de ventanas es frágil
            _logger.warning("window.action_failed", extra={"error": str(exc)})
            return ToolResult(success=False, error=f"no pude {params.action} esa ventana: {exc}")

    @staticmethod
    def _matching_titles(gw: Any, needle: str) -> list[str]:
        """Títulos que contienen el texto buscado. Exacto gana a parcial.

        Si el usuario da el título justo, esa es la ventana aunque otras lo
        contengan (evita que "Notepad" case con media docena de ventanas).
        """
        target = needle.strip().lower()
        titles = [t for t in gw.getAllTitles() if t.strip()]
        exact = [t for t in titles if t.lower() == target]
        if exact:
            return exact[:1]
        return [t for t in titles if target in t.lower()]

    @staticmethod
    def _first_window(gw: Any, title: str) -> Any:
        matches = gw.getWindowsWithTitle(title)
        return matches[0] if matches else None

    @staticmethod
    def _act(window: Any, action: str) -> ToolResult:
        title = window.title
        result: dict[str, Any] = {"ventana": title, "accion": action}
        if action == "focus":
            # Si está minimizada, restaurar antes: activar una minimizada no la muestra.
            if getattr(window, "isMinimized", False):
                window.restore()
            try:
                window.activate()
            except Exception:  # noqa: BLE001 - Windows bloquea el robo de foco
                # Error 258: Windows no deja que un proceso en segundo plano pase
                # una ventana al frente. Minimizar y restaurar sí lo consigue.
                window.minimize()
                window.restore()
        elif action == "minimize":
            window.minimize()
        elif action == "maximize":
            window.maximize()
        else:
            window.close()
        _logger.info("window.action", extra={"action": action, "title": title})
        return ToolResult(success=True, output=result)
