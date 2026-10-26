"""Portable conversation sidecars stored next to the saved CAD document."""

import json
import os
import tempfile
from pathlib import Path


def sidecar_path(cad_path: str) -> Path:
    path = Path(cad_path)
    if not path.is_absolute():
        path = path.resolve()
    return path.with_suffix(".cadnext.json")


def write_sidecar(cad_path: str, data: dict, *, max_bytes: int = 64 * 1024 * 1024) -> Path:
    path = sidecar_path(cad_path)
    if not path.parent.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {path.parent}")
    text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("Conversation sidecar is too large.")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path
