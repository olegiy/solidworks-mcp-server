"""Characterize the numerical operations required by mesh reconstruction."""

import importlib
import importlib.util
import io

import pytest


def packages():
    names = ("numpy", "scipy", "trimesh", "rtree")
    missing = [name for name in names if importlib.util.find_spec(name) is None]
    assert not missing, f"Mesh dependencies are missing: {', '.join(missing)}"
    return tuple(importlib.import_module(name) for name in names)


def test_mesh_dependencies_import_on_target_python():
    np, scipy, trimesh, rtree = packages()
    assert np.isfinite(scipy.linalg.det(np.eye(3)))
    assert trimesh.Trimesh is not None
    assert rtree.index.Index is not None
    assert importlib.util.find_spec("networkx") is not None


@pytest.mark.parametrize("format", ["stl_ascii", "stl", "obj"])
def test_load_mesh_bytes_without_filesystem_or_materials(format):
    np, _, trimesh, _ = packages()
    from tests.mesh_fixtures import make_plate_with_hole

    fixture = make_plate_with_hole()
    original = trimesh.Trimesh(vertices=fixture.vertices, faces=fixture.faces, process=False)
    encoded = original.export(file_type=format)
    if isinstance(encoded, str):
        encoded = encoded.encode("utf-8")
    loaded = trimesh.load_mesh(
        io.BytesIO(encoded), file_type="stl" if format.startswith("stl") else format,
        process=False, skip_materials=True, resolver=None, allow_remote=False,
    )
    assert len(loaded.faces) == len(fixture.faces)
    assert np.allclose(loaded.bounds, [[-20, -15, 0], [20, 15, 5]], atol=1e-5)


def test_plate_section_preserves_outer_boundary_and_hole():
    np, _, trimesh, _ = packages()
    from tests.mesh_fixtures import make_plate_with_hole

    fixture = make_plate_with_hole()
    mesh = trimesh.Trimesh(vertices=fixture.vertices, faces=fixture.faces, process=False)
    section = mesh.section(plane_origin=[0, 0, 2.5], plane_normal=[0, 0, 1])
    assert section is not None
    assert len(section.discrete) == 2
    radii = sorted(float(np.linalg.norm(loop[:, :2], axis=1).max()) for loop in section.discrete)
    assert radii[0] == pytest.approx(5, abs=1e-6)
    assert radii[1] == pytest.approx(25, abs=1e-6)


def test_distance_is_to_triangle_surface_instead_of_nearest_vertex():
    np, _, trimesh, _ = packages()
    mesh = trimesh.Trimesh(
        vertices=[[0, 0, 0], [100, 0, 0], [0, 100, 0]], faces=[[0, 1, 2]], process=False,
    )
    closest, distance, triangle = mesh.nearest.on_surface([[25, 25, 3]])
    assert np.allclose(closest, [[25, 25, 0]])
    assert distance.tolist() == pytest.approx([3])
    assert triangle.tolist() == [0]


def test_stepped_bushing_fixture_is_closed_and_has_expected_dimensions():
    np, _, trimesh, _ = packages()
    from tests.mesh_fixtures import make_stepped_bushing

    fixture = make_stepped_bushing()
    mesh = trimesh.Trimesh(vertices=fixture.vertices, faces=fixture.faces, process=False)
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert mesh.volume > 0
    assert np.allclose(mesh.bounds, [[-15, -15, 0], [15, 15, 40]], atol=1e-6)
    section = mesh.section(plane_origin=[0, 0, 30], plane_normal=[0, 0, 1])
    assert len(section.discrete) == 2
    radii = sorted(float(np.linalg.norm(loop[:, :2], axis=1).max()) for loop in section.discrete)
    assert radii == pytest.approx([5, 10], abs=1e-6)


def test_feasibility_command_reports_actual_surface_error_and_memory():
    import json
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "scripts.mesh_feasibility", "--sizes", "100", "--queries", "10"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data["benchmarks"]) == 1
    row = data["benchmarks"][0]
    assert 90 <= row["triangles"] <= 100
    assert row["queries"] == 10
    assert row["max_distance_error_mm"] < 1e-8
    assert row["peak_working_set_bytes"] > 0
    assert row["duration_seconds"] > 0
