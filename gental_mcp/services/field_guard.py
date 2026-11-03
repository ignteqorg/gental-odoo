"""Which fields of an exposed model assistants may see (spec §6.2)."""
import re

SECRET_FIELD_RE = r"(^password$|_token$|_secret$|^api_key|^totp_|^signup_|^oauth_access_token$)"
_SECRET = re.compile(SECRET_FIELD_RE)
_ATTRIBUTES = ["type", "string", "required", "readonly", "selection", "relation"]


def _allowed(records, described, names, hidden) -> list:
    """Names from ``names`` (all of ``described`` when None) that are known, not hidden, not secret-like
    and not binary. ``described`` is ``fields_get`` for the acting user, so it already lacks the fields
    that user may not read (field ``groups``).

    Known means a real field of the model: before 19.0, ``res.users.fields_get`` also lists the
    ``in_group_*`` / ``sel_groups_*`` pseudo-fields of the user form, which are not in ``_fields``.
    """
    candidates = described if names is None else names
    model_fields = records._fields
    return [
        name for name in candidates
        if name in described and name in model_fields and name not in hidden and not _SECRET.search(name)
        and described[name].get("type") != "binary"
    ]


def filter_fields(env, model: str, names=None, hidden=frozenset()) -> list:
    """The fields of ``model`` that ``env.user`` may see, from ``names`` (all fields when None).

    Drops unknown names, the admin's ``hidden`` fields, secret-like names, binary fields and fields the
    user cannot read. Order follows ``names``.
    """
    records = env[model]
    return _allowed(records, records.fields_get(attributes=["type"]), names, hidden)


def readable_fields(env, exposed) -> dict:
    """``{name: {type, string, required, readonly, selection?, relation?}}`` for ``exposed.model``.

    The fields kept are the ones ``filter_fields`` keeps for the acting user (``fields_get`` already
    applies field ``groups`` for ``env.user``). ``readonly`` is also true where the user lacks write
    access. Labels follow ``env.lang``.
    """
    records = env[exposed.model]
    described = records.fields_get(attributes=_ATTRIBUTES)
    out = {}
    for name in _allowed(records, described, None, exposed.hidden):
        info = described[name]
        item = {
            "type": info.get("type"),
            "string": info.get("string") or name,
            "required": bool(info.get("required")),
            "readonly": bool(info.get("readonly")),
        }
        if isinstance(info.get("selection"), (list, tuple)):
            item["selection"] = [[value, label] for value, label in info["selection"]]
        if info.get("relation"):
            item["relation"] = info["relation"]
        out[name] = item
    return out


RELATIONAL_TYPES = frozenset({"many2one", "many2many", "one2many"})


def writable_fields(env, exposed, readable=None) -> set:
    """Readable fields the user may set: not read-only, and not one2many (child records stay out of reach)."""
    readable = readable_fields(env, exposed) if readable is None else readable
    return {
        name for name, info in readable.items()
        if not info["readonly"] and info["type"] != "one2many"
    }


def filterable_fields(env, exposed, readable=None) -> dict:
    """Readable fields usable in domains and ``order``: stored ones and ``id``.

    Non-stored fields (``display_name`` included) search through methods that may look at hidden fields.
    """
    readable = readable_fields(env, exposed) if readable is None else readable
    fields = env[exposed.model]._fields
    return {name: info for name, info in readable.items() if name == "id" or fields[name].store}
