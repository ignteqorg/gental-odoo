"""Curated business tools: the ``CuratedTool`` base class, its registry and the MCP provider.

Rules for tools built on this: run all ORM as the acting user (``ctx.env``, never sudo); check every
many2one / many2many id taken from the arguments with ``visible_records`` before writing it; return only
fields the tool explicitly declares (never whole ``read()`` results or ``display_name`` of relations).

Before ``run``, every call checks that the acting user may read each field the tool declares in
``required_fields()`` (field ``groups``): 17.0 and 18.0 don't enforce field groups on attribute reads of
computed fields, so the framework does it for every version.

A tool is listed only when its area's Odoo app is installed, its area is on in Configuration, the tool
itself is on (``gental.mcp.tool.setting``, on by default) and, for customer-scoped tools, a Gental
identity secret is configured. Every call re-checks the same switches. Tools run as the acting user.

Odoo's own error text (access errors, business errors) never reaches the model: it is logged here and the
model gets a generic, localized refusal (``odoo_refusal``).
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Type, Union

from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.tools.translate import _lt

from .. import compat
from ..core import meta as gmeta
from ..core.context import ODOO_LANGS, CallContext
from ..core.tools import ToolAnnotations, ToolDenied, ToolError, ToolResult, ToolSpec
from . import areas, identity

_logger = logging.getLogger(__name__)

KINDS = ("read", "write", "destructive")
ODOO_ERRORS = (AccessError, MissingError, UserError, ValidationError)
# Largest Odoo record id (Postgres ``integer``): every id argument's schema maximum.
ID_MAX = 2147483647

# Tools a public, customer-facing agent may use: catalog reads, lead and contact capture, quoting, and the
# 🔒 identity tools (the verified customer's own records only). Every other tool is a staff tool: it reads
# or changes any customer's data and belongs on internal agents only. Exposed on each tool as
# ``_meta["ai.gental/audience"]`` so the Gental catalog can default "visitor safe" from it.
PUBLIC_SAFE_TOOLS = frozenset({
    "search_products", "stock_availability", "create_lead", "upsert_contact", "preview_quotation",
    "create_quotation", "my_invoices", "my_balance", "my_orders", "my_deliveries", "my_invoice_document",
    "my_order_document",
})
AUDIENCE_PUBLIC, AUDIENCE_STAFF = "public", "staff"
STAFF_DESCRIPTION = _lt("Staff tool: %s")
STAFF_TITLE = _lt("Staff: %s")


@dataclass(frozen=True)
class AppField:
    """A declared field that exists only when one of ``modules`` is installed (e.g. ``qty_available`` from
    ``stock``), or only before Odoo version ``dropped_in`` (``res.partner.mobile``: 19, ``company_name``: 20).
    Checked like any other field when present; the cross-version test accepts it missing only then."""

    name: str
    modules: Tuple[str, ...] = ()
    dropped_in: Optional[int] = None


# A field name, the names of one field renamed between Odoo versions (one of them exists), or an ``AppField``.
FieldName = Union[str, Tuple[str, ...], AppField]

# Fields ``identity.match_partner`` reads: declared for every 🔒 tool (``CuratedTool.declared_fields``).
IDENTITY_MATCH_FIELDS: Dict[str, List[FieldName]] = {
    "res.partner": ["phone", AppField("mobile", dropped_in=19), "country_id", "commercial_partner_id"],
    "res.country": ["phone_code"],
}

REGISTRY: Dict[str, Type["CuratedTool"]] = {}

MESSAGES = {
    "off": {
        "en": "This tool is turned off in Odoo.",
        "ar": "هذي الأداة مقفلة في أودو.",
    },
    "identity": {
        "en": "This needs a verified customer.",
        "ar": "هذي تحتاج عميل موثّق.",
    },
    "not_visible": {
        "en": "Some of these records don't exist or you can't see them.",
        "ar": "بعض هذي السجلات مو موجودة أو ما تقدر تشوفها.",
    },
    "unknown": {
        "en": "This tool isn't available in this Odoo.",
        "ar": "هذي الأداة مو متوفرة في هذا الأودو.",
    },
    "no_field_access": {
        "en": "You don't have the Odoo rights this tool needs.",
        "ar": "ما عندك الصلاحيات اللي تحتاجها هذي الأداة في أودو.",
    },
    "refused": {
        "en": "Odoo didn't allow this with your rights, so nothing was changed.",
        "ar": "أودو ما سمح بهذا بصلاحياتك، فما تغيّر شي.",
    },
    "failed": {
        "en": "Odoo couldn't do this, so nothing was changed. Check the details, or ask staff to do it in Odoo.",
        "ar": "أودو ما قدر يسوي هذا، فما تغيّر شي. تأكد من التفاصيل، أو خل الموظفين يسوونه في أودو.",
    },
}


def message(key: str, locale: str) -> str:
    texts = MESSAGES[key]
    return texts.get(locale, texts["en"])


def odoo_refusal(ctx: CallContext, error: Exception) -> ToolError:
    """A generic, localized tool error for an Odoo exception: ``ToolDenied`` for an access error (logged as
    denied), ``ToolError`` otherwise. Odoo's own message (which may name records, fields or other people's
    data) is logged server-side only."""
    tool = ctx.tool_spec.name if ctx.tool_spec is not None else ""
    _logger.info("Gental MCP: Odoo refused tool %s for user %s: %s", tool, ctx.user_id,
                 error.args[0] if error.args else error)
    if isinstance(error, AccessError):
        return ToolDenied(message("refused", ctx.locale))
    return ToolError(message("failed", ctx.locale))


def id_schema(description=None, title=None, **extra) -> dict:
    """The schema of a record id argument (``1`` to ``ID_MAX``); ``title`` and ``description`` are ``_lt`` terms."""
    schema = {"type": "integer", "minimum": 1, "maximum": ID_MAX, **extra}
    if title is not None:
        schema["title"] = title
    if description is not None:
        schema["description"] = description
    return schema


class CuratedTool:
    """One curated tool. Subclasses set the class attributes and implement ``input_schema`` and ``run``.

    ``label`` and ``description`` are ``_lt`` terms, and so is the ``title`` (and any ``description``) of every
    input property: ``spec`` renders them in the request's language (one language per response)."""

    name: str = ""
    area: str = ""
    kind: str = "read"  # "read" | "write" | "destructive"
    idempotent: bool = False
    requires_identity: bool = False
    label = ""  # ``_lt`` term: the tool's title
    description = ""  # ``_lt`` term, written for the model

    def input_schema(self, ctx: CallContext) -> dict:
        raise NotImplementedError

    def run(self, args: dict, ctx: CallContext) -> ToolResult:
        raise NotImplementedError

    def required_fields(self) -> Dict[str, List[FieldName]]:
        """Model -> field names this tool reads or writes. Checked on every call (the acting user must be
        allowed to read each one) and against each Odoo version's models in the tests. A field renamed
        between Odoo versions is a tuple of its names, e.g. ``("team_ids", "team_id")``: one must exist. A
        ``None`` among them marks a field some versions dropped, e.g. ``("company_name", None)`` (gone in 20.0):
        the tool must not use it where it doesn't exist."""
        return {}

    def declared_fields(self) -> Dict[str, List[FieldName]]:
        """``required_fields()``, plus the fields the identity match reads for a 🔒 tool."""
        fields = {model: list(names) for model, names in self.required_fields().items()}
        if self.requires_identity:
            for model, names in IDENTITY_MATCH_FIELDS.items():
                merged = fields.setdefault(model, [])
                merged.extend(n for n in names if n not in merged)
        return fields

    def field_access_refusal(self, ctx: CallContext) -> str:
        """Refusal text when the acting user may not read a field of ``required_fields()``."""
        return message("no_field_access", ctx.locale)

    def annotations(self) -> ToolAnnotations:
        return ToolAnnotations(
            read_only=self.kind == "read",
            destructive=self.kind == "destructive",
            idempotent=self.idempotent,
        )

    def audience(self) -> str:
        """``"public"`` for tools safe on a customer-facing agent (``PUBLIC_SAFE_TOOLS``), else ``"staff"``."""
        return AUDIENCE_PUBLIC if self.name in PUBLIC_SAFE_TOOLS else AUDIENCE_STAFF

    def title(self, lang: str) -> str:
        """The title in Odoo language ``lang``: staff tools' start with ``Staff:`` (``للموظفين:``)."""
        title = compat.translate(lang, self.label)
        return compat.translate(lang, STAFF_TITLE) % title if self.audience() == AUDIENCE_STAFF else title

    def full_description(self, lang: str) -> str:
        """The description the model sees, in Odoo language ``lang``: staff tools' start with ``Staff tool:``."""
        description = compat.translate(lang, self.description)
        if self.audience() == AUDIENCE_STAFF:
            return compat.translate(lang, STAFF_DESCRIPTION) % description
        return description

    def spec(self, ctx: CallContext, input_schema: Optional[dict] = None) -> ToolSpec:
        lang = ODOO_LANGS.get(ctx.locale, "en_US")
        schema = self.input_schema(ctx) if input_schema is None else input_schema
        return ToolSpec(self.name, self.title(lang), self.full_description(lang), compat.translate_terms(lang, schema),
                        self.annotations(), meta={gmeta.AUDIENCE: self.audience()})


def visible_records(ctx: CallContext, model: str, ids):
    """The ``model`` records with ``ids`` that the acting user can see, searched as that user (access
    rules and record rules apply). Raises ``ToolError`` when any id is missing or not visible, so a tool
    never links a record the user couldn't pick themselves."""
    wanted = {int(i) for i in ids}
    records = ctx.env[model].search([("id", "in", sorted(wanted))]) if wanted else ctx.env[model]
    if set(records.ids) != wanted:
        raise ToolError(message("not_visible", ctx.locale))
    return records


def register(tool_cls: Type[CuratedTool]) -> Type[CuratedTool]:
    """Class decorator adding ``tool_cls`` to ``REGISTRY`` under its ``name``."""
    if not tool_cls.name or tool_cls.area not in areas.AREAS or tool_cls.kind not in KINDS:
        raise ValueError(f"Invalid curated tool {tool_cls.__name__}: name, area or kind")
    REGISTRY[tool_cls.name] = tool_cls
    return tool_cls


class CuratedToolProvider:
    """``ToolProvider`` over ``REGISTRY`` for the acting user's environment."""

    def __init__(self, env):
        self.env = env

    def _installed_tools(self) -> List[CuratedTool]:
        """Registered tools whose Odoo app is installed, by name."""
        installed = areas.installed_areas(self.env)
        return [REGISTRY[n]() for n in sorted(REGISTRY) if REGISTRY[n].area in installed]

    def _switches(self):
        """``(config, disabled tool names)``, read once per request step."""
        return self.env["gental.mcp.config"].get(), self.env["gental.mcp.tool.setting"].disabled_names()

    @staticmethod
    def _refusal(tool: CuratedTool, config, disabled) -> Optional[str]:
        """Why ``tool`` is switched off right now (a ``MESSAGES`` key), or None when it may run."""
        if not config[f"area_{tool.area}_enabled"] or tool.name in disabled:
            return "off"
        if tool.requires_identity and not _identity_configured(config):
            return "identity"
        return None

    def _find(self, name: str) -> Optional[CuratedTool]:
        tool_cls = REGISTRY.get(name)
        if tool_cls is None or tool_cls.area not in areas.installed_areas(self.env):
            return None
        return tool_cls()

    def list_tools(self, ctx: CallContext) -> List[ToolSpec]:
        config, disabled = self._switches()
        return [t.spec(ctx) for t in self._installed_tools() if self._refusal(t, config, disabled) is None]

    def hidden_spec(self, name: str, ctx: CallContext) -> Optional[ToolSpec]:
        """Spec of an installed tool that is switched off, so calling it is denied (and logged) rather
        than unknown. Tools of missing apps stay unknown."""
        tool = self._find(name)
        if tool is None or self._refusal(tool, *self._switches()) is None:
            return None
        # Permissive schema: arguments are never validated (nor the real schema built) for a refused call,
        # so it is always denied and never reveals the switched-off tool's arguments.
        return tool.spec(ctx, input_schema={"type": "object"})

    def call(self, name: str, args: dict, ctx: CallContext) -> ToolResult:
        tool = self._find(name)
        if tool is None:
            raise ToolError(message("unknown", ctx.locale))
        refusal = self._refusal(tool, *self._switches())
        if refusal is not None:
            raise ToolDenied(message(refusal, ctx.locale))
        if tool.requires_identity:
            identity.require(ctx)
        if not _declared_fields_readable(tool, ctx.env):
            raise ToolDenied(tool.field_access_refusal(ctx))
        try:
            return tool.run(args, ctx)
        except ODOO_ERRORS as e:
            # Wall 3 (the acting user's ACLs or record rules: logged as denied) or a business error Odoo raised.
            raise odoo_refusal(ctx, e) from None

    def required_fields(self) -> Dict[str, List[str]]:
        """Union of every registered tool's ``required_fields``, alternatives flattened (every candidate name)."""
        out: Dict[str, List[str]] = {}
        for name in sorted(REGISTRY):
            for model, names in REGISTRY[name]().declared_fields().items():
                merged = out.setdefault(model, [])
                merged.extend(f for f in _flat(names) if f not in merged)
        return out


def _declared_fields_readable(tool: CuratedTool, env) -> bool:
    """Whether ``env``'s user may read every declared field of ``tool`` (models not installed are skipped)."""
    for model, names in tool.declared_fields().items():
        if model not in env:
            continue
        try:
            compat.check_field_read(env, model, _flat(names))
        except AccessError:
            return False
    return True


def _flat(names) -> List[str]:
    """``required_fields`` names with version alternatives (tuples) expanded."""
    return [n for entry in names for n in field_alternatives(entry)]


def field_alternatives(entry) -> Tuple[str, ...]:
    """The candidate names of one ``required_fields`` entry."""
    if isinstance(entry, AppField):
        return (entry.name,)
    return entry if isinstance(entry, tuple) else (entry,)


def _identity_configured(config) -> bool:
    """True once a Gental identity secret is set (never reads the secret itself)."""
    return bool(config.identity_secret_set)
