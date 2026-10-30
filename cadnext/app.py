"""Loopback API and static chat, with one lifespan-owned MCP and agent."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cadnext.agent import Agent, AgentBusy
from cadnext.attachments import AttachmentError
from cadnext.conversations import write_sidecar
from cadnext.debug import DebugLog, DebugMcp, debug_scope
from cadnext.evaluation import EvaluationOptions
from cadnext.i18n import get_language, language_from_header, set_language, t
from cadnext.mcp import ROOT, SolidWorksMcp
from cadnext.privacy import PrivacyContext
from cadnext.provider import Provider, ProviderError
from cadnext.reasoning import ReasoningCatalog
from cadnext.settings import Profile, ReasoningEffort, SettingsStore


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=300)
    reasoning_effort: ReasoningEffort | None = None
    stream_responses: bool = False
    vision: bool = False
    local: bool | None = None
    api_key: SecretStr | None = None

    def values(self):
        values = self.model_dump(exclude_unset=True)
        if isinstance(values.get("api_key"), SecretStr):
            values["api_key"] = values["api_key"].get_secret_value()
        return values


class AttachmentBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    mimeType: str
    data: str


class EvaluationOptionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = True
    target_similarity: float = Field(default=100, gt=0, le=100)
    max_evaluations: int = Field(default=10, ge=1, le=100)
    max_no_progress: int = Field(default=3, ge=1, le=10)

    def options(self) -> EvaluationOptions:
        return EvaluationOptions(enabled=self.enabled,
                                 target_similarity=float(self.target_similarity),
                                 max_evaluations=self.max_evaluations,
                                 max_no_progress=self.max_no_progress)


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    profile_id: str = Field(min_length=1, max_length=100)
    message: str = Field(max_length=20000)
    attachments: list[AttachmentBody] = Field(default_factory=list)
    max_calls: int = Field(default=80, ge=1, le=1000)
    evaluation: EvaluationOptionsBody = Field(default_factory=EvaluationOptionsBody)
    sensitive: list[str] = Field(default_factory=list, max_length=100)
    request_id: str | None = Field(default=None, min_length=1, max_length=100,
                                   pattern=r"^[A-Za-z0-9_-]+$")
    language: Literal["ru", "en"] | None = None


class ModelListBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_url: str = Field(min_length=1, max_length=2000)
    profile_id: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: SecretStr | None = None


class ConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmation_id: str = Field(min_length=1, max_length=100)
    approved: bool
    images: bool = False


class AppSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    python_execution_allowed: bool | None = None
    debug_logging_enabled: bool | None = None
    vision_profile_id: str | None = Field(default=None, max_length=100)
    vision_routing_approved: bool | None = None


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conversation: dict


def create_app(*, settings=None, mcp_factory=SolidWorksMcp, provider_factory=Provider,
               ui_dir: Path | None = None) -> FastAPI:
    ui_dir = Path(ui_dir) if ui_dir is not None else ROOT / "ui" / "dist"

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings if settings is not None else SettingsStore()
        app.state.debug = DebugLog(app.state.settings)
        if app.state.settings.debug_logging_enabled:
            for profile in app.state.settings.list():
                app.state.debug.register_secret(app.state.settings.secret(profile.id))
        app.state.debug.write("application_start")
        app.state.contexts = {}
        app.state.providers = {}
        app.state.cleanups = set()
        app.state.request_runs = {}
        app.state.stopped_requests = set()
        app.state.project_lock = asyncio.Lock()
        app.state.reasoning = ReasoningCatalog(public_model_catalog)
        async with mcp_factory() as mcp:
            app.state.agent = Agent(DebugMcp(mcp, app.state.debug), settings=app.state.settings,
                                    debug=app.state.debug)
            try:
                yield
            finally:
                await app.state.agent.aclose()
                if app.state.cleanups:
                    await asyncio.gather(*app.state.cleanups, return_exceptions=True)
                app.state.debug.write("application_stop")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        request_id = uuid4().hex
        started = time.perf_counter()
        set_language(language_from_header(request.headers.get("accept-language")))
        if request.url.path == "/api/conversations/open":
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                length = 0
            if length > 65 * 1024 * 1024:
                return JSONResponse({"detail": t(
                    "Файл переписки слишком большой.",
                    "The conversation file is too large.")}, status_code=413)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
            return JSONResponse({"detail": t("Запрос с другого сайта запрещён.",
                                             "A request from another site is not allowed.")}, status_code=403)
        try:
            with debug_scope(request_id=request_id):
                response = await call_next(request)
        except Exception as error:
            app.state.debug.exception("http_exception", error, request_id=request_id,
                                       method=request.method, path=request.url.path)
            return JSONResponse({"detail": t(
                "Внутренняя ошибка приложения. Данные запроса не раскрываются.",
                "Internal application error. Request data is not disclosed.")},
                                status_code=500)
        if request.url.path.startswith('/api/'):
            app.state.debug.write("http_response", request_id=request_id, method=request.method,
                path=request.url.path, status=response.status_code,
                duration_seconds=time.perf_counter() - started)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": t("Некорректные поля запроса. Проверьте введённые значения.",
                                              "Invalid request fields. Check the entered values.")},
                            status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(request, error):
        return JSONResponse({"detail": t("Некорректные настройки или параметры запроса.",
                                              "Invalid settings or request parameters.")}, status_code=422)

    @app.exception_handler(AttachmentError)
    async def invalid_attachment(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"detail": t("Подключение или запрос не найден.",
                                              "Connection or request not found.")}, status_code=404)

    @app.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=502)

    def store():
        return app.state.settings

    def agent():
        return app.state.agent

    async def project_operation(operation, *args):
        async def serialized():
            async with app.state.project_lock:
                return await operation(*args)
        task = asyncio.create_task(serialized())
        app.state.cleanups.add(task)
        task.add_done_callback(app.state.cleanups.discard)
        return await asyncio.shield(task)

    @app.get("/api/health")
    async def health():
        active = next((run for run in agent().runs.values() if not run.done), None)
        uncertain = agent().uncertain or any(
            run.status == "uncertain" for run in agent().runs.values())
        return {"ok": True, "active_run_id": active.id if active else None, "uncertain": uncertain}

    @app.post("/api/cad/save")
    async def save_cad(body: SaveBody):
        with debug_scope(session_id=body.session_id):
            return await project_operation(save_cad_locked, body)

    async def save_cad_locked(body):
        try:
            result = await agent().save_project(body.session_id)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        if result.get("uncertain"):
            raise HTTPException(status_code=409, detail=t(
                "Результат сохранения неизвестен. Проверьте SolidWorks.",
                "The save outcome is unknown. Check SolidWorks."))
        if not result.get("ok"):
            raise HTTPException(status_code=422, detail=t(
                "Не удалось сохранить проект. Проверьте SolidWorks.",
                "Could not save the project. Check SolidWorks."))
        saved = (result.get("value") or {}).get("saved") or ""
        warning = result.get("conversation_error")
        conversation_path = None
        conversation = result.get("conversation")
        if conversation:
            conversation["document"] = {"path": saved}
            try:
                conversation_path = str(write_sidecar(saved, conversation))
            except (OSError, ValueError, TypeError):
                warning = t(
                    "Проект сохранён, но переписку сохранить не удалось.",
                    "The project was saved, but the conversation could not be saved.")
        payload = {"ok": True, "saved": saved}
        if conversation_path:
            payload["conversation"] = conversation_path
        if warning:
            payload["warning"] = warning
        return payload

    @app.post("/api/conversations/open")
    async def open_conversation(body: ConversationBody):
        return await project_operation(open_conversation_locked, body)

    async def open_conversation_locked(body):
        try:
            result = agent().load_conversation(body.conversation)
            app.state.debug.write("project_opened", session_id=result["session_id"],
                run_ids=[run["id"] for run in result["runs"]], document=result.get("document"))
            return result
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None

    @app.post("/api/projects/{session_id}/close", status_code=204)
    async def close_project(session_id: str):
        await project_operation(agent().close_conversation, session_id)
        app.state.contexts = {key: value for key, value in app.state.contexts.items()
                              if key[0] != session_id}

    @app.get("/api/projects/{session_id}/state")
    async def project_state(session_id: str):
        with debug_scope(session_id=session_id):
            return await project_operation(agent().project_state, session_id)

    @app.get("/api/profiles")
    def profiles():
        return [store().public(profile) for profile in store().list()]

    @app.get("/api/settings")
    def app_settings():
        routing = store().vision_routing
        return {"python_execution_allowed": store().python_execution_allowed,
                "debug_logging_enabled": store().debug_logging_enabled,
                "debug_log_path": str(app.state.debug.path),
                "debug_log_error": (t(
                    f"Не удаётся записать журнал отладки ({app.state.debug.error}).",
                    f"Could not write the debug log ({app.state.debug.error}).")
                    if app.state.debug.error else None),
                "debug_log_available": app.state.debug.path.is_file(),
                "vision_profile_id": routing["profile_id"] if routing else None,
                "vision_routing_approved": bool(routing),
                "vision_routing_fingerprint": routing["fingerprint"] if routing else None}

    @app.put("/api/settings")
    async def update_app_settings(body: AppSettingsBody):
        fields = body.model_fields_set
        if not fields:
            raise ValueError(t("Не указаны изменяемые настройки приложения.",
                               "No application settings were provided."))
        if "debug_logging_enabled" in fields and body.debug_logging_enabled is None:
            raise ValueError(t("Режим отладки должен быть логическим значением.",
                               "Debug logging must be a boolean value."))
        routing_fields = {"vision_profile_id", "vision_routing_approved"}
        if fields & routing_fields:
            approved = body.vision_routing_approved
            if approved is None:
                raise ValueError(t("Укажите явное согласие на автоматическую Vision-маршрутизацию.",
                                   "Provide explicit approval for automatic Vision routing."))
            if approved:
                if not body.vision_profile_id:
                    raise ValueError(t("Выберите Vision-профиль для автоматической маршрутизации.",
                                       "Choose a Vision profile for automatic routing."))
                store().set_vision_routing(body.vision_profile_id, approved=True)
            else:
                store().set_vision_routing(body.vision_profile_id, approved=False)
        if "python_execution_allowed" in fields:
            if body.python_execution_allowed is None:
                raise ValueError(t("Разрешение Python должно быть логическим значением.",
                                   "Python permission must be a boolean value."))
            store().set_python_execution_allowed(body.python_execution_allowed)
        if "debug_logging_enabled" in fields:
            try:
                if body.debug_logging_enabled:
                    for profile in store().list():
                        app.state.debug.register_secret(store().secret(profile.id))
                    app.state.debug.prepare()
                else:
                    app.state.debug.write("debug_disable_requested")
                store().set_debug_logging_enabled(body.debug_logging_enabled)
            except OSError as error:
                app.state.debug.exception("debug_configuration_error", error)
                raise HTTPException(status_code=500, detail=t(
                    "Не удалось сохранить режим отладки или открыть журнал. Проверьте доступ к каталогу.",
                    "Could not save debug mode or open its log. Check directory access.")) from None
            if body.debug_logging_enabled:
                app.state.debug.write("debug_enabled", existing_runs=len(agent().runs))
                for run in agent().runs.values():
                    app.state.debug.write("run_snapshot", run_id=run.id, session_id=run.session_id,
                        status=run.status, goal=run.goal, events=run.events[-50:], messages=run.messages[-10:])
        return app_settings()

    @app.get("/api/debug/logs")
    def debug_logs():
        try:
            content = app.state.debug.archive()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=t("Журнал отладки ещё не создан.",
                                                         "No debug log has been created yet.")) from None
        return Response(content, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="cadnext-debug.zip"'})

    @app.post("/api/profiles", status_code=201)
    def add_profile(body: ProfileBody):
        if body.api_key:
            app.state.debug.register_secret(body.api_key.get_secret_value())
        return store().public(store().save(body.values()))

    @app.get("/api/profiles/{profile_id}")
    def get_profile(profile_id: str):
        return store().public(store().get(profile_id))

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: str, body: ProfileBody):
        if body.api_key:
            app.state.debug.register_secret(body.api_key.get_secret_value())
        return store().public(store().save(body.values(), profile_id))

    @app.delete("/api/profiles/{profile_id}", status_code=204)
    def delete_profile(profile_id: str):
        store().delete(profile_id)

    def provider_for(profile, *, privacy=None):
        if privacy is None:
            # Connection probes contain a fixed application message, never CAD history.
            privacy = PrivacyContext(local=profile.local, consent=True, vision=profile.vision,
                                     profile_id=profile.id)
        secret = store().secret(profile.id)
        app.state.debug.register_secret(secret)
        provider = provider_factory(profile, secret, privacy=privacy)
        provider.debug = app.state.debug
        return provider

    async def public_model_catalog():
        profile = Profile(name="Публичный каталог OpenRouter", base_url="https://openrouter.ai/api/v1",
                          model="catalog")
        privacy = PrivacyContext(local=False, consent=True, profile_id=profile.id)
        # A separate provider has no access to any saved connection's credentials or messages.
        provider = provider_factory(profile, None, privacy=privacy)
        try:
            return await asyncio.wait_for(provider.list_model_catalog(), timeout=12)
        finally:
            await provider.aclose()

    @app.get("/api/reasoning")
    async def reasoning(model: str = Query(min_length=1, max_length=300)):
        return await app.state.reasoning.lookup(model)

    @app.get("/api/models")
    async def models(profile_id: str):
        provider = provider_for(store().get(profile_id))
        try:
            return await provider.list_model_catalog()
        finally:
            await provider.aclose()

    @app.post("/api/models")
    async def draft_models(body: ModelListBody):
        # Listing needs neither a saved profile nor a selected model.
        draft = Profile(name="Каталог моделей", base_url=body.base_url, model="catalog")
        saved = store().get(body.profile_id) if body.profile_id else None
        secret = body.api_key.get_secret_value() if body.api_key is not None else None
        if body.api_key is None and saved and saved.base_url == draft.base_url:
            secret = store().secret(saved.id)
        privacy = PrivacyContext(local=draft.local, consent=True, profile_id=draft.id)
        app.state.debug.register_secret(secret)
        provider = provider_factory(draft, secret, privacy=privacy)
        provider.debug = app.state.debug
        try:
            return await provider.list_model_catalog()
        finally:
            await provider.aclose()

    @app.post("/api/profiles/{profile_id}/test")
    async def test_profile(profile_id: str):
        provider = provider_for(store().get(profile_id))
        try:
            result = await provider.complete([{"role": "user", "content": t(
                "Проверь native tool calling: вызови cadnext_connection_test без аргументов.",
                "Check native tool calling: call cadnext_connection_test with no arguments.")}],
                [{"type": "function", "function": {"name": "cadnext_connection_test",
                    "description": t("Проверка подключения без CAD-операций.",
                                     "Connection check without CAD operations."), "parameters": {
                        "type": "object", "properties": {}, "additionalProperties": False}}}])
            calls = result.get("tool_calls", [])
            if not calls or any(call["function"]["name"] != "cadnext_connection_test" for call in calls):
                raise ProviderError(t("Модель не выполнила native tool calling. Проверьте поддержку инструментов.",
                                           "The model did not perform native tool calling. Check tool support."))
            return {"ok": True, "message": t("Подключение и вызовы инструментов работают.",
                                                  "The connection and tool calls work.")}
        finally:
            await provider.aclose()

    async def release_provider(run, provider):
        try:
            await agent().wait(run.id)
        finally:
            try:
                await provider.aclose()
            finally:
                app.state.providers.pop(run.id, None)

    @app.post("/api/runs", status_code=202)
    async def start_run(body: RunBody):
        return await project_operation(start_run_locked, body)

    async def start_run_locked(body):
        if body.request_id and (body.request_id in app.state.stopped_requests
                                or body.request_id in app.state.request_runs):
            raise HTTPException(status_code=409, detail=t("Запрос уже отправлен или остановлен.",
                                                               "The request was already sent or stopped."))
        try:
            agent().assert_startable()
        except (AgentBusy, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        requested = store().get(body.profile_id)
        options = body.evaluation.options()
        profile = requested
        if options.enabled and not requested.vision:
            routing = store().vision_routing
            if routing is None:
                raise HTTPException(status_code=422, detail=t(
                    "Для цикла оценки нужен назначенный Vision-профиль с явным согласием. "
                    "Настройте его или отключите оценку.",
                    "The evaluation loop needs an approved Vision profile. "
                    "Configure it or disable evaluation."))
            profile = store().get(routing["profile_id"])
            if not profile.vision:
                raise HTTPException(status_code=422, detail=t(
                    "Назначенный Vision-профиль больше не поддерживает изображения. "
                    "Обновите назначение или отключите оценку.",
                    "The assigned Vision profile no longer supports images. "
                    "Update the assignment or disable evaluation."))
        if body.attachments and not profile.vision:
            raise AttachmentError(t("Для отправки изображений включите Vision в настройках подключения.",
                                       "Enable images in the connection settings to send pictures."))
        await agent().project_state(body.session_id)
        key = (body.session_id, *asdict(profile).values(), tuple(body.sensitive))
        privacy = app.state.contexts.get(key)
        if privacy is None:
            privacy = PrivacyContext(local=profile.local, vision=profile.vision,
                session_id=body.session_id, profile_id=profile.id, sensitive=body.sensitive)
            app.state.contexts[key] = privacy
        if body.language is not None:
            set_language(body.language)
        provider = provider_for(profile, privacy=privacy)
        try:
            run = agent().start(session_id=body.session_id, provider=provider,
                                message=body.message, attachments=[item.model_dump() for item in body.attachments],
                                max_calls=body.max_calls, language=body.language or get_language(),
                                requested_profile_id=requested.id, evaluation=options)
        except (AgentBusy, RuntimeError) as error:
            await provider.aclose()
            raise HTTPException(status_code=409, detail=str(error)) from None
        except Exception:
            await provider.aclose()
            raise
        app.state.providers[run.id] = provider
        app.state.debug.write("run_request", run_id=run.id, session_id=run.session_id,
            client_request_id=body.request_id, requested_profile_id=requested.id,
            profile_id=profile.id, language=run.language, max_calls=body.max_calls,
            evaluation=body.evaluation.model_dump())
        if body.request_id:
            app.state.request_runs[body.request_id] = run.id
        task = asyncio.create_task(release_provider(run, provider))
        app.state.cleanups.add(task)
        task.add_done_callback(app.state.cleanups.discard)
        return run.snapshot()

    @app.post("/api/requests/{request_id}/stop", status_code=204)
    async def stop_request(request_id: str):
        # A beacon can arrive before the corresponding POST /runs.
        if not 1 <= len(request_id) <= 100:
            raise HTTPException(status_code=422, detail=t("Некорректный ID запроса.", "Invalid request ID."))
        app.state.stopped_requests.add(request_id)
        run_id = app.state.request_runs.get(request_id)
        if run_id:
            agent().stop(run_id)

    @app.get("/api/runs")
    async def list_runs(session_id: str | None = None):
        return [run.snapshot() for run in agent().runs.values()
                if session_id is None or run.session_id == session_id]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return agent().get(run_id).snapshot()

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str):
        agent().stop(run_id)
        return agent().get(run_id).snapshot()

    @app.post("/api/runs/{run_id}/confirm")
    async def confirm_run(run_id: str, body: ConfirmationBody):
        try:
            agent().confirm(run_id, body.confirmation_id, body.approved, images=body.images)
        except ValueError:
            raise HTTPException(status_code=409, detail=t("Подтверждение устарело или имеет другой ID.",
                                                                   "The confirmation is stale or has a different ID.")) from None
        return agent().get(run_id).snapshot()

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
        run = agent().get(run_id)
        try:
            cursor = int(request.headers.get("last-event-id", str(after)))
            if cursor < 0 or cursor > len(run.events):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=422, detail=t("Некорректный номер события.",
                                                               "Invalid event sequence number.")) from None

        async def stream():
            iterator = agent().events(run_id, after=cursor)
            try:
                async for event in iterator:
                    yield {"id": str(event["seq"]), "event": "update",
                           "data": json.dumps(event, ensure_ascii=False)}
            finally:
                await iterator.aclose()
                if not run.done:
                    agent().stop(run_id)

        async def disconnected(message):
            if not run.done:
                agent().stop(run_id)

        return EventSourceResponse(stream(), ping=2, send_timeout=15,
                                   ping_message_factory=lambda: ServerSentEvent(event="heartbeat",
                                       data=json.dumps({"last_seq": len(run.events)})),
                                   client_close_handler_callable=disconnected)

    @app.get("/")
    async def index():
        path = ui_dir / "index.html"
        if not path.is_file():
            return JSONResponse({"detail": t("Интерфейс не собран. Выполните npm ci и npm run build в ui/.",
                                              "The interface is not built. Run npm ci and npm run build in ui/.")},
                                status_code=503)
        return FileResponse(path)

    if (ui_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ui_dir / "assets"), name="assets")
    return app
