from odoo import api, fields, models

from ..compat import create_unique_index, private
from ..services import areas, curated

AREA_SELECTION = [
    ("contacts", "Contacts"),
    ("sales", "Sales"),
    ("invoicing", "Invoicing"),
    ("crm", "CRM"),
    ("inventory", "Inventory"),
    ("pos", "Point of Sale"),
]
KIND_SELECTION = [("read", "Read"), ("write", "Write"), ("destructive", "Destructive")]


class GentalMcpToolSetting(models.Model):
    """On/off switch of one curated tool. Rows mirror ``curated.REGISTRY``; tools are on by default."""

    _name = "gental.mcp.tool.setting"
    _description = "Gental MCP Curated Tool Setting"
    _order = "sequence, name"

    def _default_config(self):
        return self.env["gental.mcp.config"].get()

    name = fields.Char(string="Tool", required=True, readonly=True)
    area = fields.Selection(AREA_SELECTION, required=True, readonly=True)
    sequence = fields.Integer(readonly=True)
    enabled = fields.Boolean(default=True)
    config_id = fields.Many2one("gental.mcp.config", required=True, ondelete="cascade", default=_default_config)
    kind = fields.Selection(KIND_SELECTION, compute="_compute_from_registry")
    title = fields.Char(compute="_compute_from_registry")
    area_installed = fields.Boolean(string="App Installed", compute="_compute_area_installed")

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_tool_setting_name_uniq", self._table, ["name"])

    @api.depends("name")
    @api.depends_context("lang")
    def _compute_from_registry(self):
        arabic = (self.env.lang or "").startswith("ar")
        for rec in self:
            tool = curated.REGISTRY.get(rec.name)
            rec.kind = tool.kind if tool else False
            rec.title = (tool.title_ar if arabic else tool.title_en) if tool else rec.name

    @api.depends("area")
    def _compute_area_installed(self):
        installed = areas.installed_areas(self.env)
        for rec in self:
            rec.area_installed = rec.area in installed

    @private
    @api.model
    def disabled_names(self):
        """Names of the curated tools the owner switched off."""
        return set(self.sudo().search([("enabled", "=", False)]).mapped("name"))

    @api.model
    def _sync_from_registry(self):
        """Create a row for every registered tool and drop rows of tools that no longer exist.

        Runs on install and upgrade (``data/tool_setting_data.xml``); owners' on/off choices are kept.
        """
        Setting = self.sudo()
        order = list(areas.AREAS)
        Setting.search([("name", "not in", list(curated.REGISTRY))]).unlink()
        existing = {rec.name: rec for rec in Setting.search([])}
        config = self.env["gental.mcp.config"].get()
        for name, tool in curated.REGISTRY.items():
            vals = {"area": tool.area, "sequence": order.index(tool.area)}
            rec = existing.get(name)
            if rec is None:
                Setting.create(dict(vals, name=name, config_id=config.id))
            elif any(rec[k] != v for k, v in vals.items()):
                rec.write(vals)
        return True
