from odoo import fields, models

from ..compat import create_unique_index


class GentalMcpWebhookEvent(models.Model):
    """The webhook event catalog (``data/webhook_events.xml``): what a subscription can listen to."""

    _name = "gental.mcp.webhook.event"
    _description = "Gental MCP Webhook Event"
    _order = "id"

    code = fields.Char(required=True, readonly=True)
    name = fields.Char(required=True, translate=True, readonly=True)
    model = fields.Char(readonly=True, help="Empty for generic events, which apply to the exposed models chosen.")
    kind = fields.Selection(
        [("created", "Created"), ("transition", "Changed to a state"), ("updated", "Updated")],
        required=True, readonly=True,
    )
    state_field = fields.Char(readonly=True)
    # JSON list; empty means any change of ``state_field``.
    target_values = fields.Char(default="[]", readonly=True)
    extra_domain = fields.Char(default="[]", readonly=True)
    # JSON list; empty means every field the run-as user can read. Names missing on the model are skipped.
    payload_fields = fields.Char(default="[]", readonly=True)
    generic = fields.Boolean(readonly=True)
    available = fields.Boolean(compute="_compute_available", search="_search_available")

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_webhook_event_code_uniq", self._table, ["code"])

    def _compute_available(self):
        for rec in self:
            rec.available = rec.generic or bool(rec.model and rec.model in self.env.registry)

    def _search_available(self, operator, value):
        """Supports ``=``, ``!=``, ``in`` and ``not in`` with booleans (19.0 rewrites ``=`` into ``in``)."""
        values = [value] if value is None or isinstance(value, (bool, int, str)) else list(value)
        wanted = {bool(v) for v in values}
        if operator in ("!=", "not in"):
            wanted = {True, False} - wanted
        elif operator not in ("=", "in"):
            raise ValueError(f"unsupported operator {operator} on available")
        installed = [model for model in set(self.search([("generic", "=", False)]).mapped("model"))
                     if model and model in self.env.registry]
        available = ["|", ("generic", "=", True), ("model", "in", installed)]
        if wanted == {True, False}:
            return []
        if wanted == {True}:
            return available
        if wanted == {False}:
            return ["!", *available]
        return [("id", "in", [])]
