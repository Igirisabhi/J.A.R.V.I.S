"""Validated, atomic JSON persistence for runtime configuration files."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def load_json(path: Path, default: T) -> T:
    """Load JSON or return a caller-provided safe default."""
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError):
        return default


def save_json_atomic(
    path: Path,
    value: object,
    *,
    validator: Callable[[object], None] | None = None,
) -> None:
    """Validate and replace a JSON file without exposing partial writes."""
    if validator is not None:
        validator(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
