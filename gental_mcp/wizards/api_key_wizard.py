from odoo import _, api, fields, models

NEW_KEY_CONTEXT = "gmcp_new_key"


class GentalMcpApiKeyWizard(models.TransientModel):
    """Create an API key in two steps. The plaintext key only travels in the second step's context.

    It is never written to the database: the second step is an unsaved record, closed with Cancel.
    """

    _name = "gental.mcp.api.key.wizard"
    _description = "New API Key"

    name = fields.Char(required=True, help="What this key is for, for example the assistant or system using it.")
    user_id = fields.Many2one(
        "res.users", string="User", required=True, default=lambda self: self.env.user,
        help="The key acts as this user and can only do what this user is allowed to do. Only an "
             "administrator can create a key for another user.",
    )
    can_pick_user = fields.Boolean(compute="_compute_can_pick_user")
    expires_at = fields.Datetime(string="Expires", help="Leave empty for a key that does not expire.")
    state = fields.Selection([("form", "Form"), ("done", "Done")], compute="_compute_key")
    key = fields.Char(string="API Key", compute="_compute_key")

    @api.depends_context("uid")
    def _compute_can_pick_user(self):
        for wizard in self:
            wizard.can_pick_user = self.env.user._is_system()

    @api.depends_context(NEW_KEY_CONTEXT)
    def _compute_key(self):
        key = self.env.context.get(NEW_KEY_CONTEXT) or False
        for wizard in self:
            wizard.key = key
            wizard.state = "done" if key else "form"

    def action_create(self):
        self.ensure_one()
        _client, plaintext = self.env["gental.mcp.client"].create_api_key(
            self.name, self.user_id, self.expires_at or None
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("API key created"),
            "res_model": self._name,
            "view_mode": "form",
            "target": "new",
            "context": {NEW_KEY_CONTEXT: plaintext},
        }
