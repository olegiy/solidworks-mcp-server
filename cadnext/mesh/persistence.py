"""Hash-addressed portable sources; uploaded manifests never provide read paths."""

import hashlib
import os
import re
import stat
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath

from cadnext.mesh.contracts import MeshError, MeshLimits, MeshReference


def validated_reference(raw):
    if not isinstance(raw, dict):
        raise MeshError("invalid_manifest", "Некорректный эталон в переписке.")
    try:
        reference = MeshReference(**{key: raw[key] for key in (
            "asset_id", "sha256", "format", "units", "tolerance_mm", "source_to_cad")})
        if (not isinstance(reference.asset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", reference.asset_id)
                or not isinstance(reference.sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", reference.sha256)
                or raw.get("frame_id") != reference.frame_id or raw.get("scale_to_mm") != reference.scale_to_mm):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise MeshError("invalid_manifest", "Эталон в переписке не прошёл проверку.") from None
    return reference.snapshot()


def validate_manifest(raw):
    if not isinstance(raw, dict):
        raise MeshError("invalid_manifest", "Некорректные сведения об исходнике сетки.")
    reference = validated_reference(raw.get("reference"))
    relative = raw.get("relative_path")
    size = raw.get("size_bytes")
    if relative is None and size is None:
        return {"reference": reference, "relative_path": None, "size_bytes": None}
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise MeshError("invalid_manifest", "Недопустимый путь исходника сетки.")
    parts = PurePosixPath(relative).parts
    if (len(parts) != 2 or not parts[0].endswith(".cadnext.assets")
            or parts[0] in {".", ".."} or any(ord(c) < 32 for c in relative)
            or parts[1] != f'{reference["sha256"]}.{reference["format"]}'
            or type(size) is not int or not 0 < size <= MeshLimits().max_bytes):
        raise MeshError("invalid_manifest", "Некорректный путь или размер исходника сетки.")
    return {"reference": reference, "relative_path": relative, "size_bytes": size}


def _regular_path(path):
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise MeshError("unsafe_asset", "Ссылки и точки повторной обработки для исходника запрещены.")
        if part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise MeshError("unsafe_asset", "Точка повторной обработки для исходника запрещена.")


def copy_verified(source, destination, reference, *, expected_size=None):
    source, destination = Path(source), Path(destination)
    _regular_path(source)
    _regular_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _regular_path(destination.parent)
    descriptor, temporary = tempfile.mkstemp(prefix="mesh-", suffix=".tmp", dir=destination.parent)
    try:
        digest, size = hashlib.sha256(), 0
        with source.open("rb") as origin, os.fdopen(descriptor, "wb") as target:
            descriptor = None
            while chunk := origin.read(1024*1024):
                size += len(chunk)
                if size > MeshLimits().max_bytes:
                    raise MeshError("byte_limit", "Исходник сетки превышает лимит.")
                digest.update(chunk)
                target.write(chunk)
            if (not size or digest.hexdigest() != reference["sha256"]
                    or expected_size is not None and size != expected_size):
                raise MeshError("asset_changed", "Размер или хеш исходника сетки изменился.")
            target.flush()
            os.fsync(target.fileno())
        _regular_path(destination)
        os.replace(temporary, destination)
        return size
    finally:
        if descriptor is not None:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def publish_asset(cad_path, source, reference):
    reference = validated_reference(reference)
    cad = Path(cad_path).absolute()
    relative = f'{cad.name}.cadnext.assets/{reference["sha256"]}.{reference["format"]}'
    size = copy_verified(source, cad.parent / relative, reference)
    return validate_manifest({"reference": deepcopy(reference), "relative_path": relative, "size_bytes": size})
