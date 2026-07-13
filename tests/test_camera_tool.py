"""Tests de la tool camera: consulta /state del plugin vision.

La ruta feliz (visión activa) se valida en vivo; aquí cubrimos la degradación:
si la visión no responde, la tool no debe fallar, solo marcar available=False.
"""

from __future__ import annotations

from alice.tools.builtin.camera_tool import CameraParams, CameraTool


async def test_camera_degrades_when_vision_down() -> None:
    # Puerto donde no hay nada escuchando: la conexión falla.
    tool = CameraTool(base_url="http://127.0.0.1:9", timeout=0.5)
    result = await tool.execute(CameraParams())
    assert result.success is True  # no ver no es un error del sistema
    assert result.output == {"available": False}
