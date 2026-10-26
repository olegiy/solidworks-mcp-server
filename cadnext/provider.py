"""One OpenAI-compatible native-tools transport with no retries or routing."""

import asyncio
import json
import math
import time
from typing import get_args
from urllib.parse import urlsplit

import httpx

from cadnext.context import model_context
from cadnext.i18n import t
from cadnext.privacy import PrivacyContext
from cadnext.settings import Profile, ReasoningEffort
from cadnext.streaming import CompletionStream


class ProviderError(RuntimeError):
    pass


def _invalid_json_constant(value):
    raise ValueError(t("Недопустимая константа JSON.", "Invalid JSON constant."))


class Provider:
    def __init__(self, profile: Profile, secret: str | None = None, *,
                 privacy: PrivacyContext | None = None, transport=None):
        self.profile = profile
        self.privacy = privacy or PrivacyContext(local=profile.local, vision=profile.vision,
                                                  profile_id=profile.id)
        if self.privacy.local != profile.local or (self.privacy.vision and not profile.vision) or (
                self.privacy.profile_id and self.privacy.profile_id != profile.id):
            raise ValueError(t("Контекст приватности не соответствует подключению.",
                               "The privacy context does not match the connection."))
        self.privacy.bind_profile(profile.id, base_url=profile.base_url, model=profile.model)
        self.last_metrics = None
        self._image_cache = {}
        self._completion_slots = asyncio.Semaphore(2)
        self._client = httpx.AsyncClient(
            base_url=profile.base_url + "/", transport=transport, trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(120.0, connect=10.0),
            headers={"Authorization": f"Bearer {secret}"} if secret else {},
        )

    async def aclose(self):
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs):
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError:
            raise ProviderError(t("Не удалось связаться с API модели. Проверьте адрес и соединение.",
                                  "Could not reach the model API. Check the address and connection.")) from None
        if not response.is_success:
            if response.status_code in {400, 422}:
                body = response.text.lower()
                if any(word in body for word in ("tool", "function_call")):
                    raise ProviderError(t("API отклонил вызовы инструментов. Нужна поддержка native tools.",
                                          "The API rejected tool calls. Native tool support is required."))
            raise ProviderError(t(f"API модели вернул HTTP {response.status_code}. Проверьте подключение.",
                                  f"The model API returned HTTP {response.status_code}. Check the connection."))
        try:
            return response.json()
        except ValueError:
            raise ProviderError(t("API модели вернул некорректный JSON.",
                                  "The model API returned invalid JSON.")) from None

    def _is_openrouter(self) -> bool:
        return urlsplit(self.profile.base_url).hostname == "openrouter.ai"

    @staticmethod
    def _reasoning_metadata(item: dict) -> dict | None:
        source = item.get("reasoning")
        if not isinstance(source, dict):
            return None
        allowed = get_args(ReasoningEffort)
        supported = []
        values = source.get("supported_efforts", [])
        if not isinstance(values, list):
            values = []
        for value in values:
            if isinstance(value, str) and value in allowed and value not in supported:
                supported.append(value)
        mandatory = source.get("mandatory") is True
        if mandatory:
            supported = [value for value in supported if value != "none"]
        return {
            "mandatory": mandatory,
            "default_effort": source.get("default_effort") if source.get("default_effort") in supported else None,
            "supported_efforts": supported or None,
        }

    async def list_model_catalog(self) -> dict:
        data = await self._request("GET", "models")
        try:
            items = data["data"]
            if not isinstance(items, list):
                raise ValueError
            models = []
            seen = set()
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                    raise ValueError
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                models.append({"id": item["id"], "reasoning":
                               self._reasoning_metadata(item) if self._is_openrouter() else None})
            return {"models": models}
        except (KeyError, TypeError, ValueError):
            raise ProviderError(t("Не удалось прочитать список моделей; имя можно ввести вручную.",
                                  "Could not read the model list; you can enter the name manually.")) from None

    async def list_models(self) -> list[str]:
        catalog = await self.list_model_catalog()
        return sorted(model["id"] for model in catalog["models"])

    async def complete(self, messages, tools, *, stream=False, compact=True) -> dict:
        queued = time.perf_counter()
        async with self._completion_slots:
            started = time.perf_counter()
            metrics = {"request_bytes": 0, "image_count": 0,
                "input_tokens": None, "output_tokens": None, "cached_tokens": None, "cost": None,
                "first_token_seconds": None, "duration_seconds": 0.0,
                "queue_seconds": started - queued}
            try:
                context = model_context(messages, image_cache=self._image_cache) if compact else [
                    {key: value for key, value in message.items() if key != "_cadnext"}
                    for message in messages]
                payload = {"model": self.profile.model,
                           "messages": self.privacy.outgoing_messages(context), "stream": stream}
                if self.profile.reasoning_effort is not None:
                    payload["reasoning_effort"] = self.profile.reasoning_effort
                if tools:
                    payload["tools"] = self.privacy.outgoing_tools(tools)
                    payload["tool_choice"] = "auto"
                if stream:
                    payload["stream_options"] = {"include_usage": True}
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                  separators=(",", ":")).encode("utf-8")
                metrics["request_bytes"] = len(body)
                metrics["image_count"] = sum(
                    block.get("type") == "image_url" for message in payload["messages"]
                    if isinstance(message.get("content"), list) for block in message["content"])
                metrics["preparation_seconds"] = time.perf_counter() - started
                if stream:
                    data = await self._stream_request(body, metrics, started)
                else:
                    data = await self._request("POST", "chat/completions", content=body,
                                               headers={"Content-Type": "application/json"})
                self._usage_metrics(data, metrics)
                return self._completion_message(data)
            finally:
                metrics["duration_seconds"] = time.perf_counter() - started
                self.last_metrics = metrics

    @staticmethod
    def _usage_metrics(data, metrics):
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        details = usage.get("prompt_tokens_details")
        values = {"input_tokens": usage.get("prompt_tokens"),
                  "output_tokens": usage.get("completion_tokens"),
                  "cached_tokens": details.get("cached_tokens") if isinstance(details, dict) else None}
        for key, value in values.items():
            if type(value) is int and value >= 0:
                metrics[key] = value
        cost = usage.get("cost")
        if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
            metrics["cost"] = cost

    async def _stream_request(self, body, metrics, started):
        accumulated = CompletionStream()
        pending = []
        size = 0
        try:
            async with self._client.stream("POST", "chat/completions", content=body,
                    headers={"Content-Type": "application/json"}) as response:
                if not response.is_success:
                    raise ProviderError(t(f"API модели вернул HTTP {response.status_code}.",
                                          f"The model API returned HTTP {response.status_code}."))
                async for line in response.aiter_lines():
                    size += len(line.encode("utf-8"))
                    if size > 8 * 1024 * 1024:
                        raise ValueError("Stream too large")
                    if line.startswith("data:"):
                        pending.append(line[5:].lstrip(" "))
                    elif not line and pending:
                        generated = accumulated.feed("\n".join(pending))
                        pending.clear()
                        if generated and metrics["first_token_seconds"] is None:
                            metrics["first_token_seconds"] = time.perf_counter() - started
                        if accumulated.done:
                            break
            return accumulated.result()
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError(t("API вернул незавершённый или некорректный поток ответа.",
                                  "The API returned an incomplete or invalid response stream.")) from None
        finally:
            self._usage_metrics({"usage": accumulated.usage}, metrics)

    @staticmethod
    def _completion_message(data):
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProviderError(t("Ответ модели прерван или контекст исчерпан; запрос остановлен.",
                                      "The model response was interrupted or the context was exhausted; "
                                      "the request was stopped."))
            message = choice["message"]
            content = message.get("content")
            calls = message.get("tool_calls") or []
            if content is not None and not isinstance(content, str):
                raise ValueError
            if not isinstance(calls, list) or (not calls and content is None):
                raise ValueError
            ids = set()
            for call in calls:
                if not isinstance(call["id"], str) or not call["id"] or call["id"] in ids:
                    raise ValueError
                ids.add(call["id"])
                function = call["function"]
                if (call["type"] != "function" or not isinstance(function["name"], str)
                        or not function["name"] or not isinstance(function["arguments"], str)
                        or not isinstance(json.loads(function["arguments"],
                                                     parse_constant=_invalid_json_constant), dict)):
                    raise ValueError
            return {"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise ProviderError(t("API вернул некорректный ответ или вызов инструмента.",
                                  "The API returned an invalid response or tool call.")) from None
