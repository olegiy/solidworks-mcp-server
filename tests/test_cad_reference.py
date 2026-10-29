from pathlib import Path

import httpx
import pytest

from cadnext import cad_reference as reference


def test_installed_typelib_supplies_actual_argument_order_and_version():
    path = Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\sldworks.tlb")
    if not path.exists():
        pytest.skip("SolidWorks type library not installed")
    data = reference.load_index(path)
    matches = reference.search(data, "IFeatureManager InsertSheetMetalBaseFlange2")
    assert matches[0]["symbol_id"] == "IFeatureManager.InsertSheetMetalBaseFlange2"
    detail = reference.get_symbol(data, matches[0]["symbol_id"])
    assert detail["version"]["major"] == 33
    assert detail["overloads"][0]["parameters"][0]["name"] == "Thickness"
    assert detail["overloads"][0]["parameters"][-1]["name"] == "UseAutoSelect"
    assert len(detail["overloads"][0]["parameters"]) == 19
    enum = reference.get_symbol(data, "swDimensionDrivenState_e.swDimensionDriving")
    assert enum["value"] == 2


def test_help_is_optional_and_network_failure_keeps_local_signature():
    local = {"symbol_id": "ISketchManager.CreateLine", "version": {"year": 2025},
             "overloads": [{"parameters": [{"name": "X1"}]}]}
    requests = []

    def offline(request):
        requests.append(request)
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        result = reference.with_help(local, include_help=False, client=client)
        assert not requests
        result = reference.with_help(local, include_help=True, client=client)
    assert result["overloads"] == local["overloads"]
    assert result["help"]["available"] is False
    assert requests[0].url.host == "help.solidworks.com"


def test_help_does_not_follow_external_redirects_or_return_html_as_code():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://example.org/private"})

    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        result = reference.with_help({"symbol_id": "ISketchManager.CreateLine",
                                     "version": {"year": 2025}}, include_help=True, client=client)
    assert len(requests) == 1
    assert result["help"]["available"] is False


def test_examples_are_searchable_and_only_live_executed_recipes_have_version_labels():
    matches = reference.search_examples("sheet metal open profile")
    assert matches[0]["example_id"] == "sheet-open-profile"
    example = reference.get_example(matches[0]["example_id"])
    assert example["verified_versions"] == ["SolidWorks 2025 SP5 (33.5.0)"]
    assert example["parameters"] and example["api_symbols"] and example["checks"]
    compile(example["code"], "<example>", "exec")
    for name in ("holes-transform", "revolution", "chamfer", "assembly-insert", "assembly-mates"):
        item = reference.get_example(name)
        compile(item["code"], name, "exec")
        assert item["prerequisites"] and item["expected_features"]
        assert item["verified_versions"] == (["SolidWorks 2025 SP5 (33.5.0)"]
            if name in {"revolution", "chamfer"} else [])


def test_revolution_recipe_exposes_its_live_executed_dimensioned_profile_stage():
    from scripts.staged_acceptance import REVOLVED_PROFILE_CODE

    example = reference.get_example("revolution")
    assert example["preparation_code"] == REVOLVED_PROFILE_CODE
    assert "FeatureRevolve2" not in example["preparation_code"]
    compile(example["preparation_code"], "<revolution-profile>", "exec")
    assert example["preparation_checks"] == [
        {"kind": "dimension", "name": f"{name}@BushingProfile", "value": value, "unit": "mm"}
        for name, value in (("OuterRadius", 15), ("InnerRadius", 8), ("Length", 40))]


def test_sheet_recipe_exposes_profile_check_before_base_flange_stage():
    example = reference.get_example("sheet-open-profile")
    assert "InsertSheetMetalBaseFlange2" not in example["preparation_code"]
    assert "InsertSheetMetalBaseFlange2" in example["code"]
    compile(example["preparation_code"], "<sheet-profile>", "exec")
    assert {"kind": "body_count", "expected": 0} in example["preparation_checks"]
    assert {"kind": "dimension", "name": "LegAOuter@BracketProfile", "value": 60,
            "unit": "mm"} in example["preparation_checks"]


def test_hole_recipe_uses_the_live_dimensioned_coordinate_transform_stage():
    from scripts.staged_acceptance import HOLE_CODE

    example = reference.get_example("holes-transform")
    assert example["code"] == HOLE_CODE
    assert "Hole1Diameter@HoleSketch" in [c["name"] for c in example["declared_checks"]
                                         if c["kind"] == "dimension"]
    assert example["next_tool"] == "cut_through"


def test_assembly_recipes_expose_the_same_geometric_selection_and_shaft_as_direct_acceptance():
    from scripts.staged_acceptance import MATE_SELECTION_CODE, SHAFT_PROFILE_CODE

    insertion = reference.get_example("assembly-insert")
    mates = reference.get_example("assembly-mates")
    assert insertion["shaft_preparation_code"] == SHAFT_PROFILE_CODE
    assert mates["selection_code"] == MATE_SELECTION_CODE
    assert mates["selection_parameters"] == {"mate_kind": ["concentric", "coincident"]}


@pytest.mark.parametrize("status,success", [(1, True), (0, False), (4, False)])
def test_mate_recipe_checks_swaddmateerror_enum_instead_of_feature_error_codes(status, success):
    from types import SimpleNamespace as NS

    error = NS(value=-1)
    calls = []

    def add_mate(*args):
        calls.append(args)
        args[-1].value = status
        return object()

    env = {"model": NS(AddMate5=add_mate), "byref_i4": lambda: error}
    if success:
        exec(reference.get_example("assembly-mates")["code"], env)
        assert env["result"]["mate_type"] == 1
    else:
        with pytest.raises(RuntimeError):
            exec(reference.get_example("assembly-mates")["code"], env)
    assert len(calls) == 1
