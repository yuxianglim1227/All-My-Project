"""A small JSON Schema checker for the subset our schemas use, plus a forgiving JSON extractor.

Used for every AI answer that is not guaranteed by the provider: Gemini replies and text pasted back from a chat app.
Keywords: type (incl. lists), enum, required, properties, additionalProperties (false), items, minItems/maxItems,
minLength/maxLength, minimum/maximum. Unknown keywords are ignored.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPES.get(t)
    return True if py is None else isinstance(value, py)


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return human-readable problems ([] = valid)."""
    errs: list[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is(value, x) for x in types):
            return [f"{path}: expected {' or '.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errs.append(f"{path}: missing '{key}'")
        for key, sub in props.items():
            if key in value:
                errs += validate(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                errs.append(f"{path}: unexpected field(s) {', '.join(extra)}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: at most {schema['maxItems']} item(s)")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errs += validate(item, schema["items"], f"{path}[{i}]")
    return errs


_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Parse JSON out of a chat reply: plain JSON, a ```json fenced block, or the first {...}/[...] span.

    Raises ValueError with a message the owner can act on.
    """
    text = (text or "").strip().lstrip("﻿")
    if not text:
        raise ValueError("The reply is empty. Paste the whole answer from the chat app.")
    candidates = [text] + [m.group(1) for m in _FENCE.finditer(text)]
    for open_, close in (("{", "}"), ("[", "]")):
        a, b = text.find(open_), text.rfind(close)
        if 0 <= a < b:
            candidates.append(text[a:b + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise ValueError("I couldn't find valid JSON in that reply. Ask the chat app to \"reply with only the JSON\" "
                     "and paste again, including the first { and the last }.")
