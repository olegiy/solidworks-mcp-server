"""One sequential CAD run, explicit confirmation, and independent readback."""

import asyncio
import json
import logging
import math
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator
from referencing import Registry

from cadnext.attachments import AttachmentError, normalize_attachments
from cadnext.provider import ProviderError

_LOG = logging.getLogger(__name__)
_TERMINAL = {"completed", "stopped", "failed", "uncertain", "limit_reached", "rejected"}
_READ_ONLY = {"connect_solidworks", "list_open_documents", "screenshot", "get_feature_tree",
              "get_dimension", "get_mass_properties", "get_sheet_metal_info", "get_components"}
_FEATURE_RESULTS = {"create_sketch": "sketch", "create_base_flange": "feature",
                    "cut_through": "feature", "add_mate": "mate"}
_NEW_DOCUMENT = {"create_part", "create_assembly"}
_TREE_FEEDBACK = {"sketch_rectangle", "sketch_circle", "sketch_line", "close_sketch", "rebuild"}
_TREE_LIMIT = 1000
_PYTHON_REPAIR_LIMIT = 2
_SYSTEM = (
    "Ты CAD-агент для SolidWorks. Отвечай по-русски. Используй только доступные инструменты, "
    "их фактические результаты и единицы мм/градусы. Выполняй действия последовательно. "
    "Не повторяй неудачные изменения и не делай автоматический undo. "
    "Если execute_python вернул известную CAD_ERROR, агент может запросить не более двух "
    "исправлений: сначала инспектируй состояние, затем выполняй только продолжение; "
    "неизвестный результат COM не повторяй. "
    "Разрешениями на выполнение Python управляет приложение. "
    "Не проси разрешение на Python обычным сообщением: вызывай execute_python, "
    "а приложение при необходимости само покажет подтверждение. "
    "Запрос пользователя на построение уже разрешает выполнять описанные CAD-действия. "
    "Прочтение чертежа кратко опиши и продолжай; уточняй только недостающие или "
    "противоречивые данные, влияющие на геометрию. "
    "Успех вызова не доказывает корректность геометрии: учитывай поле verification. "
    "Если результат требует проверки, прямо сообщи об этом пользователю."
)


class AgentBusy(RuntimeError):
    pass


class _EndRun(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class _PythonRepair(Exception):
    def __init__(self, call_id, result):
        self.call_id = call_id
        self.result = result


@dataclass
class Run:
    session_id: str
    profile_id: str
    messages: list[dict]
    max_calls: int = 80
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "queued"
    events: list[dict] = field(default_factory=list)
    stop_requested: bool = False
    confirmation: dict | None = None
    call_count: int = 0
    checks: dict = field(default_factory=dict)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _answer: asyncio.Future | None = field(default=None, repr=False)
    _pending: dict = field(default_factory=dict, repr=False)
    _inflight: bool = field(default=False, repr=False)
    _images: list[dict] = field(default_factory=list, repr=False)
    repair_attempts: int = 0
    repairing: bool = False
    _repair_call_id: str | None = field(default=None, repr=False)

    @property
    def done(self):
        return self.status in _TERMINAL

    @property
    def verification(self):
        states = {check["status"] for check in self.checks.values()}
        if "failed" in states:
            return "failed"
        if states & {"unverified", "pending"}:
            return "unverified"
        return "verified" if "verified" in states else "not_required"

    def snapshot(self) -> dict:
        return deepcopy({"id": self.id, "session_id": self.session_id,
                         "profile_id": self.profile_id, "status": self.status,
                         "events": self.events, "stop_requested": self.stop_requested,
                         "confirmation": self.confirmation, "call_count": self.call_count,
                         "max_calls": self.max_calls, "verification": self.verification,
                         "checks": self.checks, "messages": self.messages,
                         "repair_attempts": self.repair_attempts, "repairing": self.repairing})

    def _emit(self, event_type: str, **data):
        self.events.append({"seq": len(self.events) + 1, "type": event_type,
                            "at": time.time(), "data": deepcopy(data)})
        self._changed.set()

    def _status(self, status, message):
        self.status = status
        self._emit("status", status=status, message=message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _invalid_constant(value):
    raise ValueError("Недопустимая константа JSON.")


def _file_signature(path: str):
    try:
        file = Path(path)
        stat = file.stat()
        return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) if file.is_file() else None
    except FileNotFoundError:
        return None


class Agent:
    def __init__(self, mcp, *, settings=None, max_history_bytes=8 * 1024 * 1024,
                 max_catalog_bytes=256 * 1024):
        self.mcp = mcp
        self.settings = settings
        self.runs: dict[str, Run] = {}
        self._history: dict[str, list[dict]] = {}
        self._python_approved: set[tuple] = set()
        self._active: Run | None = None
        self._uncertain = False
        self._closed = False
        self.max_history_bytes = max_history_bytes
        self.max_catalog_bytes = max_catalog_bytes

    def start(self, *, session_id: str, provider, message: str, attachments=None, max_calls=80) -> Run:
        if self._closed:
            raise RuntimeError("Агент завершает работу.")
        if self._uncertain or getattr(self.mcp, "uncertain", False):
            raise RuntimeError("Исход CAD-операции неизвестен. Проверьте SolidWorks и перезапустите приложение.")
        if self._active is not None and not self._active.done:
            raise AgentBusy("Уже выполняется CAD-запрос.")
        if (not isinstance(session_id, str) or not session_id.strip()
                or not isinstance(message, str) or (not message.strip() and not attachments)
                or type(max_calls) is not int or not 1 <= max_calls <= 1000):
            raise ValueError("Укажите диалог, текст запроса и лимит от 1 до 1000 вызовов.")
        attachments = normalize_attachments([] if attachments is None else attachments)
        if attachments and not provider.profile.vision:
            raise AttachmentError("Для отправки изображений включите Vision в настройках подключения.")
        provider.privacy.bind_session(session_id)
        profile = provider.profile
        provider.privacy.bind_profile(profile.id, base_url=profile.base_url, model=profile.model)
        provider.privacy.grant(session_id=session_id, profile_id=profile.id, images=profile.vision)
        messages = deepcopy(self._history.get(session_id, [{"role": "system", "content": _SYSTEM}]))
        content = message
        if attachments:
            content = ([{"type": "text", "text": message}] if message.strip() else []) + [
                {"type": "image_url", "image_url": {
                    "url": f'data:{image["mimeType"]};base64,{image["data"]}'}} for image in attachments]
        messages.append({"role": "user", "content": content})
        if len(_json(messages).encode("utf-8")) > self.max_history_bytes:
            raise AttachmentError("История превысила лимит контекста. Начните новый диалог.")
        run = Run(session_id=session_id, profile_id=profile.id, messages=messages, max_calls=max_calls)
        self.runs[run.id] = run
        self._active = run
        run._emit("message", role="user", content=message, **({"attachments": attachments} if attachments else {}))
        run._task = asyncio.create_task(self._run(run, provider))
        return run

    def get(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise KeyError("Запрос не найден.")
        return self.runs[run_id]

    def stop(self, run_id: str):
        run = self.get(run_id)
        if run.done or run.stop_requested:
            return
        run.stop_requested = True
        run._status("stopping", "Остановка: ждём исход уже отправленного COM-вызова."
                    if run._inflight else "Остановка: следующие действия запрещены.")
        if run._answer is not None and not run._answer.done():
            run._answer.set_result((False, False))

    def confirm(self, run_id: str, confirmation_id: str, approved: bool, *, images: bool = False):
        run = self.get(run_id)
        if (type(approved) is not bool or type(images) is not bool or run.stop_requested
                or run.confirmation is None or run.confirmation["id"] != confirmation_id
                or run._answer is None or run._answer.done()):
            raise ValueError("Подтверждение отсутствует, устарело или относится к другому вызову.")
        run._answer.set_result((approved, images))

    async def wait(self, run_id: str) -> Run:
        run = self.get(run_id)
        await asyncio.shield(run._task)
        return run

    async def aclose(self):
        self._closed = True
        if self._active is not None:
            self.stop(self._active.id)
            await self.wait(self._active.id)

    async def events(self, run_id: str, after: int = 0):
        run = self.get(run_id)
        if type(after) is not int or after < 0 or after > len(run.events):
            raise ValueError("Некорректный номер события.")
        try:
            while True:
                run._changed.clear()
                for event in run.events[after:]:
                    after = event["seq"]
                    yield deepcopy(event)
                if after < len(run.events):
                    continue
                if run.done:
                    break
                await run._changed.wait()
        finally:
            self.stop(run_id)

    @staticmethod
    def _guard(run):
        if run.stop_requested:
            raise _EndRun("stopped", "Запрос остановлен. Уже выполненные действия не отменены.")

    @staticmethod
    def _budget(run, count=1):
        if run.call_count + count > run.max_calls:
            raise _EndRun("limit_reached", "Лимит вызовов исчерпан; новые действия не выполнялись.")

    async def _confirmation(self, run, kind, **data):
        self._guard(run)
        run._answer = asyncio.get_running_loop().create_future()
        run.confirmation = {"id": uuid4().hex, "kind": kind, **deepcopy(data)}
        run._status("awaiting_confirmation", "Требуется подтверждение пользователя.")
        run._emit("confirmation", **run.confirmation)
        try:
            approved, images = await run._answer
            self._guard(run)
            if not approved:
                raise _EndRun("rejected", "Пользователь отклонил действие; оно не выполнялось.")
            run._status("running", "Подтверждение получено.")
            return images
        finally:
            run.confirmation = None
            run._answer = None

    async def _model(self, run, provider, tools):
        self._guard(run)
        # Refresh the application-owned instruction even for existing dialogue history.
        prompt = _SYSTEM
        if self.settings is not None:
            prompt += (" Постоянное разрешение Python уже включено. Пользователь выбрал всегда доверять "
                       "выполнению кода; повторного подтверждения запуска не требуется."
                       if self.settings.python_execution_allowed else
                       " Разрешение Python ещё не выдано. Если нужен код, вызывай execute_python: "
                       "первое подтверждение покажет интерфейс приложения.")
        run.messages[0] = {"role": "system", "content": prompt}
        if len(_json(run.messages).encode("utf-8")) > self.max_history_bytes:
            raise _EndRun("failed", "История превысила лимит контекста. Начните новый диалог.")
        number = 1 + sum(event["type"] == "model_start" for event in run.events)
        run._emit("model_start", number=number)
        outcome = "error"
        try:
            result = await provider.complete(deepcopy(run.messages), deepcopy(tools))
            outcome = "received"
        finally:
            run._emit("model_end", number=number, outcome=outcome)
        self._guard(run)
        return result

    @staticmethod
    def _catalog(tools):
        catalog = {}
        for tool in tools:
            function = tool["function"]
            name = function["name"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in catalog:
                raise ValueError("Некорректный каталог инструментов.")
            schema = function["parameters"]
            Draft202012Validator.check_schema(schema)
            catalog[name] = Draft202012Validator(schema, registry=Registry())
        return catalog

    @staticmethod
    def _prepare(response, provider, catalog, used_ids):
        content = response.get("content")
        calls = response.get("tool_calls") or []
        if content is not None and not isinstance(content, str):
            raise ValueError
        if not isinstance(calls, list) or (not calls and content is None):
            raise ValueError
        ids, prepared = set(used_ids), []
        for call in calls:
            call_id, function = call["id"], call["function"]
            name = function["name"]
            if (not isinstance(call_id, str) or not call_id or call_id in ids
                    or call.get("type") != "function" or name not in catalog):
                raise ValueError
            ids.add(call_id)
            arguments = provider.privacy.restore(json.loads(function["arguments"],
                                                            parse_constant=_invalid_constant))
            _json(arguments)
            if not isinstance(arguments, dict) or not catalog[name].is_valid(arguments):
                raise ValueError
            prepared.append((call_id, name, arguments))
        message = {"role": "assistant", "content": provider.privacy.restore(content)}
        if prepared:
            message["tool_calls"] = [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": _json(arguments)}} for call_id, name, arguments in prepared]
        return message, prepared

    async def _call(self, run, catalog, name, arguments, call_id, *, verification=False):
        self._guard(run)
        self._budget(run)
        if name not in catalog or not catalog[name].is_valid(arguments):
            raise _EndRun("failed", "В каталоге отсутствует необходимый инструмент проверки.")
        run.call_count += 1
        run._emit("tool_start", tool_call_id=call_id, name=name, arguments=arguments,
                  verification=verification)
        started = time.perf_counter()
        run._inflight = True
        try:
            result = deepcopy(await self.mcp.call(name, deepcopy(arguments)))
            if (not isinstance(result, dict)
                    or not {"ok", "value", "images", "error", "uncertain"} <= result.keys()
                    or type(result["ok"]) is not bool or type(result["uncertain"]) is not bool
                    or not isinstance(result["images"], list)
                    or any(not isinstance(image, dict) or not isinstance(image.get("data"), str)
                           or not isinstance(image.get("mimeType"), str) for image in result["images"])):
                raise ValueError
            _json(result)
        except Exception:
            result = {"ok": False, "value": None, "images": [], "uncertain": True,
                      "error": "Связь MCP прервана; результат CAD-операции неизвестен."}
        finally:
            run._inflight = False
        value = result.get("value")
        nested_error = isinstance(value, dict) and value.get("status") == "error"
        uncertain = bool(result.get("uncertain")) or (isinstance(value, dict)
            and value.get("error_code") == "CAD_OPERATION_UNCERTAIN")
        result["ok"] = result.get("ok") is True and not nested_error and not uncertain
        result["uncertain"] = uncertain
        self._uncertain |= uncertain
        run._emit("tool_result", tool_call_id=call_id, name=name,
                  result={key: item for key, item in result.items() if key != "images"},
                  verification=verification)
        _LOG.info("CAD tool=%s duration=%.3f status=%s", name, time.perf_counter() - started,
                  "uncertain" if uncertain else "ok" if result["ok"] else "error")
        return result

    @staticmethod
    def _success(result):
        if result.get("uncertain"):
            raise _EndRun("uncertain", "Результат CAD-операции неизвестен. Проверьте SolidWorks; повтор запрещён.")
        if not result.get("ok"):
            raise _EndRun("failed", "CAD-инструмент вернул ошибку. Запрос остановлен без повторов.")

    async def _read(self, run, catalog, name, arguments, owner, evidence):
        if name == "get_feature_tree":
            arguments = {"limit": _TREE_LIMIT, **arguments}
        result = await self._call(run, catalog, name, arguments,
                                  f"{owner}:check:{len(evidence) + 1}", verification=True)
        evidence.append({"name": name, "value": result["value"]})
        self._success(result)
        self._guard(run)
        return result["value"]

    @staticmethod
    def _check(condition):
        if not condition:
            raise _EndRun("failed", "Независимая проверка не подтвердила изменение. Повтора не будет.")

    @staticmethod
    def _number_matches(actual, expected, *, abs_tol=1e-6):
        return (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=1e-7, abs_tol=abs_tol))

    @staticmethod
    def _verification_calls(name, arguments):
        if name == "set_dimension":
            return [("get_dimension", {"name": arguments["name"]})]
        if name in _FEATURE_RESULTS:
            return [("get_feature_tree", {})] * 2 + (
                [("get_sheet_metal_info", {})] if name == "create_base_flange" else [])
        if name in _NEW_DOCUMENT:
            return [("list_open_documents", {})] * 2 + [("get_feature_tree", {})]
        if name == "add_component":
            return [("get_components", {})] * 2 + [("get_feature_tree", {})]
        if name == "save_document" and not arguments.get("save_as_path"):
            return [("connect_solidworks", {}), ("list_open_documents", {})]
        if name in _TREE_FEEDBACK:
            return [("get_feature_tree", {})]
        return []

    async def _verify(self, run, catalog, call_id, name, arguments, value, before, path,
                      file_before, evidence):
        if name == "set_dimension":
            readback = await self._read(run, catalog, "get_dimension", {"name": arguments["name"]},
                                        call_id, evidence)
            actual = readback.get("value", readback.get("value_mm", readback.get("value_deg")))
            self._check(self._number_matches(actual, arguments["value_mm"])
                        and readback.get("name") == arguments["name"]
                        and readback.get("unit") in {"mm", "deg"}
                        and readback["unit"] == value.get("unit"))
        elif name in _FEATURE_RESULTS:
            tree = await self._read(run, catalog, "get_feature_tree", {}, call_id, evidence)
            created = value.get(_FEATURE_RESULTS[name])
            self._check(created and created not in before
                        and created in {item["name"] for item in tree["features"]})
            if name == "create_base_flange":
                info = await self._read(run, catalog, "get_sheet_metal_info", {}, call_id, evidence)
                self._check(self._number_matches(info.get("thickness_mm"), arguments["thickness_mm"],
                                                abs_tol=5.0001e-5)
                    and self._number_matches(info.get("bend_radius_mm"), arguments.get("bend_radius_mm", 1),
                                             abs_tol=5.0001e-5))
        elif name in _NEW_DOCUMENT:
            documents = await self._read(run, catalog, "list_open_documents", {}, call_id, evidence)
            created = value.get("created")
            self._check(created and created not in before
                        and created in {item["title"] for item in documents["documents"]})
            tree = await self._read(run, catalog, "get_feature_tree", {}, call_id, evidence)
            self._check(isinstance(tree["features"], list))
        elif name == "add_component":
            components = await self._read(run, catalog, "get_components", {}, call_id, evidence)
            created = value.get("component")
            self._check(created and created not in before and any(
                item["name"] == created and Path(item["path"]) == Path(arguments["path"])
                and not item.get("suppressed", False) for item in components["components"]))
            tree = await self._read(run, catalog, "get_feature_tree", {}, call_id, evidence)
            self._check(created in {item["name"] for item in tree["features"]})
        elif name in {"save_document", "export_flat_pattern_dxf"}:
            target = value.get("saved") or value.get("exported")
            self._check(target and path and Path(target) == Path(path))
            signature = await asyncio.to_thread(_file_signature, target)
            self._guard(run)
            self._check(signature and signature[0] > 0 and signature != file_before)
            evidence.append({"name": "file", "path": target, "size": signature[0]})
        else:
            if name in _TREE_FEEDBACK:
                await self._read(run, catalog, "get_feature_tree", {}, call_id, evidence)
            return False
        return True

    async def _execute(self, run, provider, catalog, call_id, name, arguments):
        checks = self._verification_calls(name, arguments)
        self._budget(run, 1 + len(checks))
        for check_name, check_arguments in checks:
            if check_name == "get_feature_tree":
                check_arguments = {"limit": _TREE_LIMIT, **check_arguments}
            if check_name not in catalog or not catalog[check_name].is_valid(check_arguments):
                raise _EndRun("failed", "В каталоге отсутствует необходимый инструмент проверки.")
        evidence = []
        before = None
        if name == "execute_python":
            scope = (run.session_id, provider.profile)
            approved = (self.settings.python_execution_allowed if self.settings is not None
                        else scope in self._python_approved)
            if not approved:
                await self._confirmation(run, "python", tool_call_id=call_id, code=arguments["code"],
                    message="Разрешить выполнение Python-кода постоянно? Разрешение действует "
                            "для всех диалогов и подключений, включая перезапуск приложения."
                            if self.settings is not None else "Разрешить Python-код в этом диалоге и подключении?")
                if self.settings is not None:
                    self.settings.set_python_execution_allowed(True)
                else:
                    self._python_approved.add(scope)
        if name in _FEATURE_RESULTS:
            tree = await self._read(run, catalog, "get_feature_tree", {}, call_id, evidence)
            if len(tree["features"]) >= _TREE_LIMIT:
                raise _EndRun("failed", "Дерево может быть усечено; создание не выполнялось.")
            before = {item["name"] for item in tree["features"]}
        elif name in _NEW_DOCUMENT:
            documents = await self._read(run, catalog, "list_open_documents", {}, call_id, evidence)
            before = {item["title"] for item in documents["documents"]}
        elif name == "add_component":
            components = await self._read(run, catalog, "get_components", {}, call_id, evidence)
            before = {item["name"] for item in components["components"]}
        path = arguments.get("save_as_path") or arguments.get("output_path")
        if name == "save_document" and not path:
            connection = await self._read(run, catalog, "connect_solidworks", {}, call_id, evidence)
            documents = await self._read(run, catalog, "list_open_documents", {}, call_id, evidence)
            matches = [item for item in documents["documents"]
                       if item["title"] == connection.get("active_document")]
            self._check(len(matches) == 1 and matches[0].get("path"))
            path = matches[0]["path"]
        file_before = await asyncio.to_thread(_file_signature, path) if path else None
        self._guard(run)
        mutating = name not in _READ_ONLY
        if mutating:
            run.checks[call_id] = {"status": "pending", "message": "Изменение ещё не проверено."}
        message = content = None
        try:
            result = await self._call(run, catalog, name, arguments, call_id)
            content = {key: value for key, value in result.items() if key != "images"}
            message = {"role": "tool", "tool_call_id": call_id, "content": _json(content)}
            run.messages.append(message)
            run._pending.pop(call_id, None)
            images = result.get("images", [])
            for image in images:
                run._emit("image", tool_call_id=call_id, **image)
            run._images.extend(deepcopy(images))
            value = result.get("value")
            known_python_error = (isinstance(value, dict) and value.get("error_code") == "CAD_ERROR")
            if (name == "execute_python" and result.get("ok") is False and
                    known_python_error and not result.get("uncertain")):
                raise _PythonRepair(call_id, result)
            self._success(result)
            self._guard(run)
            if mutating:
                verified = await self._verify(run, catalog, call_id, name, arguments,
                    result["value"], before, path, file_before, evidence)
                run.checks[call_id] = {"status": "verified" if verified else "unverified",
                    "message": "Результат подтверждён." if verified else "Выполнено, результат требует проверки.",
                    "evidence": evidence}
                if name == "execute_python" and run.repairing:
                    previous = run.checks.get(run._repair_call_id)
                    if previous and previous.get("status") == "failed":
                        previous["status"] = "repaired"
                        previous["message"] = "Ошибка исправлена продолжением; исходный вызов не повторялся."
                    run.repairing = False
                    run._repair_call_id = None
        except _EndRun as error:
            if mutating:
                run.checks[call_id] = {"status": "failed" if error.status == "failed" else "unverified",
                                       "message": error.message, "evidence": evidence}
            raise
        except Exception:
            if mutating:
                run.checks[call_id] = {"status": "failed", "message": "Проверка результата завершилась ошибкой.",
                                       "evidence": evidence}
            raise
        finally:
            if mutating and message is not None:
                content["verification"] = deepcopy(run.checks[call_id])
                message["content"] = _json(content)
                run._emit("tool_result", tool_call_id=call_id, name=name, result=content, verification=False)

    @staticmethod
    def _close_pending(run, *, reason="Вызов не выполнялся: запрос остановлен."):
        for call_id in run._pending:
            run.messages.append({"role": "tool", "tool_call_id": call_id,
                "content": _json({"ok": False, "error": reason,
                                  "uncertain": False})})
        run._pending.clear()

    async def _prepare_python_repair(self, run, error):
        if run.repair_attempts >= _PYTHON_REPAIR_LIMIT:
            raise _EndRun("failed", "Исправление Python-кода не удалось после двух попыток; повтор остановлен.")
        run.repair_attempts += 1
        run.repairing = True
        run._repair_call_id = error.call_id
        value = error.result.get("value") if isinstance(error.result, dict) else None
        details = {key: value.get(key) for key in ("error", "error_code", "python_line", "error_type")
                   if isinstance(value, dict) and value.get(key) is not None}
        message = (
            f"Известная ошибка execute_python. Подготовь исправление, попытка "
            f"{run.repair_attempts} из {_PYTHON_REPAIR_LIMIT}. "
            "Не повторяй уже выполненные шаги и не запускай исходный скрипт целиком. "
            "Сначала используй доступные read-only инструменты или безопасную инспекцию, "
            "затем вызови execute_python только с минимальным продолжением, исправляющим ошибку. "
            "Если исправление невозможно, сообщи об этом без нового изменения. "
            f"Диагностика: {_json(details)}"
        )
        run._status("repairing", f"Анализируем ошибку Python; попытка {run.repair_attempts} из {_PYTHON_REPAIR_LIMIT}.")
        run._emit("repair", attempt=run.repair_attempts, call_id=error.call_id,
                  message=f"Подготовка исправления Python, попытка {run.repair_attempts} из {_PYTHON_REPAIR_LIMIT}.")
        self._close_pending(run, reason="Вызов не выполнялся: предыдущий скрипт завершился ошибкой.")
        self._flush_images(run)
        run.messages.append({"role": "user", "content": message})

    async def _run(self, run, provider):
        try:
            self._guard(run)
            run._status("running", "Запрос выполняется.")
            tools = await self.mcp.tools()
            if len(_json(tools).encode("utf-8")) > self.max_catalog_bytes:
                raise _EndRun("failed", "Каталог инструментов превысил допустимый размер.")
            catalog = self._catalog(tools)
            while True:
                response = await self._model(run, provider, tools)
                if len(_json(response).encode("utf-8")) > self.max_history_bytes:
                    raise _EndRun("failed", "Ответ модели превысил допустимый размер контекста.")
                used_ids = {call["id"] for previous in run.messages
                            for call in previous.get("tool_calls", [])}
                message, calls = self._prepare(response, provider, catalog, used_ids)
                if len(_json([*run.messages, message]).encode("utf-8")) > self.max_history_bytes:
                    raise _EndRun("failed", "История превысила лимит контекста. Начните новый диалог.")
                run.messages.append(message)
                if message["content"]:
                    run._emit("message", role="assistant", content=message["content"])
                if not calls:
                    if run.repairing:
                        raise _EndRun("failed", "Модель не предложила исправление Python-кода; запрос остановлен.")
                    run._status("completed", "Запрос завершён. Результат требует проверки."
                                if run.verification == "unverified" else "Запрос завершён.")
                    break
                run._pending = {call_id: name for call_id, name, arguments in calls}
                repair_requested = False
                for call_id, name, arguments in calls:
                    self._guard(run)
                    try:
                        await self._execute(run, provider, catalog, call_id, name, arguments)
                    except _PythonRepair as error:
                        self._guard(run)
                        await self._prepare_python_repair(run, error)
                        repair_requested = True
                        break
                if repair_requested:
                    continue
                self._flush_images(run)
        except _EndRun as error:
            if error.status in {"failed", "uncertain", "limit_reached"}:
                run._emit("error", message=error.message, uncertain=error.status == "uncertain")
            run._status(error.status, error.message)
        except ProviderError as error:
            run._emit("error", message=str(error), uncertain=False)
            run._status("failed", "Ошибка API модели; CAD-вызовы не повторялись.")
        except Exception:
            run._emit("error", message="Некорректные данные модели, каталога или проверки; запрос остановлен.",
                      uncertain=False)
            run._status("failed", "Запрос остановлен из-за ошибки.")
        finally:
            self._close_pending(run)
            self._flush_images(run)
            run.repairing = False
            run._repair_call_id = None
            self._history[run.session_id] = deepcopy(run.messages)
            run._changed.set()

    @staticmethod
    def _flush_images(run):
        if run._images:
            run.messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {
                "url": f'data:{image["mimeType"]};base64,{image["data"]}'}} for image in run._images]})
            run._images.clear()
