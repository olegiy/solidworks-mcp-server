"""Triangle-plane intersection and conservative profiles in an explicit frame."""

import hashlib
from collections import defaultdict
from dataclasses import asdict

import numpy as np
from rtree.index import Index
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from cadnext.mesh.analysis import _circle
from cadnext.mesh.contracts import SectionResult, validate_tolerance


def _intersections(mesh, frame, epsilon):
    vertices = mesh.vertices
    distances = (vertices-np.asarray(frame.origin_mm)) @ np.asarray(frame.normal)
    distances[np.abs(distances) <= epsilon] = 0
    face_distances = distances[mesh.faces]
    candidates = np.flatnonzero((face_distances.min(axis=1) <= 0) & (face_distances.max(axis=1) >= 0))
    ordinary, coplanar = [], []
    for face_id in candidates:
        ids = mesh.faces[face_id]
        points, d = vertices[ids], distances[ids]
        if np.all(d == 0):
            for a, b in ((0, 1), (1, 2), (2, 0)):
                coplanar.append((points[a], points[b], int(face_id)))
            continue
        hits = [points[i] for i in range(3) if d[i] == 0]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if d[a]*d[b] < 0:
                hits.append(points[a]+(points[b]-points[a]) * (d[a]/(d[a]-d[b])))
        if len(hits) == 2 and np.linalg.norm(hits[0]-hits[1]) > epsilon:
            ordinary.append((*hits, int(face_id)))
    return ordinary, coplanar


def _graph(ordinary, coplanar, frame, epsilon):
    segments = ordinary+coplanar
    if not segments:
        return np.empty((0, 2)), {}, {}
    xyz = np.array([p for a, b, _ in segments for p in (a, b)])
    xy = (xyz-np.asarray(frame.origin_mm)) @ np.asarray([frame.x_axis, frame.y_axis]).T
    neighbours = cKDTree(xy).query_pairs(epsilon, output_type="ndarray")
    graph = coo_matrix((np.ones(len(neighbours)), (neighbours[:, 0], neighbours[:, 1])),
                       shape=(len(xy), len(xy)))
    count, labels = connected_components(graph, directed=False)
    first = np.full(count, len(xy), dtype=int)
    np.minimum.at(first, labels, np.arange(len(xy)))
    points = xy[first]
    ordinary_edges, planar_edges = defaultdict(set), defaultdict(list)
    for i, (_, _, face_id) in enumerate(segments):
        a, b = labels[2*i:2*i+2]
        if a == b:
            continue
        edge = tuple(sorted((int(a), int(b))))
        if i < len(ordinary):
            ordinary_edges[edge].add(face_id)
        else:
            planar_edges[edge].append(face_id)
    edges = dict(ordinary_edges)
    for edge, sources in planar_edges.items():
        if len(sources) == 1:
            edges.setdefault(edge, set()).update(sources)
    adjacency = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    return points, edges, adjacency


def _chains(edges, adjacency):
    remaining = set(edges)
    chains = []
    starts = sorted(adjacency, key=lambda node: (len(adjacency[node]) == 2, node))
    for start in starts:
        while any(tuple(sorted((start, n))) in remaining for n in adjacency[start]):
            path, current = [start], start
            while True:
                candidates = sorted(n for n in adjacency[current] if tuple(sorted((current, n))) in remaining)
                if not candidates:
                    break
                following = candidates[0]
                remaining.remove(tuple(sorted((current, following))))
                path.append(following)
                current = following
                if current == start or len(adjacency[current]) != 2:
                    break
            chains.append(path)
    return chains


def _has_crossing(points, edges, epsilon):
    tree = Index()
    indexed = []

    def cross(a, b):
        return a[0]*b[1]-a[1]*b[0]

    try:
        for a, b in edges:
            p, q = points[a], points[b]
            low, high = np.minimum(p, q)-epsilon, np.maximum(p, q)+epsilon
            box = (*low, *high)
            for candidate in tree.intersection(box):
                c, d = indexed[candidate]
                if a in (c, d) or b in (c, d):
                    continue
                r, s = points[c], points[d]
                u, v = q-p, s-r
                denominator = cross(u, v)
                if abs(denominator) > epsilon*max(np.linalg.norm(u), np.linalg.norm(v)):
                    t, w = cross(r-p, v)/denominator, cross(r-p, u)/denominator
                    if 0 <= t <= 1 and 0 <= w <= 1:
                        return True
                elif abs(cross(r-p, u)) <= epsilon*np.linalg.norm(u):
                    # Collinear non-neighbouring edges with overlapping bounding boxes.
                    return True
            tree.insert(len(indexed), box)
            indexed.append((a, b))
    finally:
        tree.close()
    return False


def _area(points):
    return float(np.sum(points[:, 0]*np.roll(points[:, 1], -1)-points[:, 1]*np.roll(points[:, 0], -1))/2)


def _inside(point, polygon):
    x, y = point
    count = 0
    for a, b in zip(polygon, np.roll(polygon, -1, axis=0), strict=True):
        if (a[1] > y) != (b[1] > y) and x < a[0]+(b[0]-a[0])*(y-a[1])/(b[1]-a[1]):
            count += 1
    return count % 2 == 1


def _segments(points, sources, closed, frame, tolerance, epsilon, prefix):
    output = []
    # Diagonal splits of a flat facet must not bias a fitted radius.
    keep = []
    for i, point in enumerate(points):
        if not closed and i in (0, len(points)-1):
            keep.append(i)
            continue
        a, b = point-points[i-1], points[(i+1) % len(points)]-point
        cross = abs(a[0]*b[1]-a[1]*b[0])
        if cross > epsilon*max(np.linalg.norm(a)+np.linalg.norm(b), epsilon) or np.dot(a, b) <= 0:
            keep.append(i)

    def add(kind, start, end, faces, error, **extra):
        output.append({"segment_id": f"{prefix}:{len(output)}", "kind": kind,
                       "start_mm": np.asarray(start).tolist(), "end_mm": np.asarray(end).tolist(),
                       "start_cad_mm": frame.to_cad(start), "end_cad_mm": frame.to_cad(end),
                       "source_face_ids": sorted(faces), "max_error_mm": float(error), **extra})

    if closed and len(keep) >= 6:
        center, radius, converged = _circle(points[keep], tolerance)
        # Exact minimum radius along every chord bounds arc-to-polyline deviation too.
        delta = points-center
        directions = np.roll(points, -1, axis=0)-points
        t = np.clip(-np.einsum("ij,ij->i", delta, directions)/np.sum(directions**2, axis=1), 0, 1)
        chord_min = np.linalg.norm(delta+t[:, None]*directions, axis=1)
        error = max(np.abs(np.linalg.norm(delta, axis=1)-radius).max(), np.abs(chord_min-radius).max())
        angles = np.unwrap(np.arctan2(delta[:, 1], delta[:, 0]))
        sense = 1 if _area(points) > 0 else -1
        changes = np.diff(angles)*sense
        if converged and error <= tolerance/4 and np.all(changes > -1e-9):
            start_angle = float(np.arctan2(delta[0, 1], delta[0, 0]))
            start = center+radius*np.array([np.cos(start_angle), np.sin(start_angle)])
            opposite = 2*center-start
            faces = set().union(*sources)
            add("arc", start, opposite, faces, error, center_mm=center.tolist(), radius_mm=radius,
                sweep_radians=sense*np.pi, start_angle_radians=start_angle)
            add("arc", opposite, start, faces, error, center_mm=center.tolist(), radius_mm=radius,
                sweep_radians=sense*np.pi, start_angle_radians=start_angle+sense*np.pi)
            return output
    # Collapse only numerically collinear segments, never a tolerance-sized open gap.
    pairs = list(zip(keep, keep[1:]))
    if closed and keep:
        pairs.append((keep[-1], keep[0]))
    for a, b in pairs:
        indices = list(range(a, b)) if b > a else list(range(a, len(points)))+list(range(0, b))
        faces = set().union(*(sources[i] for i in indices))
        segment = points[b]-points[a]
        chain = points[indices]
        deviation = np.abs(segment[0]*(chain[:, 1]-points[a, 1])-segment[1]*(chain[:, 0]-points[a, 0]))
        error = float(deviation.max()/np.linalg.norm(segment)) if len(chain) else 0.
        add("line", points[a], points[b], faces, error)
    return output


def section_mesh(mesh, frame, *, tolerance_mm) -> SectionResult:
    tolerance = validate_tolerance(tolerance_mm)
    epsilon = max(float(np.ptp(mesh.vertices, axis=0).max())*1e-10, 1e-9)
    ordinary, coplanar = _intersections(mesh, frame, epsilon)
    points, edges, adjacency = _graph(ordinary, coplanar, frame, epsilon)
    paths = _chains(edges, adjacency)
    closed_polygons = [points[path[:-1]] for path in paths if path[0] == path[-1]]
    result, reasons = [], []
    if _has_crossing(points, edges, epsilon):
        reasons.append("self_intersection")
    if any(len(neighbours) > 2 for neighbours in adjacency.values()):
        reasons.append("branching_intersection")
    for index, path in enumerate(paths):
        closed = path[0] == path[-1]
        xy = points[path[:-1] if closed else path]
        role = "open"
        if closed:
            depth = sum(_inside(xy[0], p) for p in closed_polygons
                        if not np.array_equal(p, xy))
            role = "hole" if depth % 2 else "outer"
            if (_area(xy) > 0) != (role == "outer"):
                path = path[::-1]
                xy = points[path[:-1]]
        sources = [edges[tuple(sorted((a, b)))] for a, b in zip(path, path[1:])]
        prefix = hashlib.sha256(f"{mesh.sha256}:{frame.frame_id}:{index}".encode()).hexdigest()[:24]
        segments = _segments(xy, sources, closed, frame, tolerance, epsilon, prefix)
        if not closed:
            reasons.append("open_chain")
        if closed and abs(_area(xy)) <= epsilon**2:
            reasons.append("degenerate_chain")
        if any(s["max_error_mm"] > tolerance/4 for s in segments):
            reasons.append("excessive_approximation_error")
        result.append({"chain_id": prefix, "closed": closed, "role": role,
                       "points_mm": xy.tolist(), "signed_area_mm2": _area(xy) if closed else None,
                       "source_face_ids": sorted(set().union(*sources)), "segments": segments})
    if not paths:
        reasons.append("no_intersection")
    return SectionResult(mesh.sha256, frame.frame_id, asdict(frame), tolerance, epsilon,
                         result, not reasons, tuple(sorted(set(reasons))))
