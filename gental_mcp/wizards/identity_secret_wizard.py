from odoo import _, fields, models
from odoo.exceptions import UserError

from ..compat import check_write_access


class GentalMcpIdentitySecretWizard(models.TransientModel):
    """Paste the customer identity secret from Gental's connect dialog. It is not kept on the wizard."""

    _name = "gental.mcp.identity.secret.wizard"
    _description = "Set Identity Secret"

    secret = fields.Char(
        string="Identity Secret",
        help="Paste the secret Gental showed when you connected Odoo (at least 32 characters). "
             "It won't be shown again here.",
    )

    def action_set(self):
        self.ensure_one()
        config = self.env["gental.mcp.config"].get()
        check_write_access(config.with_env(self.env))
        secret = (self.secret or "").strip()
        if not secret:
            raise UserError(_("Paste the secret from Gental's connect dialog."))
        self.secret = False
        config.set_identity_secret(secret)
        return {"type": "ir.actions.act_window_close"}
