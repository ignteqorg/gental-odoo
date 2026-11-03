import ast
from typing import NamedTuple

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..compat import create_unique_index, private

# ORM entry points and environment switches the generic tools already cover (with their
# own checks) or that must never be reachable as a "method" call.
FORBIDDEN_METHODS = {
    "create", "write", "unlink", "copy", "sudo", "with_user", "with_context", "with_env",
    "with_company", "browse", "search", "search_read", "search_count", "read", "read_group",
    "name_search", "name_create", "export_data", "load", "check_access", "check_access_rights",
    "check_access_rule", "invalidate_model", "flush_model", "fields_get", "get_metadata",
    "toggle_active", "action_archive", "action_unarchive", "web_save", "web_read", "web_search_read",
    "search_fetch", "fetch", "copy_data", "web_read_group", "formatted_read_group",
    "exists",  # @api.private on 19.0 only: refused here so the allowlist is the same on every version
}


def is_rpc_callable(model, name):
    """Mirror of Odoo's ``get_public_method``: a plain public instance method, not ``@api.private``."""
    cls = type(model)
    method = getattr(cls, name, None)
    if not callable(method) or method == getattr(model, name, None):  # missing, classmethod, staticmethod
        return False
    return not any(getattr(getattr(klass, name, None), "_api_private", False) for klass in cls.mro())
DOMAIN_OPERATORS = {"&", "|", "!"}
FALSE_DOMAIN = [("id", "in", [])]


class ExposedRule(NamedTuple):
    """Plain-data snapshot of one active allowlist row, handed to the tool services."""

    model: str
    can_read: bool
    can_create: bool
    can_write: bool
    can_unlink: bool
    methods: dict  # method name -> kind ("read" / "write" / "destructive")
    hidden: frozenset  # hidden field names
    extra_domain: list

    def allows(self, operation):
        """``operation``: ``read``, ``create``, ``write`` or ``unlink``."""
        return getattr(self, "can_" + operation)


def parse_domain(text):
    """Parse a domain literal; raise ``ValueError`` unless it is a list of leaves and operators."""
    domain = ast.literal_eval((text or "").strip() or "[]")
    if not isinstance(domain, list):
        raise ValueError("not a list")
    for item in domain:
        if isinstance(item, str) and item in DOMAIN_OPERATORS:
            continue
        if isinstance(item, (list, tuple)) and len(item) == 3 and isinstance(item[0], str) and isinstance(item[1], str):
            continue
        if isinstance(item, (list, tuple)) and tuple(item) in ((0, "=", 1), (1, "=", 1)):  # FALSE/TRUE leaves
            continue
        raise ValueError(f"invalid domain item {item!r}")
    return domain


class GentalMcpExposedModel(models.Model):
    _name = "gental.mcp.exposed.model"
    _description = "Gental MCP Exposed Model"
    _rec_name = "model_id"

    config_id = fields.Many2one(
        "gental.mcp.config", default=lambda self: self.env["gental.mcp.config"].get(), ondelete="cascade"
    )
    model_id = fields.Many2one("ir.model", string="Model", required=True, ondelete="cascade", index=True)
    model = fields.Char(string="Model Name", related="model_id.model", store=True)
    can_read = fields.Boolean(default=True)
    can_create = fields.Boolean()
    can_write = fields.Boolean()
    can_unlink = fields.Boolean(string="Can Delete")
    method_ids = fields.One2many("gental.mcp.exposed.method", "exposed_model_id", string="Methods")
    hidden_field_ids = fields.Many2many(
        "ir.model.fields", string="Hidden Fields", domain="[('model_id', '=', model_id)]"
    )
    extra_domain = fields.Char(default="[]", help="Records outside this domain stay invisible to assistants.")
    active = fields.Boolean(default=True)

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_exposed_model_model_id_uniq", self._table, ["model_id"])

    @private
    @api.model
    def consent_summary(self):
        """What assistants can reach, for the OAuth consent page: ``[(model label, can change)]`` (sudo).

        Labels are in the current user's language, sorted by label.
        """
        rows = []
        for rec in self.sudo().search([]):
            if not rec.model or rec.model not in self.env:
                continue
            changes = rec.can_create or rec.can_write or rec.can_unlink or any(
                m.kind != "read" for m in rec.method_ids
            )
            if rec.can_read or changes:
                label = rec.model_id.with_context(lang=self.env.lang).name
                rows.append((label, bool(changes)))
        return sorted(rows)

    @api.model
    def _mcp_rules(self, model=None):
        """``{model: ExposedRule}`` for the active rows (optionally one model), read fresh from the database.

        Sudo: the users acting through assistants cannot read the allowlist itself. Only plain data
        leaves this method, never a sudo recordset.
        """
        domain = [("model", "=", model)] if model else []
        rules = {}
        for row in self.sudo().search(domain):
            if not row.model or row.model not in self.env:
                continue
            try:
                extra = parse_domain(row.extra_domain)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                extra = FALSE_DOMAIN  # unreadable admin domain: show nothing rather than everything
            rules[row.model] = ExposedRule(
                model=row.model,
                can_read=row.can_read,
                can_create=row.can_create,
                can_write=row.can_write,
                can_unlink=row.can_unlink,
                methods={m.name: m.kind for m in row.method_ids},
                hidden=frozenset(row.hidden_field_ids.mapped("name")),
                extra_domain=extra,
            )
        return rules

    @api.constrains("extra_domain")
    def _check_extra_domain(self):
        for rec in self:
            try:
                parse_domain(rec.extra_domain)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                raise ValidationError(_("The extra domain is not a valid domain: %s", rec.extra_domain)) from None


class GentalMcpExposedMethod(models.Model):
    _name = "gental.mcp.exposed.method"
    _description = "Gental MCP Exposed Method"

    exposed_model_id = fields.Many2one("gental.mcp.exposed.model", required=True, ondelete="cascade", index=True)
    name = fields.Char(required=True)
    kind = fields.Selection(
        [("read", "Read"), ("write", "Write"), ("destructive", "Destructive")], required=True, default="write"
    )
    description = fields.Char()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if isinstance(vals.get("name"), str):
                vals["name"] = vals["name"].strip()
        return super().create(vals_list)

    def write(self, vals):
        if isinstance(vals.get("name"), str):
            vals = dict(vals, name=vals["name"].strip())
        return super().write(vals)

    @api.constrains("name", "exposed_model_id")
    def _check_method_name(self):
        for rec in self:
            name = (rec.name or "").strip()
            model = rec.exposed_model_id.model
            if not name or name.startswith("_") or name in FORBIDDEN_METHODS:
                raise ValidationError(_("The method “%s” cannot be exposed.", name))
            if not model or model not in self.env or not is_rpc_callable(self.env[model], name):
                raise ValidationError(_("The model %(model)s has no public method “%(name)s”.", model=model, name=name))
