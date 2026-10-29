"""Pure goal contract and evidence evaluation without CAD or model access."""

import math

import pytest

from cadnext.evaluation import (
    EvaluationOptions,
    EvidenceRecord,
    GoalContract,
    Requirement,
    components_evidence,
    dimension_evidence,
    evaluate_goal,
    mass_properties_evidence,
    requested_file_evidence,
    sheet_metal_evidence,
    tree_evidence,
    update_progress,
)


def contract(*requirements, kind="cad", ambiguities=()):
    return GoalContract(kind=kind, requirements=list(requirements), ambiguities=list(ambiguities))


def numeric(id="d1", expected=50.0, *, required=True, weight=1, tolerance=0.001, unit="mm"):
    return Requirement(id=id, description=f"Размер {id}", required=required, weight=weight,
                       method="cad", expected=expected, tolerance=tolerance, unit=unit)


def evidence(contract_, requirement_id, status="pass", *, source="cad", revision=1,
             actual=50.0, document_identity="part-1", evidence_id=None):
    return EvidenceRecord(
        id=evidence_id or f"e-{requirement_id}-{status}-{source}",
        requirement_id=requirement_id,
        contract_digest=contract_.contract_digest,
        revision=revision,
        document_identity=document_identity,
        source=source,
        status=status,
        actual=actual,
    )


def test_similarity_requires_all_weight_at_100():
    goal = contract(numeric("d1", 50.0, required=False))
    result = evaluate_goal(goal, [evidence(goal, "d1", "fail", actual=49.0)],
                           revision=1, target_similarity=100)
    assert result.score == 0
    assert result.outcome == "not_achieved"


def test_required_dimension_blocks_lower_threshold():
    goal = contract(numeric("d1", 50.0, required=True),
                    numeric("d2", 20.0, required=False, weight=3))
    result = evaluate_goal(goal, [
        evidence(goal, "d1", "fail", actual=49.0),
        evidence(goal, "d2", "pass", actual=20.0),
    ], revision=1, target_similarity=50)
    assert result.score == 75
    assert result.outcome == "not_achieved"


def test_unknown_never_passes():
    goal = contract(numeric("d1", 50.0))
    result = evaluate_goal(goal, [evidence(goal, "d1", "unknown", actual=None)],
                           revision=1, target_similarity=100)
    assert result.score == 0
    assert result.outcome == "unknown"
    assert result.per_requirement[0]["status"] == "unknown"


def test_stale_revision_or_contract_cannot_pass():
    goal = contract(numeric("d1", 50.0))
    stale_revision = evaluate_goal(goal, [evidence(goal, "d1", revision=2)],
                                   revision=1, target_similarity=100)
    assert stale_revision.outcome == "unknown"
    other = contract(numeric("d1", 50.0, weight=2))
    stale_contract = evaluate_goal(goal, [evidence(other, "d1", revision=1)],
                                   revision=1, target_similarity=100)
    assert stale_contract.outcome == "unknown"


def test_visual_pass_is_labelled_not_geometric_proof():
    goal = contract(numeric("d1", 50.0))
    result = evaluate_goal(goal, [evidence(goal, "d1", "pass", source="vision", actual=50.0)],
                           revision=1, target_similarity=100)
    assert result.outcome == "unknown"
    assert result.authority == "cad"
    assert "vision" in result.per_requirement[0]["reason"].lower()

    visual = contract(Requirement(id="v1", description="Скругление", method="vision"))
    accepted = evaluate_goal(visual, [evidence(visual, "v1", "pass", source="vision", actual=None)],
                             revision=1, target_similarity=100)
    assert accepted.outcome == "achieved"
    assert accepted.authority == "vision"
    assert "геометрическ" in accepted.per_requirement[0]["reason"].lower()
    assert "model" in accepted.per_requirement[0]["reason"].lower()


def test_progress_uses_best_score_not_oscillation():
    goal = contract(numeric("d1", 50.0, weight=1), numeric("d2", 20.0, weight=1))
    high = evaluate_goal(goal, [evidence(goal, "d1", "pass")], revision=1, target_similarity=100)
    low = evaluate_goal(goal, [], revision=2, target_similarity=100)
    assert update_progress(high, low, 0, best_weight=1) == 1
    same = evaluate_goal(goal, [evidence(goal, "d1", "pass", revision=2)], revision=2,
                         target_similarity=100)
    assert update_progress(high, same, 0) == 1
    better = evaluate_goal(goal, [evidence(goal, "d1", "pass", revision=2),
                                  evidence(goal, "d2", "pass", revision=2, actual=20.0)],
                           revision=2, target_similarity=100)
    assert update_progress(low, better, 2, best_weight=1) == 0
    assert update_progress(None, low, 0) == 0


def test_update_progress_brief_three_argument_form():
    goal = contract(numeric("d1", 50.0, weight=1), numeric("d2", 20.0, weight=1))
    first = evaluate_goal(goal, [evidence(goal, "d1", "pass")], revision=1,
                          target_similarity=100)
    assert update_progress(None, first, 0) == 0

    equal = evaluate_goal(goal, [evidence(goal, "d1", "pass", revision=2)], revision=2,
                          target_similarity=100)
    assert update_progress(first, equal, 0) == 1

    lower = evaluate_goal(goal, [], revision=2, target_similarity=100)
    assert update_progress(first, lower, 1) == 2

    higher = evaluate_goal(goal, [evidence(goal, "d1", "pass", revision=2),
                                  evidence(goal, "d2", "pass", revision=2, actual=20.0)],
                           revision=2, target_similarity=100)
    assert update_progress(lower, higher, 2) == 0


def test_update_progress_best_weight_prevents_oscillation():
    goal = contract(numeric("d1", 50.0, weight=1), numeric("d2", 20.0, weight=1))
    best = evaluate_goal(goal, [evidence(goal, "d1", "pass"), evidence(goal, "d2", "pass")],
                         revision=1, target_similarity=100)
    low = evaluate_goal(goal, [], revision=2, target_similarity=100)
    middle = evaluate_goal(goal, [evidence(goal, "d1", "pass", revision=2)], revision=2,
                           target_similarity=100)
    assert update_progress(low, middle, 1, best_weight=best.passed_weight) == 2
    assert update_progress(middle, best, 2, best_weight=best.passed_weight) == 3
    # Without the historical maximum only the immediate previous result is known.
    assert update_progress(best, middle, 1) == 2


def test_duplicate_requirement_ids_rejected():
    with pytest.raises(ValueError):
        contract(numeric("d1"), numeric("d1"))


def test_requirement_count_and_weight_bounds():
    with pytest.raises(ValueError):
        contract(*[numeric(f"d{index}") for index in range(65)])
    with pytest.raises(ValueError):
        Requirement(id="d1", description="x", weight=0, method="cad", expected=1, unit="mm")
    with pytest.raises(ValueError):
        Requirement(id="d1", description="x", weight=True, method="cad", expected=1, unit="mm")


def test_malformed_numbers_rejected():
    for value in (math.nan, math.inf, -math.inf, -1.0, 0.0, 100.1):
        with pytest.raises(ValueError):
            EvaluationOptions(target_similarity=value)
    goal = contract(numeric("d1", 50.0))
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            EvidenceRecord(id="e", requirement_id="d1", contract_digest=goal.contract_digest,
                           revision=1, document_identity="p", source="cad", status="pass",
                           actual=value)
        with pytest.raises(ValueError):
            Requirement(id="d1", description="Размер", method="cad", expected=value, unit="mm")


def test_empty_cad_contract_rejected_but_informational_allowed():
    with pytest.raises(ValueError):
        GoalContract(kind="cad", requirements=[])
    informational = GoalContract(kind="informational", requirements=[])
    assert evaluate_goal(informational, [], revision=0, target_similarity=100).score is None


def test_weight_cannot_change_after_fixation():
    first = contract(numeric("d1", 50.0, weight=1))
    changed = contract(numeric("d1", 50.0, weight=2))
    assert first.contract_digest != changed.contract_digest
    result = evaluate_goal(first, [evidence(changed, "d1", revision=1)],
                           revision=1, target_similarity=100)
    assert result.outcome == "unknown"


def test_document_change_cannot_pass():
    goal = contract(numeric("d1", 50.0))
    record = evidence(goal, "d1", document_identity="part-2")
    result = evaluate_goal(goal, [record], revision=1, target_similarity=100,
                           document_identity="part-1")
    assert result.outcome == "unknown"


def test_numeric_recomputed_from_actual_not_claimed_status():
    goal = contract(numeric("d1", 50.0, tolerance=0.01))
    result = evaluate_goal(goal, [evidence(goal, "d1", "pass", actual=49.0)],
                           revision=1, target_similarity=100)
    assert result.outcome == "not_achieved"


def test_dimension_evidence_requires_matching_binding_and_unit():
    goal = contract(numeric("d1", 50.0))
    requirement = goal.requirements[0]
    missing = dimension_evidence(requirement, {"name": "D2", "value": 50.0, "unit": "mm"},
                                 bound_name="D1", revision=1, document_identity="p")
    assert missing.status == "unknown"
    wrong_unit = dimension_evidence(requirement, {"name": "D1", "value": 50.0, "unit": "deg"},
                                    bound_name="D1", revision=1, document_identity="p")
    assert wrong_unit.status == "unknown"
    malformed_unit = dimension_evidence(
        requirement, {"name": "D1", "value": 50.0, "unit": []},
        bound_name="D1", revision=1, document_identity="p")
    assert malformed_unit.status == "unknown"
    ok = dimension_evidence(requirement, {"name": "D1", "value": 50.0, "unit": "mm"},
                            bound_name="D1", revision=1, document_identity="p")
    assert ok.status == "pass"


def test_dimension_tolerance_stricter_than_reading_is_unknown():
    goal = contract(numeric("d1", 50.0, tolerance=1e-9))
    result = dimension_evidence(goal.requirements[0],
                                {"name": "D1", "value": 50.0, "unit": "mm"},
                                bound_name="D1", revision=1, document_identity="p")
    assert result.status == "unknown"
    assert "точн" in result.reason.lower()


def test_dimension_default_tolerance_is_one_micro_unit():
    requirement = Requirement(id="d1", description="Размер", method="cad",
                              expected=50.0, unit="mm")
    within = dimension_evidence(requirement, {"name": "D1", "value": 50.000001, "unit": "mm"},
                                bound_name="D1", revision=1, document_identity="p")
    outside = dimension_evidence(requirement, {"name": "D1", "value": 50.00001, "unit": "mm"},
                                 bound_name="D1", revision=1, document_identity="p")
    assert within.status == "pass"
    assert outside.status == "fail"


def test_sheet_metal_default_tolerance_is_five_micro_units():
    requirement = Requirement(id="t", description="Толщина", method="cad",
                              expected=2.0, unit="mm")
    within = sheet_metal_evidence(requirement, {"thickness_mm": 2.00004},
                                  field="thickness_mm", revision=1, document_identity="p")
    outside = sheet_metal_evidence(requirement, {"thickness_mm": 2.00006},
                                   field="thickness_mm", revision=1, document_identity="p")
    assert within.status == "pass"
    assert outside.status == "fail"


def test_sheet_metal_evidence_uses_reading_precision():
    goal = contract(Requirement(id="t", description="Толщина", method="cad",
                                expected=2.0, tolerance=5e-5, unit="mm"))
    ok = sheet_metal_evidence(goal.requirements[0],
                              {"thickness_mm": 2.00004, "bend_radius_mm": 1.0},
                              field="thickness_mm", revision=1, document_identity="p")
    assert ok.status == "pass"
    too_strict = sheet_metal_evidence(
        Requirement(id="t", description="Толщина", method="cad", expected=2.0,
                    tolerance=1e-7, unit="mm"),
        {"thickness_mm": 2.0, "bend_radius_mm": 1.0},
        field="thickness_mm", revision=1, document_identity="p")
    assert too_strict.status == "unknown"

    loose = sheet_metal_evidence(
        Requirement(id="t", description="Толщина", method="cad", expected=2.0,
                    tolerance=0.01, unit="mm"),
        {"thickness_mm": 2.004, "bend_radius_mm": 1.0},
        field="thickness_mm", revision=1, document_identity="p")
    assert loose.status == "pass"


def test_duplicate_evidence_ids_rejected():
    goal = contract(numeric("d1", 50.0))
    record = evidence(goal, "d1", evidence_id="same")
    with pytest.raises(ValueError):
        evaluate_goal(goal, [record, record], revision=1, target_similarity=100)


def test_optional_failures_allowed_below_100():
    goal = contract(numeric("d1", 50.0), numeric("d2", 20.0, required=False))
    result = evaluate_goal(goal, [evidence(goal, "d1", "pass"),
                                  evidence(goal, "d2", "fail", actual=19.0)],
                           revision=1, target_similarity=50)
    assert result.score == 50
    assert result.outcome == "achieved"


def test_vision_requirement_uses_vision_authority():
    goal = contract(Requirement(id="v1", description="Скругление", method="vision",
                                required=True, weight=1))
    result = evaluate_goal(goal, [evidence(goal, "v1", "pass", source="vision",
                                           actual=None)],
                           revision=1, target_similarity=100)
    assert result.outcome == "achieved"
    assert result.authority == "vision"
    mixed = contract(numeric("d1", 50.0),
                     Requirement(id="v1", description="Скругление", method="vision"))
    result = evaluate_goal(mixed, [evidence(mixed, "d1", "pass"),
                                   evidence(mixed, "v1", "pass", source="vision", actual=None)],
                           revision=1, target_similarity=100)
    assert result.authority == "mixed"


def test_contract_fields_are_frozen_after_fixation():
    goal = contract(numeric("d1"), ambiguities=["x"])
    with pytest.raises(AttributeError):
        goal.requirements.append(numeric("d2"))
    with pytest.raises(AttributeError):
        goal.ambiguities.append("y")
    with pytest.raises(AttributeError):
        goal.requirements[0].weight = 5


def test_evaluate_goal_number_and_document_identity_validation():
    goal = contract(numeric("d1"))
    result = evaluate_goal(goal, [evidence(goal, "d1")], revision=1,
                           target_similarity=100, number=3)
    assert result.number == 3
    with pytest.raises(ValueError):
        evaluate_goal(goal, [], revision=1, target_similarity=100, number=0)

    unbound = evaluate_goal(goal, [evidence(goal, "d1", document_identity="")],
                            revision=1, target_similarity=100, document_identity="")
    assert unbound.outcome == "unknown"
    assert unbound.per_requirement[0]["status"] == "unknown"


def test_informational_contract_never_reports_a_score():
    goal = GoalContract(kind="informational", requirements=[
        Requirement(id="v1", description="Скругление", method="vision")])
    result = evaluate_goal(goal, [], revision=0, target_similarity=100)
    assert result.score is None
    assert result.outcome == "unknown"


def test_dimension_evidence_stores_binding_and_does_not_claim_semantics():
    requirement = Requirement(id="d1", description="Размер", method="cad",
                              expected=50.0, unit="mm")
    ok = dimension_evidence(requirement, {"name": "D1@Sketch1", "value": 50.0, "unit": "mm"},
                            bound_name="D1@Sketch1", revision=1, document_identity="p",
                            tool_call_id="call-1")
    assert ok.status == "pass"
    assert "D1@Sketch1" in ok.reason
    mismatch = dimension_evidence(
        requirement, {"name": "D2@Sketch1", "value": 50.0, "unit": "mm"},
        bound_name="D1@Sketch1", revision=1, document_identity="p")
    assert mismatch.status == "unknown"
    assert "D2@Sketch1" in mismatch.reason


def test_sheet_metal_rejects_wrong_unit_or_field():
    requirement = Requirement(id="t", description="Толщина", method="cad",
                              expected=2.0, unit="deg")
    wrong_unit = sheet_metal_evidence(requirement, {"thickness_mm": 2.0},
                                      field="thickness_mm", revision=1, document_identity="p")
    assert wrong_unit.status == "unknown"

    milli = Requirement(id="t", description="Толщина", method="cad",
                        expected=2.0, unit="mm")
    unknown_field = sheet_metal_evidence(milli, {"thickness_mm": 2.0},
                                         field="bend_radius_mm", revision=1,
                                         document_identity="p")
    assert unknown_field.status == "unknown"


def test_feature_tree_truncation_never_proves_absence():
    requirement = Requirement(id="f1", description="Элемент дерева", method="cad")
    present = tree_evidence(
        requirement, {"features": [{"name": "BaseFlange", "type": "SheetMetal"}]},
        expected="BaseFlange", limit=1000, revision=1, document_identity="p",
        tool_call_id="call-1")
    assert present.status == "pass"
    assert "геометр" in present.reason.lower()

    absent = tree_evidence(requirement, {"features": []}, expected="BaseFlange",
                           limit=1000, revision=1, document_identity="p")
    assert absent.status == "fail"

    truncated = tree_evidence(
        requirement, {"features": [{"name": f"F{index}"} for index in range(3)]},
        expected="BaseFlange", limit=3, revision=1, document_identity="p")
    assert truncated.status == "unknown"
    assert "усеч" in truncated.reason.lower()

    flagged = tree_evidence(requirement, {"features": [], "truncated": True},
                            expected="BaseFlange", limit=1000, revision=1,
                            document_identity="p")
    assert flagged.status == "unknown"

    malformed = tree_evidence(requirement, {"features": [{"type": "x"}]},
                             expected="BaseFlange", limit=1000, revision=1,
                             document_identity="p")
    assert malformed.status == "unknown"


def test_components_evidence_matches_name_path_and_suppression():
    requirement = Requirement(id="c1", description="Компонент", method="cad")
    value = {"components": [
        {"name": "Part-1", "path": "C:/Parts/part.sldprt", "suppressed": False},
        {"name": "Part-2", "path": "C:/Parts/hidden.sldprt", "suppressed": True},
    ]}
    ok = components_evidence(requirement, value, name="Part-1",
                             path="C:/Parts/part.sldprt", revision=1,
                             document_identity="p", tool_call_id="call-1")
    assert ok.status == "pass"
    assert "Part-1" in ok.reason

    wrong_path = components_evidence(requirement, value, name="Part-1",
                                     path="C:/Parts/other.sldprt", revision=1,
                                     document_identity="p")
    assert wrong_path.status == "fail"

    suppressed = components_evidence(requirement, value, name="Part-2", revision=1,
                                     document_identity="p")
    assert suppressed.status == "fail"

    malformed = components_evidence(requirement, {"components": [{"name": 1}]},
                                    name="Part-1", revision=1, document_identity="p")
    assert malformed.status == "unknown"

    missing_path = components_evidence(requirement, {"components": [{"name": "Part-1"}]},
                                       name="Part-1", revision=1, document_identity="p")
    assert missing_path.status == "unknown"


def test_mass_properties_requires_explicit_numeric_reference():
    without_reference = Requirement(id="m1", description="Масса", method="cad")
    missing = mass_properties_evidence(without_reference, {"mass_kg": 1.0},
                                       field="mass_kg", revision=1, document_identity="p")
    assert missing.status == "unknown"

    # Mass units are outside the contract's mm/deg enum; the adapter compares
    # the explicit numeric reference and leaves unit semantics to the binding.
    with_reference = Requirement(id="m2", description="Масса", method="cad",
                                 expected=1.0, tolerance=1e-6, unit="mm")
    ok = mass_properties_evidence(with_reference, {"mass_kg": 1.0},
                                  field="mass_kg", revision=1, document_identity="p",
                                  tool_call_id="call-1")
    assert ok.status == "pass"
    wrong = mass_properties_evidence(with_reference, {"mass_kg": 1.1},
                                     field="mass_kg", revision=1, document_identity="p")
    assert wrong.status == "fail"
    malformed = mass_properties_evidence(with_reference, {"mass_kg": "1.0"},
                                         field="mass_kg", revision=1, document_identity="p")
    assert malformed.status == "unknown"
    no_field = mass_properties_evidence(with_reference, {"mass_kg": 1.0},
                                        field="", revision=1, document_identity="p")
    assert no_field.status == "unknown"


def test_requested_file_evidence_reuses_existing_rules():
    requirement = Requirement(id="out1", description="Выходной файл", method="cad")
    path = "C:/Out/part.sldprt"
    after = (10, 100, 200)
    ok = requested_file_evidence(
        requirement, {"saved": path}, requested_path=path,
        signature_before=None, signature_after=after, revision=1,
        document_identity="p", tool_call_id="call-1")
    assert ok.status == "pass"
    assert path in ok.reason

    exported = requested_file_evidence(
        requirement, {"exported": "C:/Out/part.dxf"}, requested_path="C:/Out/part.dxf",
        signature_before=None, signature_after=after, revision=1, document_identity="p")
    assert exported.status == "pass"

    wrong_path = requested_file_evidence(
        requirement, {"saved": "C:/Out/other.sldprt"}, requested_path=path,
        signature_before=None, signature_after=after, revision=1, document_identity="p")
    assert wrong_path.status == "fail"

    stale = requested_file_evidence(
        requirement, {"saved": path}, requested_path=path,
        signature_before=after, signature_after=after, revision=1, document_identity="p")
    assert stale.status == "fail"
    assert "не измен" in stale.reason.lower()

    empty = requested_file_evidence(
        requirement, {"saved": path}, requested_path=path,
        signature_before=None, signature_after=(0, 100, 200), revision=1,
        document_identity="p")
    assert empty.status == "fail"

    missing = requested_file_evidence(
        requirement, {"saved": path}, requested_path=path,
        signature_before=None, signature_after=None, revision=1, document_identity="p")
    assert missing.status == "fail"


def test_requested_file_evidence_rejects_malformed_input():
    requirement = Requirement(id="out1", description="Выходной файл", method="cad")
    malformed_signature = requested_file_evidence(
        requirement, {"saved": "C:/Out/part.sldprt"},
        requested_path="C:/Out/part.sldprt", signature_before=None,
        signature_after=("10", 1, 1), revision=1, document_identity="p")
    assert malformed_signature.status == "unknown"

    missing_target = requested_file_evidence(
        requirement, {}, requested_path="C:/Out/part.sldprt", signature_before=None,
        signature_after=(10, 1, 1), revision=1, document_identity="p")
    assert missing_target.status == "unknown"


def test_sheet_metal_default_precision_flows_into_evaluation():
    requirement = Requirement(id="t", description="Толщина", method="cad",
                              expected=2.0, unit="mm")
    goal = contract(requirement)
    record = sheet_metal_evidence(requirement, {"thickness_mm": 2.00004},
                                  field="thickness_mm", revision=1,
                                  document_identity="p",
                                  contract_digest=goal.contract_digest)
    assert record.status == "pass"
    result = evaluate_goal(goal, [record], revision=1, target_similarity=100)
    assert result.per_requirement[0]["status"] == "pass"
    assert result.outcome == "achieved"


def test_malformed_bindings_and_identifiers_are_rejected():
    with pytest.raises(ValueError):
        GoalContract(kind="informational", version=True)
    with pytest.raises(ValueError):
        GoalContract(kind="informational", reference_ids=[""])

    goal = contract(numeric("d1"))
    with pytest.raises(ValueError):
        EvidenceRecord(id="e", requirement_id="d1", contract_digest=goal.contract_digest,
                       revision=1, document_identity="p", source="cad", status="pass",
                       tool_call_id="")
    with pytest.raises(ValueError):
        EvidenceRecord(id="e", requirement_id="d1", contract_digest=goal.contract_digest,
                       revision=1, document_identity="p", source="cad", status="pass",
                       actual=50.0, precision=0.5)

    requirement = goal.requirements[0]
    assert dimension_evidence(requirement, {"name": "D1", "value": 50.0, "unit": "mm"},
                              bound_name="", revision=1, document_identity="p").status == "unknown"
    assert sheet_metal_evidence(requirement, {"thickness_mm": 2.0}, field=[],
                                revision=1, document_identity="p").status == "unknown"


def test_evaluation_rejects_tolerance_stricter_than_evidence_precision():
    requirement = Requirement(id="d1", description="Размер", method="cad",
                              expected=50.0, tolerance=1e-9, unit="mm")
    goal = contract(requirement)
    record = EvidenceRecord(
        id="e-d1", requirement_id="d1", contract_digest=goal.contract_digest,
        revision=1, document_identity="p", source="cad", status="pass",
        actual=50.0, precision=1e-6, tool_call_id="call-1")
    result = evaluate_goal(goal, [record], revision=1, target_similarity=100)
    assert result.per_requirement[0]["status"] == "unknown"
    assert result.outcome == "unknown"
    assert result.score == 0


def test_requirement_ids_allow_unicode_identifiers():
    requirement = Requirement(id="толщина", description="Толщина", method="cad",
                              expected=2.0, unit="mm")
    goal = contract(requirement)
    record = sheet_metal_evidence(requirement, {"thickness_mm": 2.0},
                                  field="thickness_mm", revision=1,
                                  document_identity="p",
                                  contract_digest=goal.contract_digest)
    assert record.requirement_id == "толщина"
    result = evaluate_goal(goal, [record], revision=1, target_similarity=100)
    assert result.outcome == "achieved"


def test_numeric_expectations_are_cad_only():
    with pytest.raises(ValueError):
        Requirement(id="v1", description="Скругление", method="vision",
                    expected=1.0, unit="mm")
    with pytest.raises(ValueError):
        Requirement(id="f1", description="Элемент", method="cad", tolerance=0.1)
