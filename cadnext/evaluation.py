"""Goal contract, fresh CAD evidence, and deterministic evaluation.

The evaluator never calls CAD or a model. Callers pass evidence produced from an
allowlisted CAD read for the current run revision and document identity.
"""

import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

_MAX_REQUIREMENTS = 64
_MAX_TEXT = 2000
_DIMENSION_PRECISION = 1e-6
_SHEET_METAL_PRECISION = 5e-5
_READING_PRECISIONS = {_DIMENSION_PRECISION, _SHEET_METAL_PRECISION}
_SHEET_METAL_FIELDS = {"thickness_mm", "bend_radius_mm"}
_IDENTIFIER = re.compile(r"^[\w.:-]{1,100}$", re.UNICODE)
_STATUSES = {"pass", "fail", "unknown"}
_SOURCES = {"cad", "vision"}
_METHODS = {"cad", "vision"}
_UNITS = {"mm", "deg"}
_INVALID_SIGNATURE = object()


def _finite_number(value, *, name, minimum=None, maximum=None):
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} is below the allowed range.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} is above the allowed range.")
    return value


def _integer(value, *, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
    return value


def _text(value, *, name, maximum=_MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters.")
    return value


@dataclass(frozen=True)
class EvaluationOptions:
    enabled: bool = True
    target_similarity: float = 100.0
    max_evaluations: int = 10
    max_no_progress: int = 3

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean.")
        _finite_number(self.target_similarity, name="target_similarity",
                       minimum=0.0, maximum=100.0)
        if self.target_similarity <= 0:
            raise ValueError("target_similarity must be greater than zero.")
        _integer(self.max_evaluations, name="max_evaluations", minimum=1, maximum=100)
        _integer(self.max_no_progress, name="max_no_progress", minimum=1, maximum=10)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Requirement:
    id: str
    description: str
    source: Literal["request", "attachment"] = "request"
    required: bool = True
    weight: int = 1
    method: Literal["cad", "vision"] = "cad"
    expected: float | None = None
    tolerance: float | None = None
    unit: Literal["mm", "deg"] | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("Requirement id is invalid.")
        _text(self.description, name="description")
        if self.source not in {"request", "attachment"}:
            raise ValueError("Requirement source is invalid.")
        if type(self.required) is not bool:
            raise ValueError("Requirement required flag must be a boolean.")
        _integer(self.weight, name="weight", minimum=1, maximum=100)
        if self.method not in _METHODS:
            raise ValueError("Requirement method is invalid.")
        if self.expected is not None:
            _finite_number(self.expected, name="expected")
        if self.tolerance is not None:
            _finite_number(self.tolerance, name="tolerance", minimum=0.0)
        if self.unit is not None and self.unit not in _UNITS:
            raise ValueError("Requirement unit is invalid.")
        if self.method == "vision" and (
                self.expected is not None or self.tolerance is not None
                or self.unit is not None):
            raise ValueError("A vision requirement cannot carry numeric expectations.")
        if self.method == "cad" and self.expected is not None and self.unit is None:
            raise ValueError("A numeric CAD requirement needs a unit.")
        if self.expected is None and (self.tolerance is not None or self.unit is not None):
            raise ValueError("Tolerance and unit require an expected value.")


def _contract_payload(kind, requirements, reference_ids, ambiguities):
    return {
        "version": 1,
        "kind": kind,
        "requirements": [asdict(item) for item in requirements],
        "reference_ids": list(reference_ids),
        "ambiguities": list(ambiguities),
    }


@dataclass(frozen=True)
class GoalContract:
    kind: Literal["cad", "informational"]
    requirements: list[Requirement] = field(default_factory=list)
    reference_ids: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    version: int = 1
    contract_digest: str = field(default="", compare=False)

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported goal contract version.")
        if self.kind not in {"cad", "informational"}:
            raise ValueError("Goal kind is invalid.")
        if not isinstance(self.requirements, (list, tuple)):
            raise ValueError("Goal requirements must be a sequence.")
        if len(self.requirements) > _MAX_REQUIREMENTS:
            raise ValueError("A goal contract allows at most 64 requirements.")
        seen = set()
        for requirement in self.requirements:
            if not isinstance(requirement, Requirement):
                raise ValueError("Goal requirements must be Requirement values.")
            if requirement.id in seen:
                raise ValueError("Requirement ids must be unique.")
            seen.add(requirement.id)
        if self.kind == "cad" and not self.requirements:
            raise ValueError("A CAD goal needs at least one frozen requirement.")
        if not isinstance(self.reference_ids, (list, tuple)) or len(self.reference_ids) > 64:
            raise ValueError("reference_ids is invalid.")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 200
               for item in self.reference_ids):
            raise ValueError("reference_ids is invalid.")
        if not isinstance(self.ambiguities, (list, tuple)) or len(self.ambiguities) > 64:
            raise ValueError("ambiguities is invalid.")
        for item in self.ambiguities:
            _text(item, name="ambiguity")
        # Freeze the sequences so the digest cannot drift after fixation.
        object.__setattr__(self, "requirements", tuple(self.requirements))
        object.__setattr__(self, "reference_ids", tuple(self.reference_ids))
        object.__setattr__(self, "ambiguities", tuple(self.ambiguities))
        payload = json.dumps(_contract_payload(self.kind, self.requirements,
                                               self.reference_ids, self.ambiguities),
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "contract_digest",
                           hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def as_snapshot(self):
        return {**_contract_payload(self.kind, self.requirements,
                                    self.reference_ids, self.ambiguities),
                "contract_digest": self.contract_digest}


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    requirement_id: str
    contract_digest: str
    revision: int
    document_identity: str
    source: Literal["cad", "vision"]
    status: Literal["pass", "fail", "unknown"]
    actual: float | str | None = None
    precision: float | None = None
    reason: str = ""
    tool_call_id: str | None = None
    image_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("Evidence id is invalid.")
        if not isinstance(self.requirement_id, str) or not _IDENTIFIER.fullmatch(self.requirement_id):
            raise ValueError("Evidence requirement id is invalid.")
        if not isinstance(self.contract_digest, str) or len(self.contract_digest) > 128:
            raise ValueError("Evidence contract digest is invalid.")
        _integer(self.revision, name="revision", minimum=0, maximum=10**9)
        if (not isinstance(self.document_identity, str)
                or len(self.document_identity) > 500):
            raise ValueError("Evidence document identity is invalid.")
        if self.source not in _SOURCES or self.status not in _STATUSES:
            raise ValueError("Evidence source or status is invalid.")
        if self.actual is not None and type(self.actual) not in {int, float, str}:
            raise ValueError("Evidence actual value is invalid.")
        if type(self.actual) in {int, float}:
            _finite_number(self.actual, name="actual")
        if self.precision is not None:
            _finite_number(self.precision, name="precision", minimum=0.0, maximum=1.0)
            if self.precision not in _READING_PRECISIONS:
                raise ValueError("precision must be an allowlisted reading precision.")
        if not isinstance(self.reason, str) or len(self.reason) > _MAX_TEXT:
            raise ValueError("Evidence reason is invalid.")
        if self.tool_call_id is not None and (
                not isinstance(self.tool_call_id, str) or not self.tool_call_id.strip()
                or len(self.tool_call_id) > 200):
            raise ValueError("Evidence tool call id is invalid.")
        if not isinstance(self.image_ids, tuple) or any(
                not isinstance(item, str) or not item.strip() or len(item) > 200
                for item in self.image_ids):
            raise ValueError("Evidence image ids are invalid.")

    def as_snapshot(self):
        return asdict(self)


@dataclass(frozen=True)
class GoalEvaluation:
    number: int
    revision: int
    contract_digest: str
    score: float | None
    outcome: Literal["achieved", "not_achieved", "unknown"]
    authority: Literal["cad", "mixed", "vision"]
    per_requirement: tuple[dict, ...]
    evidence_ids: tuple[str, ...]
    passed_weight: int
    total_weight: int
    target_similarity: float
    at: float

    def as_snapshot(self):
        data = asdict(self)
        data["per_requirement"] = [dict(item) for item in self.per_requirement]
        data["evidence_ids"] = list(self.evidence_ids)
        return data

    @property
    def passed(self):
        return sum(int(item["weight"]) for item in self.per_requirement
                   if item["status"] == "pass")


def _tolerance(requirement, *, precision=_DIMENSION_PRECISION):
    return requirement.tolerance if requirement.tolerance is not None else precision


def _numeric_status(actual, expected, tolerance):
    return "pass" if abs(actual - expected) <= tolerance else "fail"


def evaluate_goal(contract: GoalContract, evidence: list[EvidenceRecord], *,
                  revision: int, target_similarity: float,
                  document_identity: str | None = None,
                  number: int = 1) -> GoalEvaluation:
    if not isinstance(contract, GoalContract):
        raise ValueError("A goal contract is required.")
    _integer(revision, name="revision", minimum=0, maximum=10**9)
    _integer(number, name="number", minimum=1, maximum=10**9)
    _finite_number(target_similarity, name="target_similarity", minimum=0.0, maximum=100.0)
    if target_similarity <= 0:
        raise ValueError("target_similarity must be greater than zero.")
    if document_identity is not None and (
            not isinstance(document_identity, str) or len(document_identity) > 500):
        raise ValueError("document_identity is invalid.")
    if not isinstance(evidence, list):
        raise ValueError("Evidence must be a list.")
    seen_ids = set()
    for record in evidence:
        if not isinstance(record, EvidenceRecord):
            raise ValueError("Evidence must contain EvidenceRecord values.")
        if record.id in seen_ids:
            raise ValueError("Evidence ids must be unique.")
        seen_ids.add(record.id)
    if document_identity == "":
        # Without a bound document no CAD read can be considered fresh.
        evidence = []

    per_requirement = []
    used_ids = []
    passed_weight = 0
    total_weight = 0
    cad_passes = vision_passes = False
    for requirement in contract.requirements:
        total_weight += requirement.weight
        candidates = [record for record in evidence
                      if record.requirement_id == requirement.id
                      and record.contract_digest == contract.contract_digest
                      and record.revision == revision]
        if document_identity is not None:
            candidates = [record for record in candidates
                          if record.document_identity == document_identity]
        selected = candidates[-1] if candidates else None
        status, reason, source = "unknown", "", None
        actual = None
        if selected is None:
            reason = ("Нет свежего доказательства для текущей ревизии, контракта и документа. "
                      "No fresh evidence for this revision, contract, and document.")
        else:
            actual = selected.actual
            source = selected.source
            reason = selected.reason
            wrong_source = ((requirement.method == "cad" and selected.source != "cad")
                            or (requirement.method == "vision" and selected.source != "vision"))
            if wrong_source:
                status = "unknown"
                reason = ("CAD-требование нельзя засчитать по vision без CAD-чтения. "
                          "A numeric CAD requirement needs CAD evidence; vision cannot certify it."
                          if requirement.method == "cad" else
                          "Визуальное требование требует vision-подтверждения. "
                          "A vision requirement needs vision evidence.")
            elif selected.status == "unknown":
                status = "unknown"
                reason = selected.reason or ("Доказательство неполно. Evidence is incomplete.")
            elif requirement.method == "cad" and requirement.expected is not None:
                if type(actual) not in {int, float} or not math.isfinite(actual):
                    status = "unknown"
                    reason = ("Нет числового фактического значения CAD. "
                              "There is no numeric CAD actual value.")
                else:
                    precision = (selected.precision if selected.precision is not None
                                 else _DIMENSION_PRECISION)
                    tolerance = _tolerance(requirement, precision=precision)
                    if tolerance < precision:
                        status = "unknown"
                        reason = ("Допуск строже точности чтения; значение не засчитывается. "
                                  "The tolerance is stricter than the reading precision.")
                    else:
                        status = _numeric_status(actual, requirement.expected, tolerance)
                        if status == "fail" and selected.status == "pass":
                            reason = ("Фактическое значение не совпало с ожидаемым; "
                                      "модельный статус pass не принят. "
                                      "The actual value mismatched; a model-reported "
                                      "pass is rejected.")
            else:
                status = selected.status
                if status == "pass" and source == "vision":
                    note = ("Визуальная оценка модели; это не геометрическое доказательство. "
                            "Model visual assessment; not geometric proof.")
                    reason = f"{reason} {note}".strip()
        if status == "pass":
            passed_weight += requirement.weight
            if source == "vision":
                vision_passes = True
            elif source == "cad":
                cad_passes = True
        if selected is not None:
            used_ids.append(selected.id)
        per_requirement.append({
            "requirement_id": requirement.id,
            "description": requirement.description,
            "required": requirement.required,
            "weight": requirement.weight,
            "method": requirement.method,
            "status": status,
            "actual": actual,
            "precision": selected.precision if selected is not None else None,
            "expected": requirement.expected,
            "tolerance": requirement.tolerance,
            "unit": requirement.unit,
            "source": source,
            "reason": reason,
            "evidence_id": selected.id if selected is not None else None,
            "images": list(selected.image_ids) if selected is not None else [],
        })

    raw_score = (100.0 * passed_weight / total_weight
                 if total_weight and contract.kind == "cad" else None)
    score = round(raw_score, 6) if raw_score is not None else None
    required_states = [item["status"] for item in per_requirement if item["required"]]
    if contract.kind == "informational" or total_weight == 0:
        outcome = "unknown"
    elif "fail" in required_states:
        outcome = "not_achieved"
    elif "unknown" in required_states:
        outcome = "unknown"
    elif target_similarity >= 100:
        outcome = "achieved" if passed_weight == total_weight else "not_achieved"
    else:
        outcome = ("achieved" if raw_score is not None and raw_score >= target_similarity
                   else "not_achieved")
    authority = ("mixed" if cad_passes and vision_passes else
                 "vision" if vision_passes else "cad")
    return GoalEvaluation(
        number=number, revision=revision, contract_digest=contract.contract_digest,
        score=score, outcome=outcome, authority=authority,
        per_requirement=tuple(per_requirement), evidence_ids=tuple(used_ids),
        passed_weight=passed_weight, total_weight=total_weight,
        target_similarity=float(target_similarity), at=time.time())


def update_progress(previous: GoalEvaluation | None, current: GoalEvaluation,
                    no_progress: int, *, best_weight: int | None = None) -> int:
    """Return the next no-progress counter.

    The required three-argument form compares against the immediately previous
    result. A caller that keeps the best passed weight across corrections passes
    ``best_weight`` so a recovery below that maximum is not new progress.
    """
    if previous is not None and not isinstance(previous, GoalEvaluation):
        raise ValueError("previous must be a GoalEvaluation or None.")
    if not isinstance(current, GoalEvaluation):
        raise ValueError("current must be a GoalEvaluation.")
    _integer(no_progress, name="no_progress", minimum=0, maximum=10**9)
    if best_weight is not None:
        _integer(best_weight, name="best_weight", minimum=0, maximum=10**9)
    if previous is None:
        return 0
    if best_weight is None:
        best_weight = previous.passed_weight
    # A caller-provided historical maximum can only be raised by the previous result.
    best_weight = max(best_weight, previous.passed_weight)
    if current.passed_weight > best_weight:
        return 0
    return no_progress + 1


def _evidence(requirement, status, actual, reason, *, revision, document_identity,
              source="cad", tool_call_id=None, contract_digest="", image_ids=(), suffix="",
              record_id=None, precision=None):
    return EvidenceRecord(
        id=record_id or f"{requirement.id}:{source}:{revision}:{suffix or status}"[:100],
        requirement_id=requirement.id,
        contract_digest=contract_digest,
        revision=revision,
        document_identity=document_identity,
        source=source,
        status=status,
        actual=actual,
        precision=precision,
        reason=reason,
        tool_call_id=tool_call_id,
        image_ids=tuple(image_ids),
    )


def _actual_dimension(value: dict, unit: str):
    keys = ("value_deg", "value") if unit == "deg" else ("value_mm", "value")
    for key in keys:
        item = value.get(key)
        if type(item) in {int, float} and math.isfinite(item):
            return item
    return None


def dimension_evidence(requirement: Requirement, value: dict, *, bound_name: str,
                       revision: int, document_identity: str, tool_call_id: str | None = None,
                       contract_digest: str = "") -> EvidenceRecord:
    options = {"revision": revision, "document_identity": document_identity,
               "tool_call_id": tool_call_id, "contract_digest": contract_digest,
               "suffix": "dimension", "precision": _DIMENSION_PRECISION}
    if not isinstance(bound_name, str) or not bound_name.strip() or len(bound_name) > 200:
        return _evidence(requirement, "unknown", None,
                         "Не задана привязка размера. The dimension binding is missing.",
                         **options)
    binding = (f"Привязка размера: {bound_name}. Dimension binding: {bound_name}. "
               "Имя не доказывает семантику признака. A name does not prove feature semantics.")
    if not isinstance(value, dict):
        return _evidence(requirement, "unknown", None,
                         f"{binding} Некорректный ответ get_dimension. "
                         "Invalid get_dimension result.", **options)
    observed = value.get("name")
    if observed != bound_name:
        return _evidence(
            requirement, "unknown", None,
            f"Имя размера {observed!r} не совпало с привязкой {bound_name!r}; "
            f"число не доказывает семантику. The dimension name {observed!r} does not match "
            f"the binding {bound_name!r}; a number alone is not proof.", **options)
    unit = value.get("unit")
    if (not isinstance(unit, str) or unit not in _UNITS
            or requirement.unit is None or unit != requirement.unit):
        return _evidence(requirement, "unknown", None,
                         f"{binding} Единица размера не совпала с требованием. "
                         "The dimension unit does not match the requirement.", **options)
    actual = _actual_dimension(value, unit)
    if actual is None or requirement.expected is None:
        return _evidence(requirement, "unknown", actual,
                         f"{binding} Нет числового фактического или ожидаемого значения. "
                         "The numeric actual or expected value is missing.", **options)
    tolerance = _tolerance(requirement, precision=_DIMENSION_PRECISION)
    if tolerance < _DIMENSION_PRECISION:
        return _evidence(requirement, "unknown", actual,
                         f"{binding} Допуск строже точности чтения get_dimension "
                         "(tolerance is below the dimension reading precision).", **options)
    status = _numeric_status(actual, requirement.expected, tolerance)
    return _evidence(requirement, status, actual,
                     f"{binding} " + ("Совпало с допуском. Within tolerance."
                                      if status == "pass" else
                                      "Не совпало с ожидаемым значением. Outside tolerance."),
                     **options)


def sheet_metal_evidence(requirement: Requirement, value: dict, *, field: str,
                         revision: int, document_identity: str,
                         tool_call_id: str | None = None,
                         contract_digest: str = "") -> EvidenceRecord:
    suffix = field if isinstance(field, str) and field in _SHEET_METAL_FIELDS else "sheet_metal"
    options = {"revision": revision, "document_identity": document_identity,
               "tool_call_id": tool_call_id, "contract_digest": contract_digest,
               "suffix": suffix, "precision": _SHEET_METAL_PRECISION}
    if (not isinstance(value, dict) or not isinstance(field, str)
            or field not in _SHEET_METAL_FIELDS or field not in value):
        return _evidence(requirement, "unknown", None,
                         "Нет запрошенного поля листовых данных. "
                         "The requested sheet-metal field is missing or not a supported field.",
                         **options)
    if requirement.unit != "mm":
        return _evidence(requirement, "unknown", None,
                         "Листовые размеры читаются в мм; единица требования не мм. "
                         "Sheet-metal reads are in mm; the requirement unit is not mm.",
                         **options)
    actual = value.get(field)
    if type(actual) not in {int, float} or not math.isfinite(actual):
        return _evidence(requirement, "unknown", None,
                         "Нет числового значения листовых данных. "
                         "The sheet-metal value is not numeric.", **options)
    if requirement.expected is None:
        return _evidence(requirement, "unknown", actual,
                         "Не задан ожидаемый размер. The expected value is missing.", **options)
    tolerance = _tolerance(requirement, precision=_SHEET_METAL_PRECISION)
    if tolerance < _SHEET_METAL_PRECISION:
        return _evidence(requirement, "unknown", actual,
                         "Допуск строже точности листового чтения (tolerance is below the "
                         "sheet-metal reading precision).", **options)
    status = _numeric_status(actual, requirement.expected, tolerance)
    return _evidence(requirement, status, actual,
                     "Листовое чтение округлено до 4 знаков; " + (
                         "совпало с допуском. The sheet-metal read is rounded to 4 decimals "
                         "and is within tolerance." if status == "pass" else
                         "не совпало с ожидаемым значением. The sheet-metal read is rounded "
                         "to 4 decimals and is outside tolerance."), **options)


def presence_evidence(requirement: Requirement, names: list[str], *, expected: str,
                      revision: int, document_identity: str,
                      tool_call_id: str | None = None,
                      contract_digest: str = "") -> EvidenceRecord:
    options = {"revision": revision, "document_identity": document_identity,
               "tool_call_id": tool_call_id, "contract_digest": contract_digest,
               "suffix": "presence"}
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        return _evidence(requirement, "unknown", None,
                         "Некорректный список объектов. Invalid object listing.", **options)
    if not isinstance(expected, str) or not expected.strip():
        return _evidence(requirement, "unknown", None,
                         "Не задано ожидаемое имя объекта. The expected object name is missing.",
                         **options)
    status = "pass" if expected in names else "fail"
    return _evidence(requirement, status, expected,
                     "Объект найден. The object is present." if status == "pass" else
                     "Объект не найден. The object is missing.", **options)


def mass_properties_evidence(requirement: Requirement, value: dict, *, field: str,
                             revision: int, document_identity: str,
                             tool_call_id: str | None = None,
                             contract_digest: str = "") -> EvidenceRecord:
    options = {"revision": revision, "document_identity": document_identity,
               "tool_call_id": tool_call_id, "contract_digest": contract_digest,
               "suffix": "mass", "precision": _DIMENSION_PRECISION}
    if (not isinstance(value, dict) or not isinstance(field, str) or not field.strip()
            or field not in value):
        return _evidence(requirement, "unknown", None,
                         "Нет числового свойства модели. The mass property is not numeric.",
                         **options)
    actual = value[field]
    if type(actual) not in {int, float} or not math.isfinite(actual):
        return _evidence(requirement, "unknown", None,
                         "Недопустимое числовое свойство модели. Invalid mass property.",
                         **options)
    if requirement.expected is None:
        return _evidence(requirement, "unknown", actual,
                         "Не задан явный числовой эталон. The explicit numeric reference "
                         "is missing.", **options)
    tolerance = _tolerance(requirement, precision=_DIMENSION_PRECISION)
    if tolerance < _DIMENSION_PRECISION:
        return _evidence(requirement, "unknown", actual,
                         "Допуск строже точности чтения массовых свойств. Tolerance is "
                         "stricter than the mass-property reading precision.", **options)
    status = _numeric_status(actual, requirement.expected, tolerance)
    return _evidence(requirement, status, actual,
                     "Совпало с явным числовым эталоном. Within the explicit numeric reference."
                     if status == "pass" else
                     "Не совпало с эталоном. Outside the explicit numeric reference.",
                     **options)


def _normalized_signature(value):
    if value is None:
        return None
    if (isinstance(value, (tuple, list)) and len(value) == 3
            and all(type(item) is int for item in value)):
        return tuple(value)
    return _INVALID_SIGNATURE


def tree_evidence(requirement: Requirement, value: dict, *, expected: str, limit: int,
                  revision: int, document_identity: str,
                  tool_call_id: str | None = None,
                  contract_digest: str = "") -> EvidenceRecord:
    """Return presence evidence from a feature-tree read.

    A tree only proves that a name is listed; it never proves geometry, internal
    shape, or rebuild correctness. A tree at the read limit is treated as
    possibly truncated, so a missing name becomes unknown instead of fail.
    """
    options = {"revision": revision, "document_identity": document_identity,
               "tool_call_id": tool_call_id, "contract_digest": contract_digest,
               "suffix": "tree"}
    _integer(limit, name="limit", minimum=1, maximum=10**6)
    if not isinstance(expected, str) or not expected.strip() or len(expected) > 200:
        return _evidence(requirement, "unknown", None,
                         "Не задано имя элемента дерева. The feature name is missing.",
                         **options)
    if not isinstance(value, dict) or not isinstance(value.get("features"), list):
        return _evidence(requirement, "unknown", None,
                         "Некорректный ответ get_feature_tree. Invalid get_feature_tree result.",
                         **options)
    features = value["features"]
    names = []
    for item in features:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return _evidence(requirement, "unknown", None,
                             "Некорректный элемент дерева. Invalid feature entry.", **options)
        names.append(item["name"])
    truncated = len(features) >= limit or value.get("truncated") is True
    if expected in names:
        return _evidence(
            requirement, "pass", expected,
            "Элемент присутствует в дереве; наличие в дереве не доказывает геометрию, "
            "внутреннюю форму или корректность перестроения. Present in the tree; tree "
            "presence does not prove geometry, internal shape, or rebuild correctness.",
            **options)
    if truncated:
        return _evidence(
            requirement, "unknown", expected,
            "Дерево может быть усечено; отсутствие элемента не доказано. The tree may be "
            "truncated; the feature's absence is not proven.", **options)
    return _evidence(requirement, "fail", expected,
                     "Элемент отсутствует в полном дереве. The feature is absent from the "
                     "complete tree.", **options)


def components_evidence(requirement: Requirement, value: dict, *, name: str,
                        path: str | None = None, revision: int, document_identity: str,
                        tool_call_id: str | None = None,
                        contract_digest: str = "") -> EvidenceRecord:
    options = {"revision": revision, "document_identity": document_identity,
               "tool_call_id": tool_call_id, "contract_digest": contract_digest,
               "suffix": "components"}
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        return _evidence(requirement, "unknown", None,
                         "Не задано имя компонента. The component name is missing.", **options)
    if path is not None and (not isinstance(path, str) or not path.strip()):
        return _evidence(requirement, "unknown", None,
                         "Некорректный путь компонента. Invalid component path.", **options)
    if not isinstance(value, dict) or not isinstance(value.get("components"), list):
        return _evidence(requirement, "unknown", None,
                         "Некорректный ответ get_components. Invalid get_components result.",
                         **options)
    components = value["components"]
    for item in components:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not isinstance(item.get("path"), str) or not item["path"].strip()
                or type(item.get("suppressed", False)) is not bool):
            return _evidence(requirement, "unknown", None,
                             "Некорректный элемент списка компонентов. "
                             "Invalid component entry.", **options)
    matching = [item for item in components
                if item["name"] == name and (path is None or Path(item["path"]) == Path(path))]
    if any(not item.get("suppressed", False) for item in matching):
        return _evidence(
            requirement, "pass", name,
            f"Компонент {name!r} найден" + (f" по пути {path!r}" if path else "")
            + " и не подавлен. The component is present and not suppressed.", **options)
    if matching:
        return _evidence(requirement, "fail", name,
                         f"Компонент {name!r} найден, но подавлен. The component is "
                         "suppressed.", **options)
    return _evidence(requirement, "fail", name,
                     f"Компонент {name!r} не найден" + (f" по пути {path!r}" if path else "")
                     + ". The component is missing.", **options)


def requested_file_evidence(requirement: Requirement, value: dict, *, requested_path: str,
                            signature_before, signature_after, revision: int,
                            document_identity: str,
                            tool_call_id: str | None = None,
                            contract_digest: str = "") -> EvidenceRecord:
    """Verify a requested output file with the existing agent save/export rules.

    The signature is the ``(size, mtime_ns, ctime_ns)`` tuple produced by the
    caller's existing file reader; this module performs no filesystem access.
    """
    options = {"revision": revision, "document_identity": document_identity,
               "tool_call_id": tool_call_id, "contract_digest": contract_digest,
               "suffix": "file"}
    if not isinstance(requested_path, str) or not requested_path.strip():
        return _evidence(requirement, "unknown", None,
                         "Не задан запрошенный путь файла. The requested file path is missing.",
                         **options)
    if not isinstance(value, dict):
        return _evidence(requirement, "unknown", None,
                         "Некорректный ответ сохранения или экспорта. Invalid save or export "
                         "result.", **options)
    target = value.get("saved") or value.get("exported")
    if not isinstance(target, str) or not target.strip():
        return _evidence(requirement, "unknown", None,
                         "Ответ не содержит пути созданного файла. The result has no created "
                         "file path.", **options)
    before = _normalized_signature(signature_before)
    after = _normalized_signature(signature_after)
    if before is _INVALID_SIGNATURE or after is _INVALID_SIGNATURE:
        return _evidence(requirement, "unknown", target,
                         "Некорректная подпись файла. Invalid file signature.", **options)
    if Path(target) != Path(requested_path):
        return _evidence(
            requirement, "fail", target,
            f"Создан файл {target!r}, а запрошен {requested_path!r}. The created file "
            f"{target!r} differs from the requested {requested_path!r}.", **options)
    if after is None:
        return _evidence(requirement, "fail", target,
                         "Запрошенный файл не найден. The requested file is missing.", **options)
    if after[0] <= 0:
        return _evidence(requirement, "fail", target,
                         "Запрошенный файл пуст. The requested file is empty.", **options)
    if before is not None and after == before:
        return _evidence(requirement, "fail", target,
                         "Файл не изменился после сохранения или экспорта. The file did not "
                         "change after the save or export.", **options)
    return _evidence(requirement, "pass", target,
                     f"Файл {target!r} создан или обновлён и не пуст; размер {after[0]} байт. "
                     "The file exists, is non-empty, and changed.", **options)
