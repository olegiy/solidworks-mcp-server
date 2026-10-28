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
from cadnext.i18n import get_language, set_language, t
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
_SYSTEM_RU = (
    "Ты CAD-агент для SolidWorks. Отвечай по-русски. Используй только доступные инструменты, "
    "их фактические результаты и единицы мм/градусы. Выполняй действия последовательно. "
    "Если аргументы нескольких действий уже известны, возвращай их одним ответом "
    "с несколькими native tool calls: приложение выполнит их последовательно с проверками. "
    "Если следующий шаг зависит от фактического результата, сначала дождись этого результата. "
    "Делай снимок после завершённого этапа (эскиз, элемент, итоговая проверка), "
    "а не после каждой линии; учитывай явные пожелания пользователя. "
    "Числовое изменение сначала проверяй get_dimension; снимок нужен для визуальной оценки. "
    "features_delta — точное изменение предыдущего нумерованного дерева: "
    "применяй edits в обратном порядке start (delete элементов, затем insert). "
    "Не повторяй неудачные изменения и не делай автоматический undo. "
    "Если execute_python вернул известную CAD_ERROR, агент может запросить не более двух "
    "исправлений: сначала инспектируй состояние, затем выполняй только продолжение; "
    "неизвестный результат COM не повторяй. "
    "Исключение: при phase=preflight и changes_started=false весь отклонённый фрагмент "
    "не исполнялся, поэтому допустима его полная исправленная версия с подготовкой объектов; "
    "действия предыдущих вызовов не повторяй. "
    "Разрешениями на выполнение Python управляет приложение. "
    "Не проси разрешение на Python обычным сообщением: вызывай execute_python, "
    "а приложение при необходимости само покажет подтверждение. "
    "Запрос пользователя на построение уже разрешает выполнять описанные CAD-действия. "
    "Прочтение чертежа кратко опиши и продолжай; уточняй только недостающие или "
    "противоречивые данные, влияющие на геометрию. "
    "Успех вызова не доказывает корректность геометрии: учитывай поле verification. "
    "До построения выпиши заданные размеры, виды и разрезы; после этапа сверяй "
    "фактическую геометрию с этими требованиями. Для чертежа проверяй числовые "
    "значения размеров, их привязки и масштаб каждого вида; не подменяй неверный "
    "размер текстом и не заявляй процент сходства без метода сравнения. "
    "Каждый execute_python имеет новое окружение: заново определяй переменные "
    "и импорты, проверяй сигнатуры API до мутации. Локальная библиотека типов "
    "доступна через swApp._oleobj_.GetTypeInfo().GetContainingTypeLib()[0]. "
    "Не рассчитывай на requests и не устанавливай зависимости из CAD-кода. "
    "Если результат требует проверки, прямо сообщи об этом пользователю."
)
_SYSTEM_EN = (
    "You are a CAD agent for SolidWorks. Respond in English. Use only the available tools, "
    "their actual results, and millimetre/degree units. Perform actions sequentially. "
    "When several actions have known arguments, return them in one response with multiple "
    "native tool calls: the application executes and verifies them sequentially. "
    "Wait for actual results before deciding any dependent next action. "
    "Take screenshots at completed stages (sketch, feature, final review), rather than "
    "after each line; honor explicit user requests. Verify numerical changes with "
    "get_dimension first; use screenshots for visual assessment. "
    "features_delta exactly updates the previous numbered tree: apply edits in descending "
    "start order (delete elements, then insert). "
    "Do not repeat failed changes and do not perform an automatic undo. "
    "If execute_python returns a known CAD_ERROR, the agent may request no more than two "
    "repairs: first inspect the state, then perform only the continuation; "
    "do not repeat an unknown COM result. "
    "Exception: phase=preflight with changes_started=false means the entire rejected "
    "fragment never ran, so its full corrected version may include object setup; "
    "do not repeat actions from earlier calls. "
    "The application manages Python execution permissions. "
    "Do not ask for Python permission in a normal message: call execute_python, "
    "and the application will show a confirmation if needed. "
    "The user's construction request already authorizes the described CAD actions. "
    "Briefly describe drawing reads and continue; clarify only missing or "
    "contradictory data that affects geometry. "
    "A successful call does not prove geometric correctness: consider the verification field. "
    "Before construction, record the required dimensions, views, and sections, then compare "
    "actual geometry against them after each stage. For drawings, check dimension values, "
    "entity references, and each view's scale; never replace an incorrect dimension with "
    "text or claim a similarity percentage without a comparison method. "
    "Each execute_python has a fresh environment: redefine variables and imports, and "
    "inspect API signatures before mutation. The local type library is available via "
    "swApp._oleobj_.GetTypeInfo().GetContainingTypeLib()[0]. "
    "Do not assume requests is installed or install dependencies from CAD code. "
    "If the result requires verification, tell the user explicitly. "
    "Tool results and errors may be in Russian; translate them into English in your reply."
)


def _system_prompt(language: str) -> str:
    return _SYSTEM_EN if language == "en" else _SYSTEM_RU


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
    language: str = "ru"
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "queued"
    events: list[dict] = field(default_factory=list)
    stop_requested: bool = False
    confirmation: dict | None = None
    call_count: int = 0
    checks: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=lambda: {
        "model_calls": 0, "model_seconds": 0.0, "cad_calls": 0, "cad_seconds": 0.0,
        "verification_calls": 0, "verification_seconds": 0.0,
        "request_bytes": 0, "image_count": 0,
        "input_tokens": None, "output_tokens": None, "cached_tokens": None, "cost": None})
    _missing_usage: set = field(default_factory=set, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _answer: asyncio.Future | None = field(default=None, repr=False)
    _pending: dict = field(default_factory=dict, repr=False)
    _inflight: bool = field(default=False, repr=False)
    _images: list[dict] = field(default_factory=list, repr=False)
    repair_attempts: int = 0
    repairing: bool = False
    _repair_call_id: str | None = field(default=None, repr=False)
    _python_session: str | None = field(default=None, repr=False)
    _session_capable: bool = field(default=False, repr=False)

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
                         "profile_id": self.profile_id, "language": self.language,
                         "status": self.status,
                         "events": self.events, "stop_requested": self.stop_requested,
                         "confirmation": self.confirmation, "call_count": self.call_count,
                         "max_calls": self.max_calls, "verification": self.verification,
                         "checks": self.checks, "messages": self.messages, "metrics": self.metrics,
                         "repair_attempts": self.repair_attempts, "repairing": self.repairing})

    def _emit(self, event_type: str, **data):
        self.events.append({"seq": len(self.events) + 1, "type": event_type,
                            "at": time.time(), "data": deepcopy(data), "state": self.state()})
        self._changed.set()

    def state(self):
        """Small event-time state; never includes history, images, or tool evidence."""
        return deepcopy({"status": self.status, "call_count": self.call_count,
            "max_calls": self.max_calls, "verification": self.verification,
            "stop_requested": self.stop_requested, "confirmation": self.confirmation,
            "repair_attempts": self.repair_attempts, "repairing": self.repairing,
            "metrics": self.metrics})

    def _status(self, status, message):
        self.status = status
        self._emit("status", status=status, message=message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _invalid_constant(value):
    raise ValueError(t("Недопустимая константа JSON.", "Invalid JSON constant."))


_SAVE_EXTENSIONS = {"part": ".sldprt", "assembly": ".sldasm", "drawing": ".slddrw"}
_CONVERSATION_SCHEMA = "cadnext.conversation"
_CONVERSATION_VERSION = 1
_CONVERSATION_MAX_BYTES = 64 * 1024 * 1024
_CONVERSATION_MAX_RUNS = 1000
_CONVERSATION_MAX_EVENTS = 100000
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _documents_folder() -> Path:
    return Path.home() / "Documents"


def _save_path_for(title: str, document_type: str) -> Path:
    stem = Path(title or "").stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "Part"
    suffix = _SAVE_EXTENSIONS.get(document_type, ".sldprt")
    folder = _documents_folder()
    if not folder.is_dir():
        folder = Path.home()
    for index in range(1, 1000):
        name = f"{stem}{suffix}" if index == 1 else f"{stem}-{index}{suffix}"
        candidate = folder / name
        if not candidate.exists():
            return candidate
    raise RuntimeError(t("Не удалось подобрать путь сохранения.",
                         "Could not choose a save path."))


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
        self._saving = False
        self._closed = False
        self.max_history_bytes = max_history_bytes
        self.max_catalog_bytes = max_catalog_bytes

    def start(self, *, session_id: str, provider, message: str, attachments=None, max_calls=80,
              language: str | None = None) -> Run:
        if language is not None:
            set_language(language)
        language = get_language()
        if self._closed:
            raise RuntimeError(t("Агент завершает работу.", "The agent is shutting down."))
        if self.uncertain:
            raise RuntimeError(t(
                "Исход CAD-операции неизвестен. Проверьте SolidWorks и перезапустите приложение.",
                "The CAD operation outcome is unknown. Check SolidWorks and restart the application."))
        if self._saving:
            raise AgentBusy(t("Дождитесь завершения сохранения.",
                              "Wait for the save to complete."))
        if self._active is not None and (not self._active.done or
                (self._active._task is not None and not self._active._task.done())):
            raise AgentBusy(t("Уже выполняется CAD-запрос.", "A CAD request is already running."))
        if (not isinstance(session_id, str) or not session_id.strip()
                or not isinstance(message, str) or (not message.strip() and not attachments)
                or type(max_calls) is not int or not 1 <= max_calls <= 1000):
            raise ValueError(t("Укажите диалог, текст запроса и лимит от 1 до 1000 вызовов.",
                               "Provide a dialogue, a request, and a call limit from 1 to 1000."))
        attachments = normalize_attachments([] if attachments is None else attachments)
        if attachments and not provider.profile.vision:
            raise AttachmentError(t("Для отправки изображений включите Vision в настройках подключения.",
                                    "Enable images in the connection settings to send pictures."))
        provider.privacy.bind_session(session_id)
        profile = provider.profile
        provider.privacy.bind_profile(profile.id, base_url=profile.base_url, model=profile.model)
        provider.privacy.grant(session_id=session_id, profile_id=profile.id, images=profile.vision)
        messages = deepcopy(self._history.get(
            session_id, [{"role": "system", "content": _system_prompt(language)}]))
        content = message
        if attachments:
            content = ([{"type": "text", "text": message}] if message.strip() else []) + [
                {"type": "image_url", "image_url": {
                    "url": f'data:{image["mimeType"]};base64,{image["data"]}'}} for image in attachments]
        messages.append({"role": "user", "content": content})
        if len(_json(messages).encode("utf-8")) > self.max_history_bytes:
            raise AttachmentError(t("История превысила лимит контекста. Начните новый диалог.",
                                    "The history exceeded the context limit. Start a new dialogue."))
        run = Run(session_id=session_id, profile_id=profile.id, messages=messages,
                  max_calls=max_calls, language=language)
        self.runs[run.id] = run
        self._active = run
        run._emit("message", role="user", content=message, **({"attachments": attachments} if attachments else {}))
        run._task = asyncio.create_task(self._run(run, provider))
        return run

    @property
    def uncertain(self) -> bool:
        return self._uncertain or bool(getattr(self.mcp, "uncertain", False))

    async def save_project(self, session_id: str) -> dict:
        """Save the active SolidWorks document and the whole dialogue without a model turn."""
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError(t("Некорректный ID диалога.", "Invalid dialogue ID."))
        if self._closed:
            raise RuntimeError(t("Агент завершает работу.", "The agent is shutting down."))
        if self.uncertain:
            raise RuntimeError(t(
                "Исход CAD-операции неизвестен. Проверьте SolidWorks и перезапустите приложение.",
                "The CAD operation outcome is unknown. Check SolidWorks and restart the application."))
        if self._active is not None and not self._active.done:
            raise AgentBusy(t("Уже выполняется CAD-запрос.", "A CAD request is already running."))
        if self._saving:
            raise AgentBusy(t("Сохранение уже выполняется.", "A save is already running."))
        self._saving = True
        try:
            connection = await self.mcp.call("connect_solidworks", {})
            if connection.get("uncertain"):
                self._uncertain = True
                return connection
            if not connection.get("ok"):
                return connection
            active = (connection.get("value") or {}).get("active_document")
            if not active:
                raise RuntimeError(t("Нет активного документа SolidWorks.",
                                     "There is no active SolidWorks document."))
            documents = await self.mcp.call("list_open_documents", {})
            if documents.get("uncertain"):
                self._uncertain = True
                return documents
            if not documents.get("ok"):
                return documents
            listing = (documents.get("value") or {}).get("documents") or []
            target = next((item for item in listing if item.get("title") == active), None)
            if target is None:
                raise RuntimeError(t("Не удалось определить активный документ.",
                                     "Could not identify the active document."))
            existing = str(target.get("path") or "").strip()
            save_as_path = "" if existing else str(_save_path_for(
                active, str(target.get("type") or "")))
            result = await self.mcp.call("save_document", {"save_as_path": save_as_path})
            if result.get("uncertain"):
                self._uncertain = True
            if result.get("ok") and not result.get("uncertain"):
                try:
                    conversation = self.conversation_snapshot(session_id)
                    conversation_error = None
                except KeyError:
                    conversation = None
                    conversation_error = t(
                        "Не найдена переписка для сохранения.",
                        "No conversation was found to save.")
                return {**result, "conversation": conversation,
                        "conversation_error": conversation_error}
            return result
        finally:
            self._saving = False

    def conversation_snapshot(self, session_id: str) -> dict:
        runs = [run.snapshot() for run in self.runs.values()
                if run.session_id == session_id]
        if not runs:
            raise KeyError(t("Переписка не найдена.", "Conversation not found."))
        messages = deepcopy(runs[-1]["messages"] or self._history.get(session_id) or [])
        for run in runs:
            run["messages"] = []
        latest = runs[-1]
        return {
            "schema": _CONVERSATION_SCHEMA,
            "version": _CONVERSATION_VERSION,
            "session_id": session_id,
            "profile_id": latest.get("profile_id") or "",
            "language": latest.get("language") or "ru",
            "saved_at": time.time(),
            "messages": messages,
            "runs": runs,
        }

    def load_conversation(self, data: dict) -> dict:
        if self._closed:
            raise RuntimeError(t("Агент завершает работу.", "The agent is shutting down."))
        if self.uncertain:
            raise RuntimeError(t(
                "Исход CAD-операции неизвестен. Проверьте SolidWorks и перезапустите приложение.",
                "The CAD operation outcome is unknown. Check SolidWorks and restart the application."))
        if self._active is not None and not self._active.done:
            raise AgentBusy(t("Уже выполняется CAD-запрос.", "A CAD request is already running."))
        if self._saving:
            raise AgentBusy(t("Сохранение уже выполняется.", "A save is already running."))
        if not isinstance(data, dict):
            raise ValueError(t("Некорректный файл переписки.", "Invalid conversation file."))
        if len(_json(data).encode("utf-8")) > _CONVERSATION_MAX_BYTES:
            raise ValueError(t("Файл переписки слишком большой.",
                               "The conversation file is too large."))
        if data.get("schema") != _CONVERSATION_SCHEMA or data.get("version") != _CONVERSATION_VERSION:
            raise ValueError(t("Неподдерживаемый формат переписки.",
                               "Unsupported conversation format."))
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError(t("Некорректный ID диалога в файле переписки.",
                               "Invalid dialogue ID in the conversation file."))
        messages = data.get("messages")
        if (not isinstance(messages, list) or not messages
                or any(not isinstance(message, dict) or not isinstance(message.get("role"), str)
                       for message in messages)):
            raise ValueError(t("В файле переписки нет корректной истории сообщений.",
                               "The conversation file has no valid message history."))
        if len(_json(messages).encode("utf-8")) > self.max_history_bytes:
            raise ValueError(t("История переписки превышает лимит контекста.",
                               "The conversation history exceeds the context limit."))
        raw_runs = data.get("runs")
        if (not isinstance(raw_runs, list) or not raw_runs
                or len(raw_runs) > _CONVERSATION_MAX_RUNS):
            raise ValueError(t("В файле переписки нет корректных прогонов.",
                               "The conversation file has no valid runs."))
        if (session_id in self._history
                or any(run.session_id == session_id for run in self.runs.values())):
            session_id = uuid4().hex
        used_ids = set(self.runs)
        loaded = []
        for index, raw in enumerate(raw_runs):
            if not isinstance(raw, dict):
                raise ValueError(t("Некорректный прогон в файле переписки.",
                                   "Invalid run in the conversation file."))
            status = raw.get("status")
            if status not in _TERMINAL:
                raise ValueError(t("В файле переписки есть незавершённый прогон.",
                                   "The conversation file contains an unfinished run."))
            events = raw.get("events") or []
            if (not isinstance(events, list) or len(events) > _CONVERSATION_MAX_EVENTS
                    or any(not isinstance(event, dict) or event.get("seq") != seq
                           or not isinstance(event.get("type"), str)
                           or not isinstance(event.get("data"), dict)
                           for seq, event in enumerate(events, 1))):
                raise ValueError(t("Некорректные события в файле переписки.",
                                   "Invalid events in the conversation file."))
            checks = raw.get("checks") or {}
            if (not isinstance(checks, dict)
                    or any(not isinstance(value, dict)
                           or not isinstance(value.get("status"), str)
                           for value in checks.values())):
                raise ValueError(t("Некорректные проверки в файле переписки.",
                                   "Invalid checks in the conversation file."))
            profile_id = raw.get("profile_id")
            if not isinstance(profile_id, str) or len(profile_id) > 100:
                profile_id = ""
            language = raw.get("language") if raw.get("language") in {"ru", "en"} else "ru"
            try:
                max_calls = int(raw.get("max_calls") or 80)
                call_count = int(raw.get("call_count") or 0)
                repair_attempts = int(raw.get("repair_attempts") or 0)
            except (TypeError, ValueError):
                raise ValueError(t("Некорректные счётчики в файле переписки.",
                                   "Invalid counters in the conversation file.")) from None
            if not 1 <= max_calls <= 1000 or call_count < 0 or not 0 <= repair_attempts <= 2:
                raise ValueError(t("Некорректные счётчики в файле переписки.",
                                   "Invalid counters in the conversation file."))
            run_id = raw.get("id")
            if not isinstance(run_id, str) or not _SESSION_ID.fullmatch(run_id) \
                    or run_id in used_ids:
                run_id = uuid4().hex
            used_ids.add(run_id)
            run = Run(session_id=session_id, profile_id=profile_id,
                      messages=deepcopy(messages) if index == len(raw_runs) - 1 else [],
                      max_calls=max_calls, language=language, status=status,
                      events=deepcopy(events), stop_requested=bool(raw.get("stop_requested")),
                      call_count=call_count, checks=deepcopy(checks))
            run.id = run_id
            run.repair_attempts = repair_attempts
            metrics = raw.get("metrics")
            if isinstance(metrics, dict):
                run.metrics.update(metrics)
            loaded.append(run)
        if messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": _system_prompt(loaded[-1].language)})
        self._history[session_id] = deepcopy(messages)
        if any(run.status == "uncertain" for run in loaded):
            self._uncertain = True
        for run in loaded:
            self.runs[run.id] = run
        document = data.get("document")
        if not isinstance(document, dict):
            document = {}
        latest = loaded[-1]
        return {"session_id": session_id, "profile_id": latest.profile_id,
                "language": latest.language, "document": deepcopy(document),
                "runs": [run.snapshot() for run in loaded]}

    def get(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise KeyError(t("Запрос не найден.", "Request not found."))
        return self.runs[run_id]

    def stop(self, run_id: str):
        run = self.get(run_id)
        if run.done or run.stop_requested:
            return
        set_language(run.language)
        run.stop_requested = True
        run._status("stopping", t(
            "Остановка: ждём исход уже отправленного COM-вызова."
            if run._inflight else "Остановка: следующие действия запрещены.",
            "Stopping: waiting for the outcome of an already sent COM call."
            if run._inflight else "Stopping: no further actions are allowed."))
        if run._answer is not None and not run._answer.done():
            run._answer.set_result((False, False))

    def confirm(self, run_id: str, confirmation_id: str, approved: bool, *, images: bool = False):
        run = self.get(run_id)
        set_language(run.language)
        if (type(approved) is not bool or type(images) is not bool or run.stop_requested
                or run.confirmation is None or run.confirmation["id"] != confirmation_id
                or run._answer is None or run._answer.done()):
            raise ValueError(t("Подтверждение отсутствует, устарело или относится к другому вызову.",
                               "The confirmation is missing, stale, or belongs to another call."))
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
            raise ValueError(t("Некорректный номер события.", "Invalid event sequence number."))
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
            raise _EndRun("stopped", t("Запрос остановлен. Уже выполненные действия не отменены.",
                                       "The request was stopped. Completed actions were not undone."))

    @staticmethod
    def _budget(run, count=1):
        if run.call_count + count > run.max_calls:
            raise _EndRun("limit_reached", t("Лимит вызовов исчерпан; новые действия не выполнялись.",
                                             "The call limit was reached; no new actions were performed."))

    async def _confirmation(self, run, kind, **data):
        self._guard(run)
        run._answer = asyncio.get_running_loop().create_future()
        run.confirmation = {"id": uuid4().hex, "kind": kind, **deepcopy(data)}
        run._status("awaiting_confirmation", t("Требуется подтверждение пользователя.",
                                               "User confirmation is required."))
        run._emit("confirmation", **run.confirmation)
        try:
            approved, images = await run._answer
            self._guard(run)
            if not approved:
                raise _EndRun("rejected", t("Пользователь отклонил действие; оно не выполнялось.",
                                            "The user declined the action; it was not performed."))
            run._status("running", t("Подтверждение получено.", "Confirmation received."))
            return images
        finally:
            run.confirmation = None
            run._answer = None

    async def _model(self, run, provider, tools):
        self._guard(run)
        # Refresh the application-owned instruction even for existing dialogue history.
        prompt = _system_prompt(run.language)
        if self.settings is not None:
            prompt += (t(
                " Постоянное разрешение Python уже включено. Пользователь выбрал всегда доверять "
                "выполнению кода; повторного подтверждения запуска не требуется.",
                " Permanent Python permission is enabled. The user chose to always trust code "
                "execution; no repeated run confirmation is required.")
                if self.settings.python_execution_allowed else t(
                " Разрешение Python ещё не выдано. Если нужен код, вызывай execute_python: "
                "первое подтверждение покажет интерфейс приложения.",
                " Python permission has not been granted. If code is needed, call execute_python: "
                "the application will show the first confirmation."))
        run.messages[0] = {"role": "system", "content": prompt}
        if len(_json(run.messages).encode("utf-8")) > self.max_history_bytes:
            raise _EndRun("failed", t("История превысила лимит контекста. Начните новый диалог.",
                                      "The history exceeded the context limit. Start a new dialogue."))
        number = 1 + sum(event["type"] == "model_start" for event in run.events)
        run.metrics["model_calls"] += 1
        run._emit("model_start", number=number)
        started = time.perf_counter()
        outcome = "error"
        try:
            result = await provider.complete(deepcopy(run.messages), deepcopy(tools))
            outcome = "received"
        finally:
            run.metrics["model_seconds"] += time.perf_counter() - started
            metrics = getattr(provider, "last_metrics", None)
            safe = {}
            if isinstance(metrics, dict):
                safe = {key: value for key, value in metrics.items()
                        if value is None or (type(value) in {int, float}
                                             and math.isfinite(value) and value >= 0)}
                for key in ("request_bytes", "image_count"):
                    value = safe.get(key)
                    if value is not None:
                        run.metrics[key] = (run.metrics[key] or 0) + value
            for key in ("input_tokens", "output_tokens", "cached_tokens", "cost"):
                value = safe.get(key)
                if value is None:
                    run._missing_usage.add(key)
                run.metrics[key] = None if key in run._missing_usage else (run.metrics[key] or 0) + value
            if safe:
                run._emit("model_metrics", number=number, **safe)
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
                raise ValueError(t("Некорректный каталог инструментов.", "Invalid tool catalog."))
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
            raise _EndRun("failed", t("В каталоге отсутствует необходимый инструмент проверки.",
                                      "The required verification tool is missing from the catalog."))
        run.call_count += 1
        run._emit("tool_start", tool_call_id=call_id, name=name, arguments=arguments,
                  verification=verification)
        started = time.perf_counter()
        run._inflight = True
        try:
            transport_arguments = deepcopy(arguments)
            if name == "execute_python" and run._session_capable:
                if run._python_session is None:
                    opened = await self.mcp.call("open_python_session", {})
                    self._success(opened)
                    run._python_session = opened["value"]["session_id"]
                    if not isinstance(run._python_session, str) or not run._python_session:
                        raise ValueError("Invalid Python session")
                self._guard(run)
                transport_arguments["session_id"] = run._python_session
            result = deepcopy(await self.mcp.call(name, transport_arguments))
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
                      "error": t("Связь MCP прервана; результат CAD-операции неизвестен.",
                                 "The MCP connection was interrupted; the CAD operation outcome is unknown.")}
        finally:
            run._inflight = False
            prefix = "verification" if verification else "cad"
            run.metrics[prefix + "_calls"] += 1
            run.metrics[prefix + "_seconds"] += time.perf_counter() - started
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
            raise _EndRun("uncertain", t(
                "Результат CAD-операции неизвестен. Проверьте SolidWorks; повтор запрещён.",
                "The CAD operation outcome is unknown. Check SolidWorks; retrying is forbidden."))
        if not result.get("ok"):
            raise _EndRun("failed", t("CAD-инструмент вернул ошибку. Запрос остановлен без повторов.",
                                      "The CAD tool returned an error. The request stopped without retries."))

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
            raise _EndRun("failed", t("Независимая проверка не подтвердила изменение. Повтора не будет.",
                                      "Independent verification did not confirm the change. There will be no retry."))

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
                raise _EndRun("failed", t("В каталоге отсутствует необходимый инструмент проверки.",
                                          "The required verification tool is missing from the catalog."))
        evidence = []
        before = None
        if name == "execute_python":
            scope = (run.session_id, provider.profile)
            approved = (self.settings.python_execution_allowed if self.settings is not None
                        else scope in self._python_approved)
            if not approved:
                await self._confirmation(run, "python", tool_call_id=call_id, code=arguments["code"],
                    message=t("Разрешить выполнение Python-кода постоянно? Разрешение действует "
                              "для всех диалогов и подключений, включая перезапуск приложения.",
                              "Allow Python code execution permanently? The permission applies "
                              "to all dialogues and connections, including after an application restart.")
                            if self.settings is not None else t(
                              "Разрешить Python-код в этом диалоге и подключении?",
                              "Allow Python code in this dialogue and connection?"))
                if self.settings is not None:
                    self.settings.set_python_execution_allowed(True)
                else:
                    self._python_approved.add(scope)
        if name in _FEATURE_RESULTS:
            tree = await self._read(run, catalog, "get_feature_tree", {}, call_id, evidence)
            if len(tree["features"]) >= _TREE_LIMIT:
                raise _EndRun("failed", t("Дерево может быть усечено; создание не выполнялось.",
                                          "The feature tree may be truncated; nothing was created."))
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
            run.checks[call_id] = {"status": "pending", "message": t(
                "Изменение ещё не проверено.", "The change has not been verified yet.")}
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
                    "message": t("Результат подтверждён.", "The result is confirmed.") if verified else t(
                        "Выполнено, результат требует проверки.",
                        "Completed; the result requires verification."),
                    "evidence": evidence}
                if name == "execute_python" and run.repairing:
                    previous = run.checks.get(run._repair_call_id)
                    if previous and previous.get("status") == "failed":
                        previous["status"] = "repaired"
                        previous["message"] = t(
                            "Ошибка исправлена продолжением; исходный вызов не повторялся.",
                            "The error was repaired with a continuation; the original call was not repeated.")
                    run.repairing = False
                    run._repair_call_id = None
        except _EndRun as error:
            if mutating:
                run.checks[call_id] = {"status": "failed" if error.status == "failed" else "unverified",
                                       "message": error.message, "evidence": evidence}
            raise
        except Exception:
            if mutating:
                run.checks[call_id] = {"status": "failed", "message": t(
                    "Проверка результата завершилась ошибкой.",
                    "Result verification failed with an error."), "evidence": evidence}
            raise
        finally:
            if mutating and message is not None:
                content["verification"] = deepcopy(run.checks[call_id])
                message["content"] = _json(content)
                run._emit("tool_result", tool_call_id=call_id, name=name, result=content, verification=False)

    @staticmethod
    def _close_pending(run, *, reason=None):
        reason = reason or t("Вызов не выполнялся: запрос остановлен.",
                             "The call was not performed: the request was stopped.")
        for call_id in run._pending:
            run.messages.append({"role": "tool", "tool_call_id": call_id,
                "content": _json({"ok": False, "error": reason,
                                  "uncertain": False})})
        run._pending.clear()

    async def _prepare_python_repair(self, run, error):
        if run.repair_attempts >= _PYTHON_REPAIR_LIMIT:
            raise _EndRun("failed", t(
                "Исправление Python-кода не удалось после двух попыток; повтор остановлен.",
                "Python code repair failed after two attempts; retrying was stopped."))
        run.repair_attempts += 1
        run.repairing = True
        run._repair_call_id = error.call_id
        value = error.result.get("value") if isinstance(error.result, dict) else None
        details = {key: value.get(key) for key in ("error", "error_code", "python_line", "error_type",
                                                  "hint", "phase", "changes_started")
                   if isinstance(value, dict) and value.get(key) is not None}
        preflight = details.get("phase") == "preflight" and details.get("changes_started") is False
        instruction = t(
            "Предварительная проверка отклонила весь фрагмент до обращения к CAD. "
            "Исправь отклонённый фрагмент целиком, включая необходимую подготовку объектов: "
            "ни одна его строка не исполнялась. Не повторяй действия предыдущих вызовов. ",
            "Preflight rejected the entire fragment before accessing CAD. "
            "Submit the full corrected fragment, including necessary object setup: "
            "none of its lines ran. Do not repeat actions from earlier calls. ") if preflight else t(
            "Не повторяй уже выполненные шаги и не запускай исходный скрипт целиком. "
            "Сначала используй доступные read-only инструменты или безопасную инспекцию, "
            "затем вызови execute_python только с минимальным продолжением, исправляющим ошибку. ",
            "Do not repeat steps that already ran and do not rerun the entire original script. "
            "First use the available read-only tools or a safe inspection, "
            "then call execute_python only with a minimal continuation that fixes the error. ")
        message = t(
            f"Известная ошибка execute_python. Подготовь исправление, попытка "
            f"{run.repair_attempts} из {_PYTHON_REPAIR_LIMIT}. {instruction}"
            "Если исправление невозможно, сообщи об этом без нового изменения. "
            f"Диагностика: {_json(details)}",
            f"A known execute_python error occurred. Prepare a repair, attempt "
            f"{run.repair_attempts} of {_PYTHON_REPAIR_LIMIT}. {instruction}"
            "If repair is impossible, report that without another change. "
            f"Diagnostics: {_json(details)}")
        run._status("repairing", t(
            f"Анализируем ошибку Python; попытка {run.repair_attempts} из {_PYTHON_REPAIR_LIMIT}.",
            f"Analyzing the Python error; attempt {run.repair_attempts} of {_PYTHON_REPAIR_LIMIT}."))
        run._emit("repair", attempt=run.repair_attempts, call_id=error.call_id,
                  message=t(f"Подготовка исправления Python, попытка {run.repair_attempts} из {_PYTHON_REPAIR_LIMIT}.",
                            f"Preparing a Python repair, attempt {run.repair_attempts} of {_PYTHON_REPAIR_LIMIT}."))
        self._close_pending(run, reason=t("Вызов не выполнялся: предыдущий скрипт завершился ошибкой.",
                                          "The call was not performed: the previous script failed."))
        self._flush_images(run)
        run.messages.append({"role": "user", "content": message})

    async def _run(self, run, provider):
        set_language(run.language)
        try:
            self._guard(run)
            run._status("running", t("Запрос выполняется.", "The request is running."))
            tools = await self.mcp.tools()
            run._session_capable = {"open_python_session", "close_python_session"} <= {
                tool["function"]["name"] for tool in tools}
            tools = deepcopy([tool for tool in tools if tool["function"]["name"] not in
                              {"open_python_session", "close_python_session"}])
            for tool in tools:
                if tool["function"]["name"] == "execute_python":
                    schema = tool["function"]["parameters"]
                    schema["properties"].pop("session_id", None)
                    schema["required"] = [key for key in schema.get("required", [])
                                          if key != "session_id"]
                    schema["additionalProperties"] = False
            if len(_json(tools).encode("utf-8")) > self.max_catalog_bytes:
                raise _EndRun("failed", t("Каталог инструментов превысил допустимый размер.",
                                          "The tool catalog exceeded the allowed size."))
            catalog = self._catalog(tools)
            while True:
                response = await self._model(run, provider, tools)
                if len(_json(response).encode("utf-8")) > self.max_history_bytes:
                    raise _EndRun("failed", t("Ответ модели превысил допустимый размер контекста.",
                                              "The model response exceeded the allowed context size."))
                used_ids = {call["id"] for previous in run.messages
                            for call in previous.get("tool_calls", [])}
                message, calls = self._prepare(response, provider, catalog, used_ids)
                if len(_json([*run.messages, message]).encode("utf-8")) > self.max_history_bytes:
                    raise _EndRun("failed", t("История превысила лимит контекста. Начните новый диалог.",
                                              "The history exceeded the context limit. Start a new dialogue."))
                run.messages.append(message)
                if message["content"]:
                    run._emit("message", role="assistant", content=message["content"])
                if not calls:
                    if run.repairing:
                        raise _EndRun("failed", t(
                            "Модель не предложила исправление Python-кода; запрос остановлен.",
                            "The model did not propose a Python repair; the request was stopped."))
                    run._status("completed", t("Запрос завершён. Результат требует проверки.",
                                               "The request completed. The result requires verification.")
                                if run.verification == "unverified" else t("Запрос завершён.",
                                                                            "The request completed."))
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
            run._status("failed", t("Ошибка API модели; CAD-вызовы не повторялись.",
                                    "Model API error; CAD calls were not repeated."))
        except Exception:
            run._emit("error", message=t(
                "Некорректные данные модели, каталога или проверки; запрос остановлен.",
                "Invalid model, catalog, or verification data; the request was stopped."),
                      uncertain=False)
            run._status("failed", t("Запрос остановлен из-за ошибки.",
                                    "The request was stopped because of an error."))
        finally:
            if run._python_session is not None:
                try:
                    closed = await self.mcp.call("close_python_session", {
                        "session_id": run._python_session})
                    if not closed.get("ok"):
                        self._uncertain = True
                        run._status("uncertain", t("Не удалось подтвердить закрытие Python-сессии.",
                                                    "Python session closure could not be confirmed."))
                except Exception:
                    self._uncertain = True
                    run._status("uncertain", t("Связь MCP прервана при закрытии Python-сессии.",
                                                "MCP disconnected while closing the Python session."))
                finally:
                    run._python_session = None
            self._close_pending(run)
            self._flush_images(run)
            run.repairing = False
            run._repair_call_id = None
            self._history[run.session_id] = deepcopy(run.messages)
            run._changed.set()

    @staticmethod
    def _flush_images(run):
        if run._images:
            run.messages.append({"role": "user", "_cadnext": {"source": "screenshot"},
                "content": [{"type": "image_url", "image_url": {
                "url": f'data:{image["mimeType"]};base64,{image["data"]}'}} for image in run._images]})
            run._images.clear()
