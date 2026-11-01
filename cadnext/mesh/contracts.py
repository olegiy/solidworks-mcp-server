"""Immutable input contracts for geometry measured in millimetres."""

import math
from dataclasses import dataclass

import numpy as np


class MeshError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MeshLimits:
    max_bytes: int = 100 * 1024 * 1024
    max_triangles: int = 250_000
    max_vertices: int = 250_000
    max_line_bytes: int = 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in (
                self.max_bytes, self.max_triangles, self.max_vertices, self.max_line_bytes)):
            raise ValueError("Лимиты сетки должны быть положительными целыми числами.")


@dataclass(frozen=True)
class MeshData:
    vertices: np.ndarray
    faces: np.ndarray
    sha256: str
    units: str
    scale_to_mm: float


def input_scale(format: str, units: str) -> float:
    if format not in {"stl", "obj"}:
        raise MeshError("unsupported_format", "Выберите файл STL или треугольный OBJ.")
    if units not in {"mm", "in"}:
        raise MeshError("unknown_units", "Укажите единицы исходной сетки: миллиметры или дюймы.")
    return 1.0 if units == "mm" else 25.4


def validate_tolerance(value) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 10:
        raise MeshError("invalid_tolerance", "Допуск должен быть конечным числом от 0 до 10 мм (исключая 0).")
    return float(value)


@dataclass(frozen=True)
class MeshSummary:
    sha256: str
    bounds_mm: list
    vertices: int
    triangles: int
    components: int
    boundary_edges: int
    nonmanifold_edges: int
    watertight: bool
    winding_consistent: bool
    volume_mm3: float | None
    area_mm2: float
    pca_ambiguous: bool
    source_to_cad: None
    regions: list


@dataclass(frozen=True)
class FitResult:
    kind: str
    region_id: str
    parameters: dict
    used_points: int
    rms_mm: float
    max_residual_mm: float
    coverage: dict
    degeneracies: tuple[str, ...]
    usable: bool
