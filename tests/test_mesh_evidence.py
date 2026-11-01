"""Mesh evidence is independently bound to source, frame and current CAD revision."""

from dataclasses import asdict

import pytest

from tests.test_mesh_compare import box, options


def comparison():
    from cadnext.mesh.compare import compare_surfaces

    return asdict(compare_surfaces(box(), box(), options=options()))


def reference():
    return {"sha256": box().sha256, "frame_id": "frame", "tolerance_mm": .1}


def snapshot():
    return {"sha256": box().sha256, "frame_id": "frame", "document_identity": "doc",
            "revision": 7, "run_id": "run", "precision_known": True, "chord_tolerance_mm": .01}


def test_current_complete_mesh_evidence_is_separate_pass():
    from cadnext.mesh.evidence import validate_mesh_evidence

    assert validate_mesh_evidence(comparison(), reference(), snapshot()) == "pass"


@pytest.mark.parametrize("field,value", [
    ("revision", 8), ("document_identity", "other"), ("frame_id", "other"),
    ("sha256", "0"*64), ("run_id", "other"), ("precision_known", False),
])
def test_stale_or_untrusted_snapshot_cannot_reuse_pass(field, value):
    from cadnext.mesh.evidence import validate_mesh_evidence

    assert validate_mesh_evidence(comparison(), reference(), {**snapshot(), field: value}) == "unknown"


def test_changed_reference_or_tolerance_invalidates_comparison():
    from cadnext.mesh.evidence import validate_mesh_evidence

    for field, value in (("sha256", "0"*64), ("tolerance_mm", .2), ("frame_id", "other")):
        assert validate_mesh_evidence(comparison(), {**reference(), field: value}, snapshot()) == "unknown"


def test_status_label_does_not_override_a_failed_required_region():
    from cadnext.mesh.evidence import validate_mesh_evidence

    record = comparison()
    record["violating_regions"] = ["bore"]
    record["status"] = "pass"
    assert validate_mesh_evidence(record, reference(), snapshot()) == "fail"
