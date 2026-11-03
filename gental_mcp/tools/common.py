"""Helpers shared by the curated tool modules: result text in the request locale, Odoo errors as tool
errors, enum schemas, labels and amounts."""
import contextlib
import json

from markupsafe import Markup, escape

from .. import compat
from ..core.context import ODOO_LANGS
from ..core.tools import ToolResult
from ..services import curated
from ..services.generic_tools import localized_env  # noqa: F401 - re-exported for the tool modules


def text(ctx, term, *args, **kwargs) -> str:
    """``term`` (an ``_lt`` term) in the request locale, formatted with ``args`` or ``kwargs``."""
    translated = compat.translate(ODOO_LANGS.get(ctx.locale, "en_US"), term)
    if kwargs:
        return translated % kwargs
    return translated % args if args else translated


def lang_context(ctx) -> dict:
    """A ``context`` dict for ``odoo._`` in the request locale (``_`` reads a local named ``context``)."""
    return {"lang": ODOO_LANGS.get(ctx.locale, "en_US")}


@contextlib.contextmanager
def odoo_errors(ctx):
    """Report Odoo's access and business errors to the model as a generic, localized refusal (the call's
    savepoint rolls back; Odoo's own text is logged server-side only)."""
    try:
        yield
    except curated.ODOO_ERRORS as e:
        raise curated.odoo_refusal(ctx, e) from None


def enum(values, titles, **extra) -> dict:
    """An enum property whose values carry localized titles (``oneOf`` of ``const`` + ``title``)."""
    return {"type": "string", "oneOf": [{"const": v, "title": titles[v]} for v in values], **extra}


def html_text(value: str):
    """Plain text as safe HTML for the chatter: escaped, line breaks as ``<br/>``."""
    return Markup("<br/>").join(escape(line) for line in value.splitlines())


def selection_label(record, field_name) -> str:
    """The label of ``record[field_name]`` in ``record``'s language ('' when unset)."""
    value = record[field_name]
    if not value:
        return ""
    return dict(record._fields[field_name]._description_selection(record.env)).get(value, value)


def selection_labels(ctx, record, field_name) -> dict:
    """Selection labels of ``record``'s field in the call's language (the user's own when that language
    isn't active in this database)."""
    lang = ODOO_LANGS.get(ctx.locale)
    if lang and ctx.env["res.lang"].search_count([("code", "=", lang), ("active", "=", True)]):
        record = record.with_context(lang=lang)
    return dict(record._fields[field_name]._description_selection(record.env))


def like_escape(value: str) -> str:
    """``value`` for an exact ``=like`` / ``=ilike`` match (its wildcards escaped)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def money(amount, currency) -> str:
    """``amount`` with the currency's decimals and code, e.g. ``72.50 SAR``."""
    return f"{amount:.{currency.decimal_places}f} {currency.name}"


def quantity(qty) -> str:
    return str(int(qty)) if float(qty).is_integer() else f"{qty:g}"


def iso(value):
    """A date or datetime as ISO 8601 text, or None."""
    return value.isoformat() if value else None


def result(summary: str, structured: dict, record=None) -> ToolResult:
    """A tool result: ``summary`` (in the request locale) then the structured data as compact JSON."""
    data = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(f"{summary}\n{data}", structured=structured, record=record)
