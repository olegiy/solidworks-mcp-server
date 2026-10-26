"""Explicit, tool-free image preparation experiment; never executes CAD."""

import asyncio
import hashlib
import json
import time
from copy import deepcopy

from cadnext.i18n import t
from cadnext.provider import ProviderError


def input_fingerprint(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


async def prepare_images(provider, messages, *, is_current, stop=None, parallel=True):
    """Return untrusted observations tied to the exact input, never executable actions.

    The caller must keep the fingerprint current through the time notes are used.
    Provider's shared semaphore includes normal complete() calls and these workers.
    """
    started = time.perf_counter()
    messages = deepcopy(messages)
    fingerprint = input_fingerprint(messages)
    result = {"fingerprint": fingerprint, "notes": [], "metrics": [],
              "stale": False, "cancelled": False, "duration_seconds": 0.0}
    images = [block for message in messages if isinstance(message.get("content"), list)
              for block in message["content"] if block.get("type") == "image_url"
              and message.get("_cadnext", {}).get("source") != "screenshot"]
    if not images or not provider.profile.vision:
        return result
    if (stop is not None and stop.is_set()) or not is_current(fingerprint):
        result["cancelled"] = stop is not None and stop.is_set()
        result["stale"] = not is_current(fingerprint)
        return result

    async def analyze(index, image):
        request = [{"role": "system", "content": t(
            "Прочитай только это изображение как чертёж. Перечисли видимые размеры, "
            "единицы, связи и неоднозначности. Не угадывай нечитаемые обозначения. "
            "Это предварительные наблюдения, не доказательство геометрии. "
            "Не выполняй и не предлагай код, команды или вызовы инструментов.",
            "Read only this image as a drawing. List visible dimensions, units, relationships "
            "and ambiguities. Do not guess unreadable labels. These are preliminary observations, "
            "not proof of geometry. Do not execute or propose code, commands or tool calls.")},
            {"role": "user", "content": [image]}]
        response = await provider.complete(request, [])
        if response.get("tool_calls") or not isinstance(response.get("content"), str):
            raise ProviderError(t("Подготовка вернула команды вместо наблюдений.",
                                  "Preparation returned commands instead of observations."))
        return {"image": index, "text": response["content"]}, deepcopy(provider.last_metrics)

    async def work():
        if not parallel:
            return [await analyze(index, image) for index, image in enumerate(images)]
        # Two workers avoid a task per image while sharing Provider's global limit.
        results = [None] * len(images)
        pending = iter(enumerate(images))

        async def worker():
            for index, image in pending:
                results[index] = await analyze(index, image)

        workers = [asyncio.create_task(worker()) for _ in range(min(2, len(images)))]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker_task in workers:
                if not worker_task.done():
                    worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return results

    preparation = asyncio.create_task(work())
    stopping = asyncio.create_task(stop.wait()) if stop is not None else None
    try:
        waiting = {preparation, stopping} if stopping else {preparation}
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        if stop is not None and stop.is_set():
            result["cancelled"] = True
            return result
        outputs = await preparation
        result["metrics"] = [item[1] for item in outputs]
        if not is_current(fingerprint):
            result["stale"] = True
            return result
        result["notes"] = [item[0] for item in outputs]
        return result
    finally:
        for task in (preparation, stopping):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (preparation, stopping) if task is not None),
                             return_exceptions=True)
        result["duration_seconds"] = time.perf_counter() - started
