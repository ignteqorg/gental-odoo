"""Generic model tools over the admin allowlist (spec §6.2).

Everything runs as the acting user (``ctx.env``): their ACLs, record rules and field groups apply, and
nothing here raises privileges. The allowlist row is re-read on every call, so a switch turned off takes effect
on the next call even when the assistant still holds an older tool listing.
"""
import base64
import binascii
import datetime
import inspect
import json
import re

import psycopg2

from odoo import _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.models import BaseModel

from ..compat import can_read_model, domain_and, is_model_method
from ..core.tools import ToolAnnotations, ToolDenied, ToolError, ToolResult, ToolSpec
from ..models.exposed_model import FORBIDDEN_METHODS, is_rpc_callable
from . import domain_guard, field_guard
from .action_keys import once_per_key

MAX_RECORDS = 200
DEFAULT_LIMIT = 20
MAX_WRITE_IDS = 50
MAX_RESULT_BYTES = 1024 * 1024
# Search/read results are cut to about this size (whole records), so one call can't flood the model's context.
MAX_RECORDS_TEXT_BYTES = 16 * 1024
# Fields returned when the caller names none: display_name plus the first of these present on the model.
DEFAULT_FIELDS_MAX = 8
KEY_FIELDS = (
    "name", "login", "ref", "default_code", "email", "phone", "mobile", "state", "date", "date_order",
    "invoice_date", "date_deadline", "amount_total", "amount_residual", "list_price", "partner_id",
)
ODOO_ERRORS = (AccessError, UserError, ValidationError)
_ORDER_TERM = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s+(asc|desc))?\s*$", re.IGNORECASE)
_SEVERITY = {"read": 0, "write": 1, "destructive": 2}

# tool name -> allowlist operation
OPERATIONS = {
    "odoo_describe_model": None,  # any exposed model: callers need the fields to create or call methods
    "odoo_search": "read",
    "odoo_read": "read",
    "odoo_create": "create",
    "odoo_update": "write",
    "odoo_delete": "unlink",
}
CALL_METHOD = "odoo_call_method"

_IDS = {"type": "array", "items": {"type": "integer"}}
_FIELDS = {"type": "array", "items": {"type": "string"},
           "description": "Field names (see odoo_describe_model). Empty: display_name and a few key fields."}
_VALUES = {"type": "object", "description": "Field values: scalars; many2many fields take a list of ids."}
_LEAF_VALUE = {"anyOf": [
    {"type": "string"}, {"type": "number"}, {"type": "boolean"},
    {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
]}
_DOMAIN = {
    "type": "array",
    "description": "Odoo domain over the model's own fields, e.g. [[\"name\", \"ilike\", \"acme\"]]; "
                   "no dotted paths. Items are [field, operator, value] or \"&\", \"|\", \"!\".",
    "items": {"anyOf": [
        {"type": "string", "enum": ["&", "|", "!"]},
        {"type": "array", "items": _LEAF_VALUE, "minItems": 3, "maxItems": 3},
    ]},
}


# Added to every generic tool's description: the model titles are how the assistant tells the models apart.
_PICK_MODEL = " Pick the model whose title matches the request; call odoo_describe_model first when unsure."


def model_title(rule):
    """``"<label> (<technical name>): <description>"``, or without the description when there is none.

    Label and description are already one clean line each (``ExposedRule``); the admin's description is
    the only free text reaching the model here.
    """
    title = f"{rule.label or rule.model} ({rule.model})"
    return f"{title}: {rule.description}" if rule.description else title


def _model_schema(models, rules):
    """The ``model`` property: one ``const`` per exposed model with a title saying what it is (R18)."""
    return {"type": "string", "oneOf": [{"const": m, "title": model_title(rules[m])} for m in models]}


def _specs(model_enum, method_enum, method_kind, rules):
    """The spec of every generic tool, given the model enum per tool and the rules behind it."""
    out = []

    def add(name, title, description, properties, required, annotations):
        if name in model_enum:
            schema = {
                "type": "object",
                "properties": {"model": _model_schema(model_enum[name], rules), **properties},
                "required": ["model", *required],
                "additionalProperties": False,
            }
            out.append(ToolSpec(name, title, description + _PICK_MODEL, schema, annotations))

    add("odoo_describe_model", "Describe an Odoo model",
        "What an exposed Odoo model is (label and the administrator's description), its fields (name, "
        "type, label, required, read-only, selection values, relation) and the methods you may call. Use "
        "it before searching, creating or calling methods.",
        {}, [], ToolAnnotations(read_only=True, idempotent=True))
    add("odoo_search", "Search Odoo records",
        "Search records of an exposed model. Returns {records, next_cursor}; pass next_cursor back as "
        "cursor for the next page. Ask only for the fields you need: large results are cut short.",
        {"domain": _DOMAIN, "fields": _FIELDS,
         "order": {"type": "string", "description": "\"<field> asc|desc\", comma-separated for several."},
         "limit": {"type": "integer", "minimum": 1, "description": "Default 20, at most 200."},
         "cursor": {"type": "string"}},
        [], ToolAnnotations(read_only=True, idempotent=True))
    add("odoo_read", "Read Odoo records",
        "Read records of an exposed model by id. Ids you cannot see are left out.",
        {"ids": {**_IDS, "maxItems": MAX_RECORDS}, "fields": _FIELDS},
        ["ids"], ToolAnnotations(read_only=True, idempotent=True))
    add("odoo_create", "Create an Odoo record",
        "Create one record of an exposed model. Returns {id, display_name}.",
        {"values": _VALUES}, ["values"], ToolAnnotations(read_only=False))
    add("odoo_update", "Update Odoo records",
        "Set field values on records of an exposed model (at most 50 ids).",
        {"ids": {**_IDS, "maxItems": MAX_WRITE_IDS}, "values": _VALUES},
        ["ids", "values"], ToolAnnotations(read_only=False, idempotent=True))
    add("odoo_delete", "Delete Odoo records",
        "Delete records of an exposed model (at most 50 ids). This cannot be undone.",
        {"ids": {**_IDS, "maxItems": MAX_WRITE_IDS}},
        ["ids"], ToolAnnotations(read_only=False, destructive=True))
    if CALL_METHOD in model_enum:
        add(CALL_METHOD, "Call an Odoo method",
            "Run a method the administrator allowed on records of an exposed model (at most 50 ids; "
            "an empty list calls it on the model). odoo_describe_model lists the methods per model.",
            {"ids": {**_IDS, "maxItems": MAX_WRITE_IDS},
             "method": {"type": "string", "enum": method_enum},
             "kwargs": {"type": "object", "description": "Keyword arguments of the method."}},
            ["ids", "method"],
            ToolAnnotations(read_only=method_kind == "read", destructive=method_kind == "destructive"))
    return out


class GenericToolProvider:
    def __init__(self, env):
        self.env = env
        self._listed = None  # allowlist snapshot behind this request's listing

    # -- listing --------------------------------------------------------------------------------

    def list_tools(self, ctx):
        if self._listed is None:
            # Model labels in the request's language; one read of the allowlist for every tool.
            self._listed = localized_env(ctx)["gental.mcp.exposed.model"]._mcp_rules()
        rules = self._listed
        model_enum = {}
        for tool, operation in OPERATIONS.items():
            models = sorted(m for m, r in rules.items() if operation is None or r.allows(operation))
            if models:
                model_enum[tool] = models
        with_methods = {m: r for m, r in rules.items() if r.methods}
        method_enum, method_kind = [], "read"
        if with_methods:
            model_enum[CALL_METHOD] = sorted(with_methods)
            method_enum = sorted({name for r in with_methods.values() for name in r.methods})
            kinds = [kind for r in with_methods.values() for kind in r.methods.values()]
            method_kind = max(kinds, key=lambda k: _SEVERITY.get(k, 2))
        return _specs(model_enum, method_enum, method_kind, rules)

    # -- dispatch -------------------------------------------------------------------------------

    def call(self, name, args, ctx):
        env = localized_env(ctx)
        self.env = env  # the frame lookup of ``_()`` reads ``self.env.lang``
        if name != CALL_METHOD and name not in OPERATIONS:
            raise ToolError(_("Unknown tool: %s", name))
        model = args.get("model")
        rule = env["gental.mcp.exposed.model"]._mcp_rules(model).get(model) if isinstance(model, str) else None
        if rule is None:
            raise ToolDenied(_("The model %s is not available to assistants.", model))
        if name == CALL_METHOD:
            if args.get("method") not in rule.methods:
                raise ToolDenied(_("The method %(method)s is not allowed on %(model)s.",
                                   method=args.get("method"), model=model))
        else:
            operation = OPERATIONS[name]
            if operation is not None and not rule.allows(operation):
                raise ToolDenied(_("Assistants may not do this on %s.", model))
        handler = getattr(self, "_" + name)
        try:
            if name == "odoo_create":
                return handler(env, rule, args, ctx)
            return handler(env, rule, args)
        except AccessError as e:
            # Wall 3: the acting user's ACLs or record rules refused it. Logged as denied.
            raise ToolDenied(_odoo_message(e)) from e
        except ODOO_ERRORS as e:
            raise ToolError(_odoo_message(e)) from e
        except psycopg2.DataError as e:
            # A value of the wrong type reached SQL (e.g. text compared with a number); the savepoint
            # around the call restores the cursor.
            detail = (str(e).strip().splitlines() or [""])[0]
            raise ToolError(_("Invalid value: %s", detail)) from e

    # -- tools ----------------------------------------------------------------------------------

    def _odoo_describe_model(self, env, rule, args):
        payload = {
            "model": rule.model,
            "label": rule.label or rule.model,
            "description": rule.description or None,
            "fields": field_guard.readable_fields(env, rule),
            "methods": [{"name": n, "kind": k} for n, k in sorted(rule.methods.items())],
        }
        return _result(env, payload)

    def _odoo_search(self, env, rule, args):
        readable = field_guard.readable_fields(env, rule)
        filterable = field_guard.filterable_fields(env, rule, readable)
        relational = {n for n, info in filterable.items() if info["type"] in field_guard.RELATIONAL_TYPES}
        domain = args.get("domain") or []
        domain_guard.validate(domain, set(filterable), env, relational)
        fields = self._fields(env, rule, readable, args.get("fields"))
        order = self._order(filterable, relational, args.get("order"))
        limit = max(1, min(int(args.get("limit") or DEFAULT_LIMIT), MAX_RECORDS))
        offset = self._decode_cursor(args.get("cursor"))
        Model = env[rule.model]
        try:
            records = Model.search(domain_and(domain, rule.extra_domain), offset=offset, limit=limit + 1, order=order)
        except ValueError as e:
            raise ToolError(str(e)) from e
        more = len(records) > limit
        rows = records[:limit].read(fields)
        rows, truncated = _fit_records(rows)
        more = more or truncated
        next_offset = offset + len(rows)
        next_cursor = base64.urlsafe_b64encode(str(next_offset).encode()).decode() if more else None
        payload = {"records": rows, "next_cursor": next_cursor}
        if truncated:
            payload["note"] = _truncated_note(env)
        return _result(env, payload)

    def _odoo_read(self, env, rule, args):
        readable = field_guard.readable_fields(env, rule)
        fields = self._fields(env, rule, readable, args.get("fields"))
        ids = args["ids"][:MAX_RECORDS]
        records = _visible(env, rule, ids)
        by_id = {row["id"]: row for row in records.read(fields)}
        rows = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        kept, truncated = _fit_records(rows)
        payload = {"records": kept}
        if truncated:
            payload["omitted_ids"] = [row["id"] for row in rows[len(kept):]]
            payload["note"] = _truncated_note(env)
        return _result(env, payload)

    def _odoo_create(self, env, rule, args, ctx):
        values = self._values(env, rule, args["values"])

        def create():
            record = env[rule.model].create(values)
            env.flush_all()
            return record

        # A retried action (same Gental action key) returns the record it already created.
        record, created = once_per_key(ctx, rule.model, create)
        record = record.with_env(env)
        if created:
            self._still_inside(env, rule, record)
        elif not _visible(env, rule, record.ids):
            raise ToolError(_("The record this action created is no longer available."))
        result = _result(env, {"id": record.id, "display_name": record.display_name, "created": created})
        result.record = (rule.model, record.id)
        return result

    def _odoo_update(self, env, rule, args):
        records = self._all_visible(env, rule, args["ids"])
        values = self._values(env, rule, args["values"], records)
        records.write(values)
        env.flush_all()
        self._still_inside(env, rule, records)
        result = _result(env, {"updated": records.ids})
        if len(records) == 1:
            result.record = (rule.model, records.id)
        return result

    def _odoo_delete(self, env, rule, args):
        records = self._all_visible(env, rule, args["ids"])
        ids = records.ids
        records.unlink()
        env.flush_all()
        return _result(env, {"deleted": ids})

    def _odoo_call_method(self, env, rule, args):
        name = args["method"]
        Model = env[rule.model]
        # Re-check at call time: the method may have become private or forbidden since it was allowed.
        if name.startswith("_") or name in FORBIDDEN_METHODS or not is_rpc_callable(Model, name):
            raise ToolDenied(_("The method %(method)s cannot be called on %(model)s.", method=name, model=rule.model))
        if args["ids"]:
            records = self._all_visible(env, rule, args["ids"])
        elif is_model_method(getattr(type(Model), name)):
            records = Model
        else:
            raise ToolError(_("The method %s works on records: pass their ids.", name))
        method = getattr(records, name)
        kwargs = args.get("kwargs") or {}
        signature = inspect.signature(method)
        try:
            bound = signature.bind(**kwargs)
        except TypeError as e:
            raise ToolError(_("Invalid arguments for %(method)s: %(error)s", method=name, error=str(e))) from e
        extra = [p.name for p in signature.parameters.values()
                 if p.kind is inspect.Parameter.VAR_KEYWORD and bound.arguments.get(p.name)]
        if extra:
            # Undeclared keywords reach **kwargs, where methods pass them on unchecked (e.g. to create()).
            unknown = sorted(bound.arguments[extra[0]])
            raise ToolError(_("Invalid arguments for %(method)s: unknown %(names)s", method=name, names=unknown))
        before = None if args["ids"] else _max_id(env, Model)
        value = method(**kwargs)
        env.flush_all()
        if args["ids"]:
            self._still_inside(env, rule, records.exists())
        elif before is not None:
            created = _created_since(env, Model, before)
            if created:
                self._still_inside(env, rule, created)
        result = _result(env, {"result": _safe(env, value, rule)})
        if len(records) == 1:
            result.record = (rule.model, records.id)
        return result

    # -- argument checks ------------------------------------------------------------------------

    def _fields(self, env, rule, readable, requested):
        if not requested:
            # A small default keeps results readable; callers name more fields when they need them.
            fields = env[rule.model]._fields
            keys = [n for n in KEY_FIELDS if n in readable and fields[n].store]
            return (["display_name"] if "display_name" in readable else []) + keys[:DEFAULT_FIELDS_MAX]
        for name in requested:
            if name not in readable:
                raise ToolError(_("Unknown or hidden field: %s", name))
        return list(dict.fromkeys(requested))

    def _order(self, filterable, relational, order):
        """``"<field> asc|desc"`` terms over stored, non-relational fields (a relational order sorts by
        the target's own order, which may include hidden fields)."""
        if not order:
            return None
        terms = []
        for part in order.split(","):
            match = _ORDER_TERM.match(part)
            if not match or match.group(1) not in filterable or match.group(1) in relational:
                raise ToolError(_("Invalid order “%s”: use \"<field> asc|desc\" with the model's own fields.", order))
            terms.append(f"{match.group(1)} {(match.group(2) or 'asc').lower()}")
        return ", ".join(terms)

    def _decode_cursor(self, cursor):
        if not cursor:
            return 0
        try:
            offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
        except (ValueError, binascii.Error, UnicodeError):
            offset = -1
        if offset < 0:
            raise ToolError(_("Invalid cursor: pass next_cursor from the previous result."))
        return offset

    def _values(self, env, rule, values, records=None):
        """Checked ``values`` for create (``records`` None) or for a write on ``records``."""
        readable = field_guard.readable_fields(env, rule)
        writable = field_guard.writable_fields(env, rule, readable)
        out = {}
        for name, value in values.items():
            if name not in readable:
                raise ToolError(_("Unknown or hidden field: %s", name))
            if name not in writable:
                raise ToolError(_("The field %s cannot be set.", name))
            ftype = readable[name]["type"]
            if ftype == "many2many":
                if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
                    raise ToolError(_("The field %s takes a list of record ids.", name))
                self._check_targets(env, name, readable[name]["relation"], value)
                value = [(6, 0, value)]
            elif ftype == "many2one":
                if value is None:
                    value = False
                if value is not False and (not isinstance(value, int) or isinstance(value, bool)):
                    raise ToolError(_("The field %s takes a record id or false.", name))
                if value:
                    self._check_targets(env, name, readable[name]["relation"], [value])
            elif ftype == "reference":
                if value is None:
                    value = False
                if value is not False:
                    target, rid = _parse_reference(env, value)
                    if target is None:
                        raise ToolError(_("The field %s takes \"model,id\" or false.", name))
                    self._check_targets(env, name, target, [rid])
            elif ftype == "many2one_reference":
                if value is None:
                    value = False
                if value is not False and (not isinstance(value, int) or isinstance(value, bool)):
                    raise ToolError(_("The field %s takes a record id or false.", name))
            elif not isinstance(value, domain_guard.SCALARS):
                raise ToolError(_("The field %s takes a single value.", name))
            out[name] = value
        self._check_model_references(env, rule, out, records)
        return out

    def _check_model_references(self, env, rule, values, records):
        """Many2one-reference targets (id field + model field) must be readable by the user, whichever
        of the two the call changes."""
        fields = env[rule.model]._fields
        for name, field in fields.items():
            if field.type != "many2one_reference":
                continue
            model_field = field.model_field
            if name not in values and model_field not in values:
                continue
            targets = [values] if records is None else [
                {name: values.get(name, rec[name]), model_field: values.get(model_field, rec[model_field])}
                for rec in records
            ]
            for target in targets:
                rid, comodel = target.get(name), target.get(model_field)
                if not rid:
                    continue
                if not isinstance(comodel, str) or comodel not in env:
                    raise ToolError(_("The field %(field)s needs a valid model in %(model_field)s.",
                                      field=name, model_field=model_field))
                self._check_targets(env, name, comodel, [rid])

    def _check_targets(self, env, name, comodel, ids):
        """Linked records must be readable by the user: Odoo does not check the target on write, and
        reads show linked names regardless of access."""
        ids = set(ids)
        if not ids:
            return
        Target = env[comodel]
        visible = Target.with_context(active_test=False).search([("id", "in", list(ids))]) \
            if can_read_model(Target) else Target
        if len(visible) != len(ids):
            raise ToolError(_("The field %(field)s refers to records that do not exist or you cannot see: %(ids)s",
                              field=name, ids=sorted(ids - set(visible.ids))))

    def _all_visible(self, env, rule, ids):
        """The records for ``ids``, all of them visible to the user within the extra domain."""
        ids = list(dict.fromkeys(ids))
        records = _visible(env, rule, ids)
        missing = sorted(set(ids) - set(records.ids))
        if missing:
            raise ToolError(_("Records not found or not accessible: %s", missing))
        return records

    def _still_inside(self, env, rule, records):
        """Refuse writes that move records outside the admin's extra domain."""
        if len(_visible(env, rule, records.ids)) != len(records):
            raise ToolDenied(_("The result would fall outside the records assistants may use on %s.", rule.model))


# -- helpers ------------------------------------------------------------------------------------

def localized_env(ctx):
    """``ctx.env`` with the request locale's language when it is installed, else the user's."""
    env = ctx.env
    installed = [code for code, _name in env["res.lang"].get_installed()]
    lang = env.lang or env.user.lang
    if not (lang or "").startswith(ctx.locale or "-"):
        lang = next((code for code in installed if code.split("_")[0] == ctx.locale), lang)
    return env(context=dict(env.context, lang=lang or "en_US"))


def _visible(env, rule, ids):
    """Records among ``ids`` the user can read (record rules) and that match the extra domain; archived included."""
    if not ids:
        return env[rule.model]
    return env[rule.model].with_context(active_test=False).search(
        domain_and([("id", "in", list(ids))], rule.extra_domain)
    ).with_env(env)


def _odoo_message(error):
    return str(error.args[0]) if error.args else str(error)


def _json_default(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _result(env, payload):
    context = env.context  # noqa: F841 - read by _()
    text = json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ToolError(_("The result is larger than 1 MB: ask for fewer fields or records."))
    return ToolResult(text, structured=json.loads(text))


def _fit_records(rows):
    """``(kept, truncated)``: the leading ``rows`` that fit ``MAX_RECORDS_TEXT_BYTES`` (at least one)."""
    budget, kept = MAX_RECORDS_TEXT_BYTES, []
    for row in rows:
        size = len(json.dumps(row, ensure_ascii=False, default=_json_default, separators=(",", ":")).encode()) + 1
        if kept and size > budget:
            return kept, True
        budget -= size
        kept.append(row)
    return kept, False


def _truncated_note(env):
    context = env.context  # noqa: F841 - read by _()
    return _("Results were cut short to stay readable: ask for fewer fields or records.")


def _max_id(env, Model):
    """Highest id in the model's table (no record data), to spot records a model-level call creates."""
    if Model._abstract:
        return None
    env.cr.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{Model._table}"')
    return env.cr.fetchone()[0]


def _created_since(env, Model, before):
    """Records of ``Model`` with an id above ``before``: created in this transaction (repeatable read)."""
    env.cr.execute(f'SELECT id FROM "{Model._table}" WHERE id > %s', [before])
    return Model.browse([row[0] for row in env.cr.fetchall()])


def _parse_reference(env, value):
    """``("model", id)`` from a ``"model,id"`` string, or ``(None, None)``."""
    if not isinstance(value, str):
        return None, None
    model, _sep, rid = value.partition(",")
    model = model.strip()
    try:
        rid = int(rid)
    except ValueError:
        return None, None
    if model not in env or rid <= 0:
        return None, None
    return model, rid


def _safe(env, value, rule=None, depth=0):
    """A JSON-safe copy of a method result: recordsets → [{id, display_name}], actions → {type, name}.

    Returned recordsets are re-read as the acting user: only records they can read are kept (a method
    may return privileged records), at most ``MAX_RECORDS``; records of the exposed model must also match
    its extra domain.
    """
    if depth > 20:
        return str(value)
    if isinstance(value, BaseModel):
        Model = env[value._name]
        ids = [i for i in value.ids if isinstance(i, int)][:MAX_RECORDS]
        if not ids or not can_read_model(Model):
            return []
        domain = [("id", "in", ids)]
        if rule is not None and value._name == rule.model:
            domain = domain_and(domain, rule.extra_domain)
        visible = Model.with_context(active_test=False).search(domain)
        return [{"id": r.id, "display_name": r.display_name} for r in visible]
    if isinstance(value, dict):
        action_type = value.get("type")
        if isinstance(action_type, str) and action_type.startswith("ir.actions"):
            return {"type": action_type, "name": _safe(env, value.get("name"), rule, depth + 1)}
        return {str(k): _safe(env, v, rule, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(env, v, rule, depth + 1) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)
