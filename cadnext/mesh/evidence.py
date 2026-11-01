"""Independent mesh evidence gate; never substitute for native CAD dimensions."""

import math


def validate_mesh_evidence(comparison, reference, snapshot):
    try:
        if (comparison["reference_sha256"] != reference["sha256"] or
                comparison["cad_sha256"] != snapshot["sha256"] or
                comparison["frame_id"] != reference["frame_id"] or
                snapshot["frame_id"] != reference["frame_id"] or
                comparison["tolerance_mm"] != reference["tolerance_mm"] or
                any(comparison[key] != snapshot[key] for key in
                    ("document_identity", "revision", "run_id")) or
                snapshot["precision_known"] is not True or
                comparison["chord_tolerance_mm"] != snapshot["chord_tolerance_mm"] or
                comparison["method"] != "area_weighted_triangle_surface_v1" or comparison["reasons"]):
            return "unknown"
        tolerance, chord = reference["tolerance_mm"], snapshot["chord_tolerance_mm"]
        if (type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance <= 0 or
                type(chord) not in (int, float) or not math.isfinite(chord) or not 0 <= chord <= tolerance/4):
            return "unknown"
        statistics = [comparison["reference_to_cad"], comparison["cad_to_reference"], *comparison["regions"]]
        if not comparison["regions"]:
            return "unknown"
        for item in statistics:
            if type(item["samples"]) is not int or item["samples"] <= 0:
                return "unknown"
            if any(type(item[key]) not in (int, float) or not math.isfinite(item[key]) or item[key] < 0
                   for key in ("rms_mm", "p95_mm", "sampled_max_mm", "fraction_within_tolerance")):
                return "unknown"
        if (comparison["violating_regions"] or
                any(item["sampled_max_mm"] > tolerance-chord for item in statistics)):
            return "fail"
        return "pass"
    except (KeyError, TypeError, ValueError):
        return "unknown"
