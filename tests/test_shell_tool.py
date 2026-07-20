"""Tests del ShellTool: denylist dura, allowlist de lectura y confirmación."""

from __future__ import annotations

import pytest

from alice.tools.builtin.shell_tool import ShellParams, ShellTool
from alice.tools.tool import Permission


def _tool() -> ShellTool:
    return ShellTool(timeout_seconds=15.0)


def test_definition_requires_shell_and_audits() -> None:
    tool = _tool()
    assert Permission.SHELL in tool.definition.permissions
    assert tool.definition.audit is True


# --- Nivel 1: prohibido siempre -------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "format C: /fs:ntfs",
        "del /s /q C:\\Windows",
        "rmdir /s /q C:\\",
        "rm -rf /",
        "shutdown /s /t 0",
        "reg delete HKLM\\Software /f",
        "vssadmin delete shadows /all",
        "bcdedit /set safeboot minimal",
        "diskpart",
        "sc delete spooler",
        "takeown /f C:\\Windows",
        "dd if=/dev/zero of=/dev/sda",
    ],
)
async def test_destructive_commands_are_refused(command: str) -> None:
    tool = _tool()
    # Ni siquiera se pregunta: se rechazan de plano.
    assert tool.confirmation_question(ShellParams(command=command)) is None
    result = await tool.execute(ShellParams(command=command))
    assert result.success is False
    assert "me niego" in (result.error or "")


async def test_destructive_inside_a_chain_is_also_refused() -> None:
    # La denylist mira TODO el comando, no solo la primera palabra.
    tool = _tool()
    result = await tool.execute(ShellParams(command="echo hola && format C:"))
    assert result.success is False
    assert "me niego" in (result.error or "")


# --- Nivel 2: solo lectura, sin molestar ----------------------------------

@pytest.mark.parametrize("command", ["whoami", "dir", "hostname", "git status", "pip list"])
def test_read_only_commands_need_no_confirmation(command: str) -> None:
    tool = _tool()
    assert tool.is_read_only(command) is True
    assert tool.confirmation_question(ShellParams(command=command)) is None


def test_chaining_loses_the_read_only_pass() -> None:
    # `dir` es inofensivo, pero `dir && otra_cosa` puede ser cualquier cosa.
    tool = _tool()
    assert tool.is_read_only("dir && echo algo") is False
    assert tool.confirmation_question(ShellParams(command="dir && echo algo")) is not None


def test_write_subcommand_is_not_read_only() -> None:
    tool = _tool()
    assert tool.is_read_only("git push") is False  # git status sí, git push no
    assert tool.is_read_only("pip install requests") is False


# --- Nivel 3: pide confirmación -------------------------------------------

def test_unknown_command_asks_for_confirmation() -> None:
    tool = _tool()
    question = tool.confirmation_question(ShellParams(command="mkdir cosas_nuevas"))
    assert question is not None
    assert "mkdir cosas_nuevas" in question  # el usuario ve QUÉ va a ejecutarse


# --- Ejecución real --------------------------------------------------------

async def test_read_only_command_runs_and_captures_output() -> None:
    tool = _tool()
    result = await tool.execute(ShellParams(command="echo hola-alice"))
    assert result.success is True, result.error
    assert result.output is not None
    assert "hola-alice" in result.output["stdout"]
    assert result.output["exit_code"] == 0


async def test_failing_command_reports_exit_code() -> None:
    tool = _tool()
    result = await tool.execute(ShellParams(command="exit 4"))
    assert result.success is False
    assert result.output is not None
    assert result.output["exit_code"] == 4


async def test_empty_command_is_rejected() -> None:
    result = await _tool().execute(ShellParams(command="   "))
    assert result.success is False


async def test_timeout_kills_runaway_command() -> None:
    tool = ShellTool(timeout_seconds=0.5)
    # `timeout` es un comando de Windows que espera N segundos.
    result = await tool.execute(ShellParams(command="ping -n 20 127.0.0.1"))
    assert result.success is False
    assert "super" in (result.error or "")
