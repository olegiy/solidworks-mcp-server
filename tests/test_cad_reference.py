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


def test_examples_are_searchable_and_not_claimed_live_verified():
    matches = reference.search_examples("sheet metal open profile")
    assert matches[0]["example_id"] == "sheet-open-profile"
    example = reference.get_example(matches[0]["example_id"])
    assert example["verified_versions"] == []
    assert example["parameters"] and example["api_symbols"] and example["checks"]
    compile(example["code"], "<example>", "exec")
    for name in ("holes-transform", "revolution", "chamfer", "assembly-insert", "assembly-mates"):
        item = reference.get_example(name)
        compile(item["code"], name, "exec")
        assert item["prerequisites"] and item["expected_features"]
