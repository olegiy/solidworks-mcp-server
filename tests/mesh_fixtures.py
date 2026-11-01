"""Original analytic fixtures, generated without CAD or third-party sample files."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class MeshFixture:
    vertices: object
    faces: object
    expected: dict


def make_plate_with_hole():
    import numpy as np

    angles = sorted({*(i * math.tau / 64 for i in range(64)),
                     *(math.atan2(y, x) % math.tau for x in (-20, 20) for y in (-15, 15))})
    outer, inner = [], []
    for angle in angles:
        c, s = math.cos(angle), math.sin(angle)
        radius = min(20 / abs(c) if abs(c) > 1e-12 else math.inf,
                     15 / abs(s) if abs(s) > 1e-12 else math.inf)
        outer.append((radius * c, radius * s))
        inner.append((5 * c, 5 * s))
    n = len(angles)
    vertices = [(x, y, z) for z in (0, 5) for loop in (outer, inner) for x, y in loop]
    faces = []
    for i in range(n):
        j = (i + 1) % n
        # Bottom, top, outside wall, and bore wall, all wound outwards.
        faces.extend(((i, n+j, j), (i, n+i, n+j),
                      (2*n+i, 2*n+j, 3*n+j), (2*n+i, 3*n+j, 3*n+i),
                      (i, j, 2*n+j), (i, 2*n+j, 2*n+i),
                      (n+i, 3*n+j, n+j), (n+i, 3*n+i, 3*n+j)))
    return MeshFixture(np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64),
                       {"width_mm": 40, "height_mm": 30, "thickness_mm": 5,
                        "hole_diameter_mm": 10})


def make_stepped_bushing():
    import numpy as np

    profile = [(5, 0), (15, 0), (15, 15), (10, 15), (10, 40), (5, 40)]
    n = 128
    vertices = [(r * math.cos(i * math.tau/n), r * math.sin(i * math.tau/n), z)
                for r, z in profile for i in range(n)]
    faces = []
    for ring in range(len(profile)):
        next_ring = (ring + 1) % len(profile)
        for i in range(n):
            j = (i + 1) % n
            a, b, c, d = ring*n+i, ring*n+j, next_ring*n+j, next_ring*n+i
            faces.extend(((a, b, c), (a, c, d)))
    return MeshFixture(np.asarray(vertices, dtype=np.float64), np.asarray(faces, dtype=np.int64),
                       {"height_mm": 40, "flange_diameter_mm": 30,
                        "stem_diameter_mm": 20, "hole_diameter_mm": 10})
