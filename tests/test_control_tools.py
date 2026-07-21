"""Tests del control del PC (Fase 3): apps, sistema y ventanas.

Se centran en las barandillas y en la lógica pura (resolución de nombres,
qué pide confirmación, degradación sin las librerías opcionales). No se abren
apps ni se apaga nada de verdad: eso se verificó a mano.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from alice.tools.builtin.app_tool import AppLauncherTool, AppParams
from alice.tools.builtin.system_tool import SystemControlTool, SystemParams
from alice.tools.builtin.window_tool import WindowManagerTool, WindowParams
from alice.tools.tool import Permission

if TYPE_CHECKING:
    from pathlib import Path


# --- app_launcher ----------------------------------------------------------

def test_app_tool_permissions_and_audit() -> None:
    tool = AppLauncherTool()
    assert Permission.WRITE_SYSTEM in tool.definition.permissions
    assert tool.definition.audit is True


@pytest.mark.parametrize(
    ("spoken", "expected_process"),
    [
        ("bloc de notas", "notepad.exe"),
        ("Calculadora", "CalculatorApp.exe"),
        ("spotify", "Spotify.exe"),
        ("ajustes", "SystemSettings.exe"),
    ],
)
def test_app_aliases_resolve(spoken: str, expected_process: str) -> None:
    # El usuario habla en español; la tool traduce al ejecutable.
    _, process = AppLauncherTool._resolve(spoken)  # noqa: SLF001
    assert process == expected_process


def test_unknown_app_falls_back_to_its_name() -> None:
    target, process = AppLauncherTool._resolve("miprograma")  # noqa: SLF001
    assert target == "miprograma"
    assert process == "miprograma.exe"


async def test_critical_process_is_never_closed() -> None:
    # Matar lsass tumba Windows: se rechaza sin más.
    tool = AppLauncherTool()
    result = await tool.execute(AppParams(action="close", app="lsass"))
    assert result.success is False
    assert "crítico" in (result.error or "")


async def test_empty_app_is_rejected() -> None:
    result = await AppLauncherTool().execute(AppParams(action="open", app="  "))
    assert result.success is False


def test_opening_an_app_needs_no_confirmation() -> None:
    tool = AppLauncherTool()
    assert tool.confirmation_question(AppParams(action="open", app="bloc de notas")) is None


# --- system_control --------------------------------------------------------

def test_system_tool_permissions_and_audit() -> None:
    tool = SystemControlTool()
    assert Permission.WRITE_SYSTEM in tool.definition.permissions
    assert tool.definition.audit is True


@pytest.mark.parametrize("action", ["shutdown", "restart", "sleep"])
def test_power_actions_always_confirm(action: str) -> None:
    # Lo irreversible en el momento nunca se hace a la ligera.
    tool = SystemControlTool()
    question = tool.confirmation_question(SystemParams(action=action))  # type: ignore[arg-type]
    assert question is not None


@pytest.mark.parametrize("action", ["volume_get", "mute", "lock", "screenshot"])
def test_ordinary_actions_do_not_confirm(action: str) -> None:
    tool = SystemControlTool()
    assert tool.confirmation_question(SystemParams(action=action)) is None  # type: ignore[arg-type]


async def test_volume_set_without_level_is_rejected() -> None:
    result = await SystemControlTool().execute(SystemParams(action="volume_set"))
    assert result.success is False
    assert "0-100" in (result.error or "")


async def test_volume_degrades_without_pycaw(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sin la librería opcional, la tool avisa; no revienta ni tumba a Alice.
    tool = SystemControlTool()
    monkeypatch.setattr(SystemControlTool, "_endpoint", staticmethod(lambda: None))
    result = await tool.execute(SystemParams(action="volume_get"))
    assert result.success is False
    assert "pycaw" in (result.error or "")


async def test_volume_is_clamped_to_range(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    class _FakeEndpoint:
        @staticmethod
        def SetMasterVolumeLevelScalar(value: float, _ctx: object) -> None:  # noqa: N802
            seen.append(value)

    monkeypatch.setattr(SystemControlTool, "_endpoint", staticmethod(lambda: _FakeEndpoint()))
    tool = SystemControlTool()
    await tool.execute(SystemParams(action="volume_set", level=500))
    await tool.execute(SystemParams(action="volume_set", level=-20))
    assert seen == [1.0, 0.0]  # 500 -> 100%, -20 -> 0%


# --- window_manager --------------------------------------------------------

def test_window_tool_permissions_and_audit() -> None:
    tool = WindowManagerTool()
    assert Permission.WRITE_SYSTEM in tool.definition.permissions
    assert tool.definition.audit is True


def test_closing_a_window_confirms_but_looking_does_not() -> None:
    tool = WindowManagerTool()
    assert tool.confirmation_question(WindowParams(action="list")) is None
    assert tool.confirmation_question(WindowParams(action="focus", title="x")) is None
    question = tool.confirmation_question(WindowParams(action="close", title="Bloc de notas"))
    assert question is not None
    assert "Bloc de notas" in question


async def test_window_action_without_title_is_rejected() -> None:
    result = await WindowManagerTool().execute(WindowParams(action="focus", title=""))
    assert result.success is False


async def test_window_degrades_without_pygetwindow(monkeypatch: pytest.MonkeyPatch) -> None:
    import alice.tools.builtin.window_tool as mod

    monkeypatch.setattr(mod, "_load_gw", lambda: None)
    result = await WindowManagerTool().execute(WindowParams(action="list"))
    assert result.success is False
    assert "pygetwindow" in (result.error or "")


class _FakeGw:
    """pygetwindow falso con los títulos que se le den."""

    def __init__(self, titles: list[str]) -> None:
        self._titles = titles

    def getAllTitles(self) -> list[str]:  # noqa: N802
        return self._titles

    def getWindowsWithTitle(self, title: str) -> list[object]:  # noqa: N802
        return []


async def test_window_not_found_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import alice.tools.builtin.window_tool as mod

    monkeypatch.setattr(mod, "_load_gw", lambda: _FakeGw(["Visual Studio Code", "Explorador"]))
    result = await WindowManagerTool().execute(
        WindowParams(action="focus", title="no_existe_esta_ventana")
    )
    assert result.success is False
    assert "no encontré" in (result.error or "")


async def test_ambiguous_title_asks_instead_of_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Con varias ventanas que encajan, actuar sobre la primera podría cerrar
    # la equivocada y perder trabajo: se pide concretar.
    import alice.tools.builtin.window_tool as mod

    monkeypatch.setattr(
        mod, "_load_gw", lambda: _FakeGw(["notas.txt - Notepad", "*informe - Notepad"])
    )
    result = await WindowManagerTool().execute(WindowParams(action="close", title="Notepad"))
    assert result.success is False
    assert "2 ventanas" in (result.error or "")
    assert result.output is not None
    assert len(result.output["candidatas"]) == 2  # se ofrecen para elegir


def test_exact_title_wins_over_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    # Si el usuario da el título justo, esa es la ventana aunque otras lo contengan.
    import alice.tools.builtin.window_tool as mod

    gw = _FakeGw(["Notepad", "notas.txt - Notepad"])
    assert mod.WindowManagerTool._matching_titles(gw, "Notepad") == ["Notepad"]  # noqa: SLF001


async def test_window_list_returns_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    import alice.tools.builtin.window_tool as mod

    monkeypatch.setattr(
        mod, "_load_gw", lambda: _FakeGw(["Visual Studio Code", "", "   ", "Spotify"])
    )
    result = await WindowManagerTool().execute(WindowParams(action="list"))
    assert result.success is True
    assert result.output == {"ventanas": ["Visual Studio Code", "Spotify"]}


async def test_screenshot_goes_to_its_folder(tmp_path: Path) -> None:
    # La captura real se verificó a mano; aquí solo que apunta a su carpeta.
    tool = SystemControlTool(tmp_path / "shots")
    assert tool._shots.name == "shots"  # noqa: SLF001
