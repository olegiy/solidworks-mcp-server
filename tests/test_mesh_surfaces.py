"""Engineering primitives must describe facets as well as their vertices."""

import numpy as np
import pytest

from tests.test_mesh_analysis import cylinder_mesh, mesh_data


def revolution(kind, n=128, m=32):
    u = np.linspace(0, 2*np.pi, n, endpoint=False)
    if kind == "sphere":
        v = np.linspace(-1.3, 1.3, m)
        points = [(7*np.cos(t)*np.cos(a), 7*np.cos(t)*np.sin(a), 7*np.sin(t)) for t in v for a in u]
    elif kind == "torus":
        v = np.linspace(0, 2*np.pi, m, endpoint=False)
        points = [((10+2*np.cos(t))*np.cos(a), (10+2*np.cos(t))*np.sin(a), 2*np.sin(t))
                  for t in v for a in u]
    else:
        v = np.linspace(0, 10, m)
        points = [((4+t*.3)*np.cos(a), (4+t*.3)*np.sin(a), t) for t in v for a in u]
    faces = []
    for j in range(m if kind == "torus" else m-1):
        for i in range(n):
            a, b = j*n+i, j*n+(i+1) % n
            c, d = ((j+1) % m)*n+i, ((j+1) % m)*n+(i+1) % n
            faces.extend([(a, b, d), (a, d, c)])
    return mesh_data(points, faces)


@pytest.mark.parametrize("kind", ["cylinder", "cone", "sphere", "torus"])
def test_geometric_models_fit_complete_facets(kind):
    from cadnext.mesh.surfaces import measure_surface

    mesh = cylinder_mesh() if kind == "cylinder" else revolution(kind)
    result = measure_surface(mesh, np.arange(len(mesh.faces)), kind, .1)
    assert result.usable, result.degeneracies
    assert result.coverage["facet_bound_mm"] <= .025
    if kind in {"cylinder", "sphere"}:
        assert result.parameters["radius_mm"] == pytest.approx(7, abs=.025)
    if kind == "torus":
        assert result.parameters["major_radius_mm"] == pytest.approx(10, abs=.025)
        assert result.parameters["minor_radius_mm"] == pytest.approx(2, abs=.025)


def test_catalog_joins_curved_strips_and_ids_bind_tolerance():
    from cadnext.mesh.surfaces import surface_catalog

    mesh = cylinder_mesh()
    first = surface_catalog(mesh, .1)
    assert first["complete"]
    assert first["unrecognized_triangles"] == 0
    assert len(first["surfaces"]) == 1
    surface = first["surfaces"][0]
    assert surface["kind"] == "cylinder" and surface["usable"]
    assert surface == surface_catalog(mesh, .1)["surfaces"][0]
    assert surface["surface_id"] != surface_catalog(mesh, .2)["surfaces"][0]["surface_id"]


def test_seed_selects_connected_surface_and_reports_miss():
    from cadnext.mesh.analysis import fit_region
    from cadnext.mesh.contracts import MeshError

    mesh = cylinder_mesh()
    result = fit_region(mesh, {"seed_mm": [7, 0, 12]}, "cylinder", tolerance_mm=.1)
    assert result.usable
    with pytest.raises(MeshError, match="ближай") as caught:
        fit_region(mesh, {"seed_mm": [100, 0, 12]}, "cylinder", tolerance_mm=.1)
    assert caught.value.code == "seed_distance"


def test_coarse_facets_and_small_coverage_are_unusable():
    from cadnext.mesh.surfaces import measure_surface

    coarse = revolution("sphere", n=12, m=8)
    result = measure_surface(coarse, np.arange(len(coarse.faces)), "sphere", .1)
    assert not result.usable
    assert "facet_error" in result.degeneracies
    small = cylinder_mesh(angle=.1)
    result = measure_surface(small, np.arange(len(small.faces)), "cylinder", .1)
    assert not result.usable
    assert "insufficient_coverage" in result.degeneracies


@pytest.mark.parametrize("kind", ["cone", "sphere", "torus"])
def test_rotated_noisy_shared_vertices_recover_parameters(kind):
    from scipy.spatial.transform import Rotation

    from cadnext.mesh.surfaces import measure_surface

    mesh = revolution(kind)
    rotation = Rotation.from_rotvec([.3, -.6, .2]).as_matrix()
    rng = np.random.default_rng(429)
    points = mesh.vertices @ rotation.T+[20, -13, 7]
    points += rng.normal(0, .1/20, points.shape)
    outliers = rng.choice(len(points), len(points)//100, replace=False)
    directions = rng.normal(size=(len(outliers), 3))
    points[outliers] += .1*directions/np.linalg.norm(directions, axis=1)[:, None]
    result = measure_surface(mesh_data(points, mesh.faces), np.arange(len(mesh.faces)), kind, .1)
    assert result.usable, result.degeneracies
    assert result.coverage["excluded_points"] <= len(points)*.02
    if kind == "sphere":
        assert result.parameters["radius_mm"] == pytest.approx(7, abs=.025)
        assert np.linalg.norm(np.asarray(result.parameters["center_mm"])-[20, -13, 7]) < .025
    else:
        assert abs(np.dot(result.parameters["axis"], rotation[:, 2])) > np.cos(np.deg2rad(.5))


async def test_surface_catalog_worker_projection_and_selection(tmp_path):
    import asyncio

    from tests.test_mesh_tools import ready_tools

    tools, asset = await ready_tools(tmp_path)
    binding = tools.bind("s", {"asset_id": asset.asset_id, "tolerance_mm": .1}, run_id="run")
    try:
        page = await tools.dispatch(binding, "mesh_inspect", {"catalog": "surfaces"}, stop=asyncio.Event())
        assert page["surfaces"] and page["summary"]["complete"]
        assert "source_face_ids" not in str(page)
        sid = next(s["surface_id"] for s in page["surfaces"] if s["kind"] == "plane")
        measured = await tools.dispatch(binding, "mesh_fit", {"region": {"surface_id": sid}, "kind": "plane"}, stop=asyncio.Event())
        assert measured["fit"]["usable"]
    finally:
        await tools.aclose()


def test_growing_competing_models_separates_smooth_cylinder_cone_join():
    from cadnext.mesh.surfaces import surface_catalog

    u = np.linspace(0, 2*np.pi, 96, endpoint=False)
    z = np.linspace(0, 20, 21)
    points = [((7+max(h-10, 0)*.2)*np.cos(a), (7+max(h-10, 0)*.2)*np.sin(a), h)
              for h in z for a in u]
    faces = []
    for j in range(20):
        for i in range(96):
            a, b = j*96+i, j*96+(i+1) % 96
            faces.extend([(a, b, b+96), (a, b+96, a+96)])
    catalog = surface_catalog(mesh_data(points, faces), .1)
    assert {s["kind"] for s in catalog["surfaces"] if s["usable"]} >= {"cylinder", "cone"}
    assert catalog["unrecognized_triangles"] == 0
