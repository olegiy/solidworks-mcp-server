"""Loopback API and static chat, with one lifespan-owned MCP and agent."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cadnext.agent import Agent, AgentBusy
from cadnext.mcp import ROOT, SolidWorksMcp
from cadnext.privacy import PrivacyContext
from cadnext.provider import Provider, ProviderError
from cadnext.settings import Profile, SettingsStore


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=300)
    vision: bool = False
    local: bool | None = None
    api_key: SecretStr | None = None

    def values(self):
        values = self.model_dump(exclude_unset=True)
        if isinstance(values.get("api_key"), SecretStr):
            values["api_key"] = values["api_key"].get_secret_value()
        return values


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    profile_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=20000)
    max_calls: int = Field(default=80, ge=1, le=1000)
    sensitive: list[str] = Field(default_factory=list, max_length=100)
    request_id: str | None = Field(default=None, min_length=1, max_length=100,
                                   pattern=r"^[A-Za-z0-9_-]+$")


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


def create_app(*, settings=None, mcp_factory=SolidWorksMcp, provider_factory=Provider,
               ui_dir: Path | None = None) -> FastAPI:
    ui_dir = Path(ui_dir) if ui_dir is not None else ROOT / "ui" / "dist"

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings if settings is not None else SettingsStore()
        app.state.contexts = {}
        app.state.providers = {}
        app.state.cleanups = set()
        app.state.request_runs = {}
        app.state.stopped_requests = set()
        async with mcp_factory() as mcp:
            app.state.agent = Agent(mcp)
            try:
                yield
            finally:
                await app.state.agent.aclose()
                if app.state.cleanups:
                    await asyncio.gather(*app.state.cleanups, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
            return JSONResponse({"detail": "Запрос с другого сайта запрещён."}, status_code=403)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse({"detail": "Внутренняя ошибка приложения. Данные запроса не раскрываются."},
                                status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "Некорректные поля запроса. Проверьте введённые значения."},
                            status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_value(request, error):
        return JSONResponse({"detail": "Некорректные настройки или параметры запроса."}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"detail": "Подключение или запрос не найден."}, status_code=404)

    @app.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=502)

    def store():
        return app.state.settings

    def agent():
        return app.state.agent

    @app.get("/api/health")
    async def health():
        active = next((run for run in agent().runs.values() if not run.done), None)
        uncertain = any(run.status == "uncertain" for run in agent().runs.values())
        return {"ok": True, "active_run_id": active.id if active else None, "uncertain": uncertain}

    @app.get("/api/profiles")
    def profiles():
        return [store().public(profile) for profile in store().list()]

    @app.post("/api/profiles", status_code=201)
    def add_profile(body: ProfileBody):
        return store().public(store().save(body.values()))

    @app.get("/api/profiles/{profile_id}")
    def get_profile(profile_id: str):
        return store().public(store().get(profile_id))

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: str, body: ProfileBody):
        return store().public(store().save(body.values(), profile_id))

    @app.delete("/api/profiles/{profile_id}", status_code=204)
    def delete_profile(profile_id: str):
        store().delete(profile_id)

    def provider_for(profile, *, privacy=None):
        if privacy is None:
            # Connection probes contain a fixed application message, never CAD history.
            privacy = PrivacyContext(local=profile.local, consent=True, vision=profile.vision,
                                     profile_id=profile.id)
        return provider_factory(profile, store().secret(profile.id), privacy=privacy)

    @app.get("/api/models")
    async def models(profile_id: str):
        provider = provider_for(store().get(profile_id))
        try:
            return await provider.list_models()
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
        provider = provider_factory(draft, secret, privacy=privacy)
        try:
            return await provider.list_models()
        finally:
            await provider.aclose()

    @app.post("/api/profiles/{profile_id}/test")
    async def test_profile(profile_id: str):
        provider = provider_for(store().get(profile_id))
        try:
            result = await provider.complete([{"role": "user", "content":
                "Проверь native tool calling: вызови cadnext_connection_test без аргументов."}],
                [{"type": "function", "function": {"name": "cadnext_connection_test",
                    "description": "Проверка подключения без CAD-операций.", "parameters": {
                        "type": "object", "properties": {}, "additionalProperties": False}}}])
            calls = result.get("tool_calls", [])
            if not calls or any(call["function"]["name"] != "cadnext_connection_test" for call in calls):
                raise ProviderError("Модель не выполнила native tool calling. Проверьте поддержку инструментов.")
            return {"ok": True, "message": "Подключение и вызовы инструментов работают."}
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
        if body.request_id and (body.request_id in app.state.stopped_requests
                                or body.request_id in app.state.request_runs):
            raise HTTPException(status_code=409, detail="Запрос уже отправлен или остановлен.")
        profile = store().get(body.profile_id)
        key = (body.session_id, *asdict(profile).values(), tuple(body.sensitive))
        privacy = app.state.contexts.get(key)
        if privacy is None:
            privacy = PrivacyContext(local=profile.local, vision=profile.vision,
                session_id=body.session_id, profile_id=profile.id, sensitive=body.sensitive)
            app.state.contexts[key] = privacy
        provider = provider_for(profile, privacy=privacy)
        try:
            run = agent().start(session_id=body.session_id, provider=provider,
                                message=body.message, max_calls=body.max_calls)
        except (AgentBusy, RuntimeError) as error:
            await provider.aclose()
            raise HTTPException(status_code=409, detail=str(error)) from None
        except Exception:
            await provider.aclose()
            raise
        app.state.providers[run.id] = provider
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
            raise HTTPException(status_code=422, detail="Некорректный ID запроса.")
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
            raise HTTPException(status_code=409, detail="Подтверждение устарело или имеет другой ID.") from None
        return agent().get(run_id).snapshot()

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
        run = agent().get(run_id)
        try:
            cursor = int(request.headers.get("last-event-id", str(after)))
            if cursor < 0 or cursor > len(run.events):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=422, detail="Некорректный номер события.") from None

        async def stream():
            iterator = agent().events(run_id, after=cursor)
            try:
                async for event in iterator:
                    yield {"id": str(event["seq"]), "event": "update",
                           "data": json.dumps(event, ensure_ascii=False)}
            finally:
                await iterator.aclose()
                agent().stop(run_id)

        async def disconnected(message):
            agent().stop(run_id)

        return EventSourceResponse(stream(), ping=10, send_timeout=15,
                                   client_close_handler_callable=disconnected)

    @app.get("/")
    async def index():
        path = ui_dir / "index.html"
        if not path.is_file():
            return JSONResponse({"detail": "Интерфейс не собран. Выполните npm ci и npm run build в ui/."},
                                status_code=503)
        return FileResponse(path)

    if (ui_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ui_dir / "assets"), name="assets")
    return app
