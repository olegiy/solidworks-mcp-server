"""Topological identity and bounded mixed line/arc profiles."""

import numpy as np

from tests.test_mesh_analysis import mesh_data
from tests.test_mesh_sections import frame


def extruded_wall(points, *, close=True):
    n = len(points)
    vertices = [(x, y, z) for z in (-1, 1) for x, y in points]
    faces = []
    for i in range(n if close else n-1):
        j = (i+1) % n
        faces.extend([(i, j, j+n), (i, j+n, i+n)])
    return mesh_data(vertices, faces)


def test_topologically_separate_edges_never_weld_even_below_epsilon():
    from cadnext.mesh.sections import section_mesh

    mesh = extruded_wall([(0, 0), (10, 0), (10, 5), (0, 5), (0, 1e-11)], close=False)
    result = section_mesh(mesh, frame(0), tolerance_mm=1)
    assert not result.usable_for_sketch
    assert not result.chains[0]["closed"]


def test_slot_has_two_lines_and_two_semicircles_with_common_ends():
    from cadnext.mesh.sections import section_mesh

    points = [(10+3*np.cos(a), 3*np.sin(a)) for a in np.linspace(-np.pi/2, np.pi/2, 65)]
    points += [(-10+3*np.cos(a), 3*np.sin(a)) for a in np.linspace(np.pi/2, 3*np.pi/2, 65)]
    result = section_mesh(extruded_wall(points), frame(0), tolerance_mm=.1)
    assert result.usable_for_sketch
    segments = result.chains[0]["segments"]
    assert len(segments) == 4
    assert [s["kind"] for s in segments].count("arc") == 2
    for i, segment in enumerate(segments):
        assert segment["end_mm"] == segments[(i+1) % len(segments)]["start_mm"]
        assert segment["max_error_mm"] <= .025
        assert segment["source_face_ids"]


def test_profile_ids_bind_tolerance_and_reference_frame():
    from cadnext.mesh.prepared import prepare
    from cadnext.mesh.sections import section_mesh

    mesh = extruded_wall([(0, 0), (10, 0), (10, 5), (0, 5)])
    first = section_mesh(prepare(mesh, "a"), frame(0), tolerance_mm=.1)
    second = section_mesh(prepare(mesh, "a"), frame(0), tolerance_mm=.2)
    third = section_mesh(prepare(mesh, "b"), frame(0), tolerance_mm=.1)
    assert len({r.chains[0]["chain_id"] for r in (first, second, third)}) == 3


def test_branch_at_original_vertex_is_not_a_sketch():
    from cadnext.mesh.sections import section_mesh

    vertices = [[0, 0, 0], [2, 0, -1], [2, 0, 1],
                [-1, 2, -1], [-1, 2, 1], [-1, -2, -1], [-1, -2, 1]]
    mesh = mesh_data(vertices, [[0, 1, 2], [0, 3, 4], [0, 5, 6]])
    result = section_mesh(mesh, frame(0), tolerance_mm=.1)
    assert not result.usable_for_sketch
    assert "branching_intersection" in result.reasons


def test_zero_length_topological_edge_terminates_profile_fitting():
    from cadnext.mesh.sections import _profile_span

    result, _ = _profile_span(np.zeros((2, 2)), .1)
    assert result is not None


def test_narrow_closed_circular_cap_is_not_replaced_with_full_circle():
    from cadnext.mesh.sections import section_mesh

    angles = np.deg2rad(np.linspace(-2.5, 2.5, 9))
    vertices = [[7*np.cos(a), 7*np.sin(a), 0] for a in angles]+[[6.996, 0, 1], [6.996, 0, -1]]
    faces = [face for i in range(9) for face in [(i, (i+1) % 9, 9), ((i+1) % 9, i, 10)]]
    result = section_mesh(mesh_data(vertices, faces), frame(0), tolerance_mm=.1)
    assert result.usable_for_sketch
    arcs = [s for c in result.chains for s in c["segments"] if s["kind"] == "arc"]
    assert sum(abs(s["sweep_radians"]) for s in arcs) < .2
