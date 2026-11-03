"""Minimal JSON-schema subset validator. Returns a list of error strings
prefixed with a JSON pointer (e.g. "/lines/0/qty: must be >= 1")."""
import re
from datetime import date, datetime

_FRACTION = re.compile(r"(\.\d{6})\d+")

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _esc(key) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def _valid_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return len(s) == 10


def _valid_datetime(s: str) -> bool:
    # Python 3.10 fromisoformat: no "Z" suffix, at most 6 fraction digits.
    norm = _FRACTION.sub(r"\1", s)
    if norm.endswith(("Z", "z")):
        norm = norm[:-1] + "+00:00"
    try:
        datetime.fromisoformat(norm)
    except ValueError:
        return False
    return "T" in s.upper()


def schema_issues(schema, path: str = "", typed_parent: bool = False) -> list:
    """Portability problems in a tool's input schema, as ``"<pointer>: <problem>"`` strings.

    Strict LLM tool APIs (Gemini, OpenAI) reject an array without ``items`` and a schema node without a
    ``type``. An ``anyOf`` / ``oneOf`` node gives the types in its branches; branches of a typed node
    (e.g. ``{"type": "string", "oneOf": [{"const": ...}]}``) inherit its type.
    """
    issues: list = []
    where = path or "/"
    if not isinstance(schema, dict):
        return [f"{where}: is not a schema object"]
    keyword = "anyOf" if schema.get("anyOf") else "oneOf"
    branches = schema.get(keyword)
    if "type" not in schema and not branches and not typed_parent:
        issues.append(f"{where}: has no type")
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if "array" in types:
        if "items" not in schema:
            issues.append(f"{where}: array without items")
        else:
            issues += schema_issues(schema["items"], f"{path}/items")
    for name, sub in (schema.get("properties") or {}).items():
        issues += schema_issues(sub, f"{path}/properties/{_esc(name)}")
    for i, sub in enumerate(branches or []):
        issues += schema_issues(sub, f"{path}/{keyword}/{i}", typed_parent="type" in schema or typed_parent)
    return issues


def validate(instance, schema: dict) -> list:
    errors: list = []
    _validate(instance, schema, "", errors)
    return errors


def _branch_errors(value, schema: dict, path: str) -> list:
    errors: list = []
    _validate(value, schema, path, errors)
    return errors


def _validate(value, schema: dict, path: str, errors: list) -> None:
    where = path or "/"

    def fail(msg):
        errors.append(f"{where}: {msg}")

    branches = schema.get("anyOf")
    if branches and not any(not _branch_errors(value, b, path) for b in branches):
        fail("must match one of the allowed forms")
        return

    t = schema.get("type")
    if t is not None:
        names = t if isinstance(t, list) else [t]
        if not any(_TYPES[n](value) for n in names if n in _TYPES):
            fail("must be of type " + " or ".join(names))
            return

    if "enum" in schema and value not in schema["enum"]:
        fail("must be one of " + ", ".join(str(e) for e in schema["enum"]))

    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}/{_esc(name)}: is required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errors.append(f"{path}/{_esc(name)}: is not allowed")
        for name, sub in props.items():
            if name in value:
                _validate(value[name], sub, f"{path}/{_esc(name)}", errors)

    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            fail(f"must have at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            fail(f"must have at most {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _validate(item, schema["items"], f"{path}/{i}", errors)

    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            fail(f"must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            fail(f"must be at most {schema['maxLength']} characters")
        fmt = schema.get("format")
        if fmt == "date" and not _valid_date(value):
            fail("must be a date (YYYY-MM-DD)")
        elif fmt == "date-time" and not _valid_datetime(value):
            fail("must be a date-time (ISO 8601)")

    elif _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            fail(f"must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            fail(f"must be <= {schema['maximum']}")
