"""One OpenAI-compatible native-tools transport with no retries or routing."""

import json
from typing import get_args
from urllib.parse import urlsplit

import httpx

from cadnext.privacy import PrivacyContext
from cadnext.settings import Profile, ReasoningEffort


class ProviderError(RuntimeError):
    pass


def _invalid_json_constant(value):
    raise ValueError("Недопустимая константа JSON.")


class Provider:
    def __init__(self, profile: Profile, secret: str | None = None, *,
                 privacy: PrivacyContext | None = None, transport=None):
        self.profile = profile
        self.privacy = privacy or PrivacyContext(local=profile.local, vision=profile.vision,
                                                  profile_id=profile.id)
        if self.privacy.local != profile.local or (self.privacy.vision and not profile.vision) or (
                self.privacy.profile_id and self.privacy.profile_id != profile.id):
            raise ValueError("Контекст приватности не соответствует подключению.")
        self.privacy.bind_profile(profile.id, base_url=profile.base_url, model=profile.model)
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
            raise ProviderError("Не удалось связаться с API модели. Проверьте адрес и соединение.") from None
        if not response.is_success:
            if response.status_code in {400, 422}:
                body = response.text.lower()
                if any(word in body for word in ("tool", "function_call")):
                    raise ProviderError("API отклонил вызовы инструментов. Нужна поддержка native tools.")
            raise ProviderError(f"API модели вернул HTTP {response.status_code}. Проверьте подключение.")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("API модели вернул некорректный JSON.") from None

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
            raise ProviderError("Не удалось прочитать список моделей; имя можно ввести вручную.") from None

    async def list_models(self) -> list[str]:
        catalog = await self.list_model_catalog()
        return sorted(model["id"] for model in catalog["models"])

    async def complete(self, messages, tools) -> dict:
        payload = {"model": self.profile.model, "messages": self.privacy.outgoing_messages(messages)}
        if self.profile.reasoning_effort is not None:
            payload["reasoning_effort"] = self.profile.reasoning_effort
        if tools:
            payload["tools"] = self.privacy.outgoing_tools(tools)
            payload["tool_choice"] = "auto"
        data = await self._request("POST", "chat/completions", json=payload)
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ProviderError("Ответ модели прерван или контекст исчерпан; запрос остановлен.")
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
            raise ProviderError("API вернул некорректный ответ или вызов инструмента.") from None
