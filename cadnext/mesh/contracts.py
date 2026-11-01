"""Immutable input contracts for geometry measured in millimetres."""

import hashlib
import json
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


@dataclass(frozen=True)
class PlaneFrame:
    origin_mm: tuple
    x_axis: tuple
    y_axis: tuple
    normal: tuple

    def __post_init__(self):
        try:
            values = np.asarray((self.origin_mm, self.x_axis, self.y_axis, self.normal), dtype=float)
            axes = values[1:]
            valid = (values.shape == (4, 3) and np.isfinite(values).all() and
                     np.allclose(axes @ axes.T, np.eye(3), atol=1e-8, rtol=0) and
                     np.allclose(np.cross(axes[0], axes[1]), axes[2], atol=1e-8, rtol=0))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise MeshError("invalid_frame", "Нужна правая ортонормальная система координат плоскости.")
        for name, value in zip(("origin_mm", "x_axis", "y_axis", "normal"), values, strict=True):
            object.__setattr__(self, name, tuple(float(x) for x in value))

    @property
    def frame_id(self):
        values = [self.origin_mm, self.x_axis, self.y_axis, self.normal]
        return hashlib.sha256(json.dumps(values, allow_nan=False).encode()).hexdigest()

    def to_cad(self, xy):
        return (np.asarray(self.origin_mm)+np.asarray(xy) @ np.asarray([self.x_axis, self.y_axis])).tolist()


@dataclass(frozen=True)
class SectionResult:
    sha256: str
    frame_id: str
    frame: dict
    tolerance_mm: float
    numerical_epsilon_mm: float
    chains: list
    usable_for_sketch: bool
    reasons: tuple[str, ...]
