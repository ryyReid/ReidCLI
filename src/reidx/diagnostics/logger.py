"""Diagnostics: logging + structured event log.

Foundation is intentionally simple — a tagged logger and a JSONL event writer for
runtime actions. Designed to grow into trace mode and token/usage reporting.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def get_logger(name: str = "reidx") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        # Default to WARNING so INFO logs don't pollute the interactive UI.
        # Set REIDX_LOG_LEVEL=INFO (or DEBUG) to see verbose logs.
        import os

        level = os.environ.get("REIDX_LOG_LEVEL", "WARNING").upper()
        logger.setLevel(getattr(logging, level, logging.WARNING))
    return logger


class EventLog:
    """Append-only JSONL event log for runtime actions (tool calls, decisions, runs)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, data: dict[str, Any] | None = None) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "data": data or {},
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
