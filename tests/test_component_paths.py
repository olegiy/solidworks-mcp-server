import pytest

from cadnext.construction import evaluate_checks


@pytest.mark.parametrize("actual, passed", [
    (r"C:\Tests\shaft.SLDPRT", True),
    ("c:/tests/shaft.sldprt", True),
    (r"C:\Other\shaft.SLDPRT", False),
    (None, False),
])
def test_component_file_checks_follow_windows_path_case_and_separators(actual, passed):
    checks = [{"kind": "component", "name": "shaft-1", "path": r"C:\Tests\shaft.sldprt", "fixed": False}]
    data = {"components": [{"name": "shaft-1", "path": actual, "fixed": False, "suppressed": False}]}
    assert evaluate_checks(checks, data)[0]["passed"] is passed
