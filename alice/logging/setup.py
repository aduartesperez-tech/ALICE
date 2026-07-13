"""Configuración de logging estructurado en JSON (solo stdlib).

Emite cada registro como una línea JSON. Los campos extra que se pasen mediante
``logger.info(msg, extra={...})`` se incluyen en la salida, lo que permite
adjuntar ``event_id``, ``correlation_id``, etc. a cada log.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Atributos estándar de LogRecord; todo lo demás se considera "extra".
_STANDARD_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Formatea cada LogRecord como una línea JSON con sus campos extra."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Path = Path("logs"),
    file_name: str = "alice.log",
    to_console: bool = True,
) -> None:
    """Configura el logger raíz con salida JSON a archivo rotativo y (opcional) consola."""
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        log_dir / file_name,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if to_console:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)


def get_logger(name: str) -> logging.Logger:
    """Devuelve un logger con nombre bajo el árbol ``alice``."""
    return logging.getLogger(name)
