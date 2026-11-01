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
                comparison["method"] not in {"area_weighted_triangle_surface_v1", "stratified_triangle_surface_v2"}
                or comparison["reasons"]):
            return "unknown"
        tolerance, chord = reference["tolerance_mm"], snapshot["chord_tolerance_mm"]
        if (type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance <= 0 or
                type(chord) not in (int, float) or not math.isfinite(chord) or not 0 <= chord <= tolerance/4):
            return "unknown"
        statistics = [comparison["reference_to_cad"], comparison["cad_to_reference"], *comparison["regions"]]
        required = set(reference.get("required_region_ids", []))
        if comparison["method"] == "stratified_triangle_surface_v2":
            for direction in ("reference_to_cad", "cad_to_reference"):
                coverage = comparison["coverage"][direction]
                counts = [coverage[k] for k in ("total_faces", "checked_faces", "partitions")]
                if (any(type(n) is not int or n <= 0 for n in counts) or
                        coverage["complete"] is not True or counts[0] != counts[1] or counts[2] > 512):
                    return "unknown"
                records = [r for r in comparison["regions"] if r.get("direction") == direction]
                if len(records) != counts[2] or sum(r["samples"] for r in records) != counts[0]:
                    return "unknown"
            checked = comparison["required_regions"]
            if (len({r["region_id"] for r in checked}) != len(checked) or
                    {r["region_id"] for r in checked} != required or
                    any(r["complete"] is not True or r["samples"] != 256 for r in checked)):
                return "unknown"
            statistics.extend(checked)
        elif required:
            return "unknown"
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
