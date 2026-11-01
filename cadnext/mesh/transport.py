"""Stdlib-only, bounded, immutable JSON result transport."""

import base64
import hashlib
import json
from uuid import uuid4

IPC_LIMIT = 1024*1024
PART_BYTES = 512*1024
RESULT_LIMIT = 64*1024*1024


def encode_json(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":")).encode()


class ResultTransport:
    def __init__(self):
        self.result = None

    def encode(self, response):
        raw = encode_json(response)
        if len(raw)+1 <= IPC_LIMIT:
            return raw+b"\n"
        envelope = {key: response[key] for key in ("request_id", "sha256", "frame_id")}
        if len(raw) > RESULT_LIMIT:
            self.result = None
            return encode_json({**envelope, "error": {"code": "result_limit",
                               "message": "Полный результат анализа превышает 64 МиБ."}})+b"\n"
        descriptor = {"id": uuid4().hex, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                      "parts": (len(raw)+PART_BYTES-1)//PART_BYTES}
        self.result = (descriptor, raw, envelope["sha256"], envelope["frame_id"])
        return encode_json({**envelope, "artifact": descriptor})+b"\n"

    def page(self, payload):
        if self.result is None:
            raise ValueError("Missing result")
        descriptor, raw, sha, frame = self.result
        index = payload.get("index")
        if (payload.get("result_id") != descriptor["id"] or type(index) is not int
                or not 0 <= index < descriptor["parts"] or payload.get("sha256") != sha
                or payload.get("frame_id") != frame):
            raise ValueError("Invalid result page")
        chunk = raw[index*PART_BYTES:(index+1)*PART_BYTES]
        return {"part": {"id": descriptor["id"], "index": index,
                         "data": base64.b64encode(chunk).decode("ascii")}}
