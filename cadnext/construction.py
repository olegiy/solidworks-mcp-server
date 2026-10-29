"""Construction plan contracts and bounded, declarative verification."""

import math
from copy import deepcopy

from jsonschema import Draft202012Validator

_TEXT = {"type": "string", "minLength": 1, "maxLength": 200}
_NUMBER = {"type": "number"}
_VECTOR = {"type": "array", "items": _NUMBER, "minItems": 3, "maxItems": 3}
_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}
STAGE_SCHEMA = {"type": "object", "properties": {
    "id": _ID, "title": _TEXT, "expected_result": _TEXT},
    "required": ["id", "title", "expected_result"], "additionalProperties": False}
PLAN_SCHEMA = {"type": "object", "properties": {"stages": {
    "type": "array", "items": STAGE_SCHEMA, "minItems": 1, "maxItems": 32}},
    "required": ["stages"], "additionalProperties": False}


def _check_schema(kind, properties, required):
    return {"type": "object", "properties": {"kind": {"const": kind}, **properties},
            "required": ["kind", *required], "additionalProperties": False}


CHECKS_SCHEMA = {"type": "array", "minItems": 0, "maxItems": 32, "items": {"oneOf": [
    _check_schema("feature", {"name": _TEXT, "type": _TEXT}, ["name"]),
    _check_schema("body_count", {"expected": {"type": "integer", "minimum": 0}}, ["expected"]),
    _check_schema("dimension", {"name": _TEXT, "value": _NUMBER,
                                "unit": {"enum": ["mm", "deg"]}}, ["name", "value", "unit"]),
    _check_schema("sheet_metal", {"thickness_mm": {"type": "number", "exclusiveMinimum": 0},
                                  "bend_radius_mm": {"type": "number", "minimum": 0}},
                  ["thickness_mm", "bend_radius_mm"]),
    _check_schema("cylinder", {"radius_mm": {"type": "number", "exclusiveMinimum": 0},
                               "count": {"type": "integer", "minimum": 1},
                               "axis_point_mm": _VECTOR, "axis_direction": _VECTOR},
                  ["radius_mm", "count"]),
    _check_schema("component", {"name": _TEXT, "path": _TEXT, "fixed": {"type": "boolean"},
                                "translation_mm": _VECTOR,
                                "rotation": {"type": "array", "items": _NUMBER,
                                             "minItems": 9, "maxItems": 9}}, ["name"]),
    _check_schema("mate", {"name": _TEXT, "mate_type": {"type": "integer", "minimum": 0},
                           "lock_rotation": {"type": "boolean"},
                           "components": {"type": "array", "items": _TEXT,
                                          "minItems": 2, "maxItems": 4}}, ["mate_type"]),
]}}
_SECTIONS = {"feature": "features", "body_count": "bodies", "dimension": "dimensions",
             "sheet_metal": "sheet_metal", "cylinder": "surfaces",
             "component": "components", "mate": "mates"}


def model_tools(tools):
    tools = deepcopy([tool for tool in tools if tool["function"]["name"] not in
                      {"open_python_session", "close_python_session"}])
    for tool in tools:
        if tool["function"]["name"] == "execute_python":
            schema = tool["function"]["parameters"]
            schema["properties"].pop("session_id", None)
            schema["properties"].update(stage_id=_ID, checks=CHECKS_SCHEMA)
            schema["required"] = [key for key in schema.get("required", []) if key != "session_id"]
            schema["additionalProperties"] = False
    tools.append({"type": "function", "function": {
        "name": "set_construction_plan",
        "description": "Задать этапы до построения. Изменять можно только ещё не начатые этапы. "
                       "Один execute_python завершает этап; для продолжения добавь следующий этап.",
        "parameters": PLAN_SCHEMA}})
    return tools


def set_plan(current, stages):
    if not Draft202012Validator(PLAN_SCHEMA).is_valid({"stages": stages}):
        raise ValueError("Некорректный план построения.")
    if len({stage["id"] for stage in stages}) != len(stages):
        raise ValueError("ID этапов должны быть уникальны.")
    automatic = {stage["id"]: stage for stage in current if stage.get("application_owned") is True}
    for stage in stages:
        if stage["id"] in automatic and any(stage[key] != automatic[stage["id"]][key]
                                           for key in ("id", "title", "expected_result")):
            raise ValueError("Служебный этап нельзя менять.")
    stages = [stage for stage in stages if stage["id"] not in automatic]
    current = [stage for stage in current if stage["id"] not in automatic]
    if len(stages) + len(automatic) > 32:
        raise ValueError("Достигнут лимит этапов.")
    previous = {stage["id"]: stage for stage in current}
    for index, stage in enumerate(current):
        if stage["status"] != "pending":
            if index >= len(stages) or any(stages[index].get(key) != stage[key]
                                            for key in ("id", "title", "expected_result")):
                raise ValueError("Начатый этап нельзя менять, удалять или переставлять.")
    return [deepcopy(previous[stage["id"]]) if stage["id"] in previous and
            previous[stage["id"]]["status"] != "pending" else
            {**stage, "status": "pending", "checks": [], "tool_call_ids": []} for stage in stages
            ] + deepcopy(list(automatic.values()))


def validate_saved_plan(stages, history):
    if not isinstance(stages, list) or not isinstance(history, list) or len(history) > 1000:
        raise ValueError("Некорректные сохранённые этапы.")

    def validate(items, *, finished):
        if not isinstance(items, list) or len(items) > 32:
            raise ValueError("Некорректные сохранённые этапы.")
        clean = []
        for stage in items:
            if not isinstance(stage, dict):
                raise ValueError("Некорректный сохранённый этап.")
            allowed = {"done", "error", "stopped"} if finished else {
                "pending", "running", "done", "error", "stopped"}
            if (not isinstance(stage.get("status"), str) or stage["status"] not in allowed
                    or type(stage.get("application_owned", False)) is not bool
                    or not isinstance(stage.get("checks"), list)
                    or len(stage["checks"]) > 32
                    or not isinstance(stage.get("tool_call_ids"), list)
                    or len(stage["tool_call_ids"]) > 1000
                    or any(not isinstance(call_id, str) or not 0 < len(call_id) <= 200
                           for call_id in stage["tool_call_ids"])):
                raise ValueError("Некорректное состояние этапа.")
            for check in stage["checks"]:
                if (not isinstance(check, dict) or type(check.get("passed")) is not bool
                        or not isinstance(check.get("observed"), list)):
                    raise ValueError("Некорректные доказательства этапа.")
                inspection_request([check.get("expected")])
            clean.append({key: stage.get(key) for key in ("id", "title", "expected_result")})
        if clean:
            set_plan([], clean)

    validate(stages, finished=True)
    for revision, entry in enumerate(history, 1):
        if not isinstance(entry, dict) or type(entry.get("revision")) is not int or entry["revision"] != revision:
            raise ValueError("Некорректная история этапов.")
        validate(entry.get("stages"), finished=False)
    return deepcopy(stages), deepcopy(history)


def inspection_request(checks):
    if not Draft202012Validator(CHECKS_SCHEMA).is_valid(checks):
        raise ValueError("Некорректные декларативные проверки.")
    for check in checks:
        for value in check.values():
            numbers = value if isinstance(value, list) else [value]
            if any(type(number) is float and not math.isfinite(number) for number in numbers):
                raise ValueError("Проверка содержит неконечное число.")
        if "axis_direction" in check and math.hypot(*check["axis_direction"]) < 1e-12:
            raise ValueError("Направление оси не может быть нулевым.")
        if "axis_point_mm" in check and "axis_direction" not in check:
            raise ValueError("Для проверки положения отверстия нужно направление оси.")
    return {"sections": sorted({_SECTIONS[check["kind"]] for check in checks}),
            "dimension_names": [check["name"] for check in checks if check["kind"] == "dimension"]}


def _near(a, b, tolerance=.001):
    return (type(a) in {float, int} and math.isfinite(a) and
            math.isclose(a, b, abs_tol=tolerance, rel_tol=0))


def _vector_near(a, b, tolerance=.001):
    return isinstance(a, list) and len(a) == len(b) and all(
        _near(x, y, tolerance) for x, y in zip(a, b, strict=True))


def _cylinder_matches(surface, check):
    if surface.get("kind") != "cylinder" or not _near(surface.get("radius_mm"), check["radius_mm"]):
        return False
    if "axis_direction" in check:
        expected, actual = check["axis_direction"], surface.get("axis_direction", [])
        if len(actual) != 3 or math.hypot(*actual) < 1e-12:
            return False
        expected = [v / math.hypot(*expected) for v in expected]
        actual = [v / math.hypot(*actual) for v in actual]
        dot = sum(a*b for a, b in zip(expected, actual, strict=True))
        if math.degrees(math.acos(min(1, abs(dot)))) > .01:
            return False
        if "axis_point_mm" in check:
            point = surface.get("axis_point_mm", [])
            if len(point) != 3:
                return False
            delta = [a-b for a, b in zip(point, check["axis_point_mm"], strict=True)]
            projection = sum(a*b for a, b in zip(delta, expected, strict=True))
            if math.hypot(*(a-projection*b for a, b in zip(delta, expected, strict=True))) > .001:
                return False
    return True


def evaluate_checks(checks, data):
    inspection_request(checks)
    results = []
    for check in checks:
        kind = check["kind"]
        observed = data.get(_SECTIONS[kind], [])
        passed = False
        if not data.get("truncated"):
            if kind == "body_count":
                passed = "bodies" in data and len(observed) == check["expected"]
            elif kind == "feature":
                passed = any(item["name"] == check["name"] and
                    ("type" not in check or item["type"] == check["type"]) for item in observed)
            elif kind == "dimension":
                passed = any(item["name"] == check["name"] and item.get("driving") is True and
                    item["unit"] == check["unit"] and _near(item["value"], check["value"],
                        .01 if check["unit"] == "deg" else .001) for item in observed)
            elif kind == "sheet_metal":
                passed = any(all(_near(item.get(key), check[key])
                                for key in ("thickness_mm", "bend_radius_mm")) for item in observed)
            elif kind == "cylinder":
                passed = sum(_cylinder_matches(item, check) for item in observed) == check["count"]
            elif kind == "component":
                passed = any(item["name"] == check["name"] and not item.get("suppressed") and
                    all((_vector_near(item.get(key), value, .00001 if key == "rotation" else .001)
                         if isinstance(value, list) else item.get(key) == value)
                        for key, value in check.items() if key not in {"kind", "name"})
                    for item in observed)
            elif kind == "mate":
                passed = any(item["type"] == check["mate_type"] and item.get("error_code") == 0 and
                    item.get("suppressed") is False and
                    ("lock_rotation" not in check or item.get("lock_rotation") is check["lock_rotation"]) and
                    ("name" not in check or item["name"] == check["name"]) and
                    ("components" not in check or sorted(item["components"]) == sorted(check["components"]))
                    for item in observed)
        results.append({"expected": deepcopy(check), "passed": bool(passed),
                        "observed": deepcopy(observed)})
    return results
