"""Pure geometry helpers for SolidWorks arrays, without additional COM calls."""

import math


def transform_point(point, matrix):
    """Apply MathTransform.ArrayData to a point in metres (row-vector convention)."""
    if (len(point) != 3 or len(matrix) != 16
            or any(type(v) not in {int, float} or not math.isfinite(v) for v in [*point, *matrix])):
        raise ValueError("Нужны три координаты и 16 конечных чисел преобразования.")
    return [matrix[12]*sum(point[j]*matrix[j*3+i] for j in range(3)) + matrix[9+i]
            for i in range(3)]
