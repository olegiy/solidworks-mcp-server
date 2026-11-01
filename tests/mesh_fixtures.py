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


def make_pocket_bracket():
    import numpy as np

    axes = ([0,5,10,30,35,40], [0,5,25,30], [0,3,5,20,25])
    occupied = set()
    for i in range(5):
        for j in range(3):
            for k in range(4):
                if k < 2 or i == 0:
                    if not (i == 2 and j == 1 and k == 1):
                        occupied.add((i,j,k))
    vertices, faces, indices = [], [], {}
    # Counterclockwise corners when viewed from outside each cell.
    sides = [((-1,0,0), [(0,0,0),(0,0,1),(0,1,1),(0,1,0)]),
             ((1,0,0), [(1,0,0),(1,1,0),(1,1,1),(1,0,1)]),
             ((0,-1,0), [(0,0,0),(1,0,0),(1,0,1),(0,0,1)]),
             ((0,1,0), [(0,1,0),(0,1,1),(1,1,1),(1,1,0)]),
             ((0,0,-1), [(0,0,0),(0,1,0),(1,1,0),(1,0,0)]),
             ((0,0,1), [(0,0,1),(1,0,1),(1,1,1),(0,1,1)])]
    for cell in sorted(occupied):
        for direction, corners in sides:
            if tuple(cell[a]+direction[a] for a in range(3)) in occupied:
                continue
            ids = []
            for corner in corners:
                point = tuple(axes[a][cell[a]+corner[a]] for a in range(3))
                if point not in indices:
                    indices[point] = len(vertices)
                    vertices.append(point)
                ids.append(indices[point])
            a,b,c,d = ids
            faces.extend(((a,b,c), (a,c,d)))
    return MeshFixture(np.asarray(vertices, dtype=float), np.asarray(faces, dtype=int),
                       {'width_mm': 40, 'depth_mm': 30, 'height_mm': 25,
                        'wall_mm': 5, 'pocket_width_mm': 20, 'pocket_depth_mm': 2})


def encode_fixture(fixture, format):
    import trimesh

    encoded = trimesh.Trimesh(fixture.vertices, fixture.faces, process=False).export(file_type=format)
    return encoded.encode() if isinstance(encoded, str) else encoded
