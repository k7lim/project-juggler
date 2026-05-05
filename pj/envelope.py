from __future__ import annotations

import json
from typing import Any


def ok(data: list | dict, **meta: Any) -> dict:
    return {"success": True, "data": data, "meta": meta}


def err(error: str, **meta: Any) -> dict:
    return {"success": False, "data": [], "meta": {**meta, "error": error}}


def to_json(envelope: dict, indent: int | None = None) -> str:
    return json.dumps(envelope, indent=indent, default=str)
