"""Triangle-plane intersection and conservative profiles in an explicit frame."""

import hashlib
from collections import defaultdict
from dataclasses import asdict

import numpy as np
from rtree.index import Index
from scipy.optimize import least_squares

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
                coplanar.append(((points[a], ("v", int(ids[a]))),
                                 (points[b], ("v", int(ids[b]))), int(face_id)))
            continue
        hits = [(points[i], ("v", int(ids[i]))) for i in range(3) if d[i] == 0]
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if d[a]*d[b] < 0:
                key = ("e", *sorted((int(ids[a]), int(ids[b]))))
                # Evaluate shared edges in the same direction for identical coordinates.
                low, high = key[1:]
                point = vertices[low]+(vertices[high]-vertices[low])*(distances[low]/(distances[low]-distances[high]))
                hits.append((point, key))
        if len(hits) == 2 and hits[0][1] != hits[1][1]:
            ordinary.append((*hits, int(face_id)))
    return ordinary, coplanar


def _graph(ordinary, coplanar, frame, epsilon):
    segments = ordinary+coplanar
    if not segments:
        return np.empty((0, 2)), {}, {}
    identities, coordinates, labels = {}, [], []
    for a, b, _ in segments:
        for xyz, key in (a, b):
            if key not in identities:
                identities[key] = len(coordinates)
                coordinates.append((xyz-np.asarray(frame.origin_mm)) @ np.asarray([frame.x_axis, frame.y_axis]).T)
            labels.append(identities[key])
    points = np.asarray(coordinates)
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


def _profile_span(points, tolerance):
    start, end = points[0], points[-1]
    direction = end-start
    length = np.linalg.norm(direction)
    if length < 1e-14:
        return None, len(points)//2
    projection = np.clip((points-start) @ direction/length**2, 0, 1)
    errors = np.linalg.norm(points-(start+projection[:, None]*direction), axis=1)
    split = int(np.argmax(errors))
    if errors.max() <= tolerance/4:
        return {"kind": "line", "max_error_mm": float(errors.max())}, split
    if len(points) < 3:
        return None, split
    midpoint = (start+end)/2
    perpendicular = np.array([-direction[1], direction[0]])/length
    center, _, _ = _circle(points, tolerance)
    initial = float(np.dot(center-midpoint, perpendicular))

    def residual(h):
        c = midpoint+h[0]*perpendicular
        radius = np.linalg.norm(start-c)
        return np.linalg.norm(points-c, axis=1)-radius

    solution = least_squares(residual, [initial], max_nfev=80)
    center = midpoint+solution.x[0]*perpendicular
    radius = float(np.linalg.norm(start-center))
    delta = points-center
    angles = np.unwrap(np.arctan2(delta[:, 1], delta[:, 0]))
    sweep = float(angles[-1]-angles[0])
    changes = np.diff(angles)*np.sign(sweep)
    if not solution.success or abs(sweep) > 2*np.pi-1e-8 or not np.all(changes >= -1e-10):
        return None, split
    directions = np.diff(points, axis=0)
    t = np.clip(-np.einsum("ij,ij->i", delta[:-1], directions)/np.sum(directions**2, axis=1), 0, 1)
    chord_minimum = np.linalg.norm(delta[:-1]+t[:, None]*directions, axis=1)
    radial_errors = np.abs(np.linalg.norm(delta, axis=1)-radius)
    chord_errors = np.abs(chord_minimum-radius)
    error = max(float(radial_errors.max()), float(chord_errors.max()))
    if error > tolerance/4:
        if chord_errors.max() > radial_errors.max():
            split = int(np.argmax(chord_errors))+1
        else:
            split = int(np.argmax(radial_errors))
        return None, split
    # Polar projection is continuous and monotone on each chord. Its radial bound
    # therefore bounds both directed distances over the entire arc and polyline.
    return {"kind": "arc", "max_error_mm": error, "center_mm": center.tolist(),
            "radius_mm": radius, "sweep_radians": sweep,
            "start_angle_radians": float(angles[0])}, split


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
    if closed:
        # Start at the longest reduced edge to keep straight/curved junctions stable.
        lengths = [np.linalg.norm(points[keep[(i+1) % len(keep)]]-points[k]) for i, k in enumerate(keep)]
        start = keep[int(np.argmax(lengths))] if keep else 0
        points = np.concatenate((points[start:], points[:start], points[start:start+1]))
        sources = sources[start:]+sources[:start]
    pieces, pending = [], [(0, len(points)-1)]
    while pending:
        a, b = pending.pop()
        fitted, split = _profile_span(points[a:b+1], tolerance)
        if fitted is not None:
            pieces.append((a, b, fitted))
        else:
            split = a+max(1, min(b-a-1, split))
            pending.extend([(split, b), (a, split)])
    pieces.sort(key=lambda p: p[0])
    i = 0
    while i+1 < len(pieces):
        a, b, _ = pieces[i]
        _, c, _ = pieces[i+1]
        merged, _ = _profile_span(points[a:c+1], tolerance)
        if merged is not None:
            pieces[i:i+2] = [(a, c, merged)]
            i = max(0, i-1)
        else:
            i += 1
    for a, b, fitted in pieces:
        faces = set().union(*sources[a:b])
        details = dict(fitted)
        kind, error = details.pop("kind"), details.pop("max_error_mm")
        add(kind, points[a], points[b], faces, error, **details)
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
        context = getattr(mesh, "context_id", "")
        prefix = hashlib.sha256(f"sections-2:{mesh.sha256}:{context}:{frame.frame_id}:{tolerance}:{index}".encode()).hexdigest()[:24]
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
