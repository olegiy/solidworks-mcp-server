"""Deterministic model-only projection of the full, authoritative local history."""

import json
from copy import deepcopy
from difflib import SequenceMatcher

from cadnext.images import optimize_image_url


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _failed(value):
    if isinstance(value, dict):
        return (value.get("ok") is False or value.get("status") in {"error", "failed"}
                or any(_failed(item) for item in value.values()))
    return isinstance(value, list) and any(_failed(item) for item in value)


class _Trees:
    def __init__(self):
        self.previous = None
        self.number = 0

    def compact(self, value):
        if isinstance(value, list):
            return [self.compact(item) for item in value]
        if not isinstance(value, dict):
            return value
        tree = value.get("features")
        if isinstance(tree, list) and all(isinstance(item, dict) and "name" in item for item in tree):
            result = {key: self.compact(item) for key, item in value.items() if key != "features"}
            self.number += 1
            full = {"features": tree, "features_observation": self.number}
            if self.previous is not None:
                matcher = SequenceMatcher(a=[_json(item) for item in self.previous],
                                          b=[_json(item) for item in tree], autojunk=False)
                edits = [{"start": i, "delete": end - i, "insert": tree[j:stop]}
                         for kind, i, end, j, stop in matcher.get_opcodes() if kind != "equal"]
                delta = {"features_delta": {"base": self.number - 1, "id": self.number,
                                            "edits": edits}}
                if len(_json(delta)) < len(_json(full)):
                    full = delta
            self.previous = tree
            return {**result, **full}
        return {key: self.compact(item) for key, item in value.items()}


def model_context(messages, *, image_cache=None):
    """Retain requirements and protocol; supersede only application-marked CAD views.

    features_delta references the previous numbered observation. Apply its edits
    in descending start order to reconstruct the exact features array.
    """
    result = deepcopy(messages)
    latest = max((index for index, message in enumerate(result)
                  if message.get("_cadnext", {}).get("source") == "screenshot"), default=-1)
    trees = _Trees()
    for index, message in enumerate(result):
        metadata = message.pop("_cadnext", {})
        content = message.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if block.get("type") == "image_url":
                    if metadata.get("source") == "screenshot":
                        if index != latest:
                            continue
                    else:
                        url = block["image_url"]["url"]
                        if image_cache is None:
                            optimized = optimize_image_url(url)
                        else:
                            if url not in image_cache:
                                if len(image_cache) >= 8:
                                    image_cache.pop(next(iter(image_cache)))
                                image_cache[url] = optimize_image_url(url)
                            optimized = image_cache[url]
                        block["image_url"]["url"] = optimized
                blocks.append(block)
            message["content"] = blocks or [{"type": "text", "text":
                "[Earlier CAD view superseded by the latest view below.]"}]
        elif message.get("role") == "tool" and isinstance(content, str):
            try:
                value = json.loads(content)
                if not _failed(value):
                    message["content"] = _json(trees.compact(value))
            except (ValueError, TypeError):
                pass
    return result
