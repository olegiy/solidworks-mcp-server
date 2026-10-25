"""Public OpenRouter model metadata, independent of configured provider credentials."""

import asyncio
import time
from copy import deepcopy

from cadnext.provider import ProviderError


class ReasoningCatalog:
    def __init__(self, load, *, ttl=3600, clock=time.monotonic):
        self._load = load
        self._ttl = ttl
        self._clock = clock
        self._expires = 0
        self._models = None
        self._lock = asyncio.Lock()

    async def lookup(self, model: str) -> dict:
        async with self._lock:
            if self._clock() >= self._expires:
                try:
                    self._models = (await self._load())["models"]
                except (ProviderError, TimeoutError):
                    self._models = None
                self._expires = self._clock() + (self._ttl if self._models is not None else 15)
            if self._models is None:
                return {"status": "unavailable", "model_id": None, "reasoning": None}
            matches = [item for item in self._models if item["id"] == model]
            if not matches and "/" not in model:
                matches = [item for item in self._models if item["id"].rsplit("/", 1)[-1] == model]
            if len(matches) != 1:
                return {"status": "ambiguous" if matches else "not_found", "model_id": None,
                        "reasoning": None}
            item = matches[0]
            return {"status": "matched", "model_id": item["id"], "reasoning": deepcopy(item["reasoning"])}
