import ipaddress
import time
from datetime import timedelta
from urllib.parse import urlsplit

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.http import request
from odoo.tools import config as odoo_config

REACHABILITY_PATH = "/.well-known/oauth-protected-resource"
EXPIRED_GRACE_DAYS = 7
IDENTITY_SECRET_PARAM = "gental_mcp.identity_secret"
IDENTITY_SECRET_MIN_LENGTH = 32
# One signing secret per database for the links and quotes this module hands out. Each use prefixes
# its message ("doc|...", "quote|...") so a signature of one kind is never valid as another.
QUOTE_SECRET_PARAM = "gental_mcp.quote_secret"
DOCUMENT_LINK_MAX_HOURS = 720

from ..compat import check_write_access, get_param, get_param_bool, private, set_param
from ..core.crypto import new_secret
from ..core.urls import normalize_url
from ..services import areas
from .res_users import _connected_users, revoke_lost


def proxy_warning(env, remote_addr, proxy_mode):
    """A warning when Odoo seems to sit behind a reverse proxy without ``proxy_mode``, else "".

    Behind a proxy without ``proxy_mode`` every request comes from the proxy's private address and Odoo
    does not read the forwarded host and scheme, so the URL it builds for itself can be wrong.
    """
    if proxy_mode or not remote_addr:
        return ""
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return ""
    if not address.is_private:
        return ""
    context = {"lang": env.lang or "en_US"}  # noqa: F841 - read by _()
    return _(
        "Odoo sees this request coming from %s, a private address, so it probably sits behind a proxy, "
        "but proxy mode is off. Start Odoo with proxy_mode = True and have the proxy pass the "
        "X-Forwarded-For, X-Forwarded-Host and X-Forwarded-Proto headers.",
        remote_addr,
    )


class GentalMcpConfig(models.Model):
    """Settings of the MCP connection: one record, ``gental_mcp.config_main``."""

    _name = "gental.mcp.config"
    _description = "Gental MCP Settings"
    _inherit = ["mail.thread"]

    def _default_connect_groups(self):
        return self.env.ref("gental_mcp.group_mcp_connect", raise_if_not_found=False)

    enabled = fields.Boolean(default=True, tracking=True)
    public_url = fields.Char(
        string="Public URL", tracking=True,
        help="Leave empty to use Odoo's own address (web.base.url). Set it only when assistants reach "
             "this Odoo at another address: https and the root address only, for example "
             "https://odoo.example.com.",
    )
    oauth_enabled = fields.Boolean(string="OAuth Enabled", default=True, tracking=True)
    dcr_enabled = fields.Boolean(string="Dynamic Client Registration", default=True, tracking=True)
    api_keys_enabled = fields.Boolean(string="API Keys Enabled", default=True, tracking=True)
    default_lang = fields.Selection(
        [("ar", "Arabic"), ("en", "English")], string="Default Language", default="ar", required=True, tracking=True
    )
    rate_limit_per_min = fields.Integer(
        string="Requests per Minute per Client", default=300, tracking=True,
        help="Each connected assistant (one Gental connection, one API key) may send this many requests "
             "a minute; more get «try again in a minute». Gental connects as one client for all its "
             "conversations, so keep this generous.",
    )
    allowed_origin_ids = fields.One2many(
        "gental.mcp.origin", "config_id", string="Allowed Browser Origins", tracking=True,
        help="Web pages allowed to call /mcp straight from a browser. This can only refuse browser "
             "callers: /mcp sends no CORS headers, so listing an origin does not open it to other sites. "
             "Servers like Gental send no Origin and are not affected.",
    )
    connect_group_ids = fields.Many2many(
        "res.groups", string="Groups Allowed to Connect", default=_default_connect_groups, tracking=True
    )
    reachability = fields.Selection(
        [("ok", "Reachable"), ("failed", "Not reachable")], string="Last Reachability Check", readonly=True, copy=False
    )
    log_retention_days = fields.Integer(string="Keep Inbox Logs (days)", default=90, tracking=True)
    webhook_log_retention_days = fields.Integer(
        string="Keep Webhook Deliveries (days)", default=90, tracking=True,
        help="Delivered and failed webhook deliveries older than this are deleted every day. "
             "Deliveries still waiting to be sent are kept. 0 keeps them for ever.",
    )
    document_link_hours = fields.Integer(
        string="Document Links Expire After (hours)", default=24, tracking=True,
        help="How long a PDF link that an assistant shares keeps working. After that the file is deleted; "
             "the row stays in Outbox › Documents for as long as the Inbox logs.",
    )
    exposed_model_ids = fields.One2many("gental.mcp.exposed.model", "config_id", string="Exposed Models")
    mcp_endpoint = fields.Char(string="MCP URL", compute="_compute_mcp_endpoint")
    url_unpinned = fields.Boolean(
        string="Address May Change", compute="_compute_url_unpinned",
        help="No Public URL is set and web.base.url is not frozen, so an administrator signing in from "
             "another address changes the MCP URL and every connected assistant must sign in again.",
    )
    # The plaintext lives in ir.config_parameter; only code (sudo) and system admins can read it.
    identity_secret = fields.Char(
        string="Identity Secret Value", compute="_compute_identity_secret", groups="base.group_system"
    )
    identity_secret_set = fields.Boolean(
        string="Identity Secret Set", compute="_compute_identity_secret",
        help="Gental signs each customer's verified phone with this secret. Tools for the chatting "
             "customer (my orders, my invoices) only work while it is set. Paste the full secret from "
             "Gental's connect dialog (at least 32 characters).",
    )
    identity_assume_company_country = fields.Boolean(
        string="Phones saved without a country belong to the company's country",
        default=True, tracking=True,
        help="When a contact has no country, a local number like 05… is read with your company's "
             "country code. Turn this off if you have customers abroad saved without a country: a "
             "number from another country could otherwise match the wrong customer. Contacts that "
             "have a country always use their own country.",
    )
    identity_secret_hint = fields.Char(string="Identity Secret", compute="_compute_identity_secret")

    # Curated tools: one switch per area (``services.areas.AREAS``) and whether its Odoo app is installed.
    area_contacts_enabled = fields.Boolean(string="Contacts", default=True, tracking=True)
    area_sales_enabled = fields.Boolean(string="Sales", default=True, tracking=True)
    area_invoicing_enabled = fields.Boolean(string="Invoicing", default=True, tracking=True)
    area_crm_enabled = fields.Boolean(string="CRM", default=True, tracking=True)
    area_inventory_enabled = fields.Boolean(string="Inventory", default=True, tracking=True)
    area_pos_enabled = fields.Boolean(string="Point of Sale", default=False, tracking=True)
    area_contacts_installed = fields.Boolean(string="Contacts App Installed", compute="_compute_areas_installed")
    area_sales_installed = fields.Boolean(string="Sales App Installed", compute="_compute_areas_installed")
    area_invoicing_installed = fields.Boolean(string="Invoicing App Installed", compute="_compute_areas_installed")
    area_crm_installed = fields.Boolean(string="CRM App Installed", compute="_compute_areas_installed")
    area_inventory_installed = fields.Boolean(string="Inventory App Installed", compute="_compute_areas_installed")
    area_pos_installed = fields.Boolean(string="Point of Sale App Installed", compute="_compute_areas_installed")
    tool_setting_ids = fields.One2many("gental.mcp.tool.setting", "config_id", string="Curated Tools")

    # The Webhooks page lists every subscription (archived ones too); rows open their own form.
    webhook_subscription_ids = fields.Many2many(
        "gental.mcp.webhook.subscription", string="Webhooks", compute="_compute_webhook_subscriptions",
    )

    def _compute_webhook_subscriptions(self):
        subs = self.env["gental.mcp.webhook.subscription"].with_context(active_test=False).search([])
        for rec in self:
            rec.webhook_subscription_ids = subs

    def action_new_webhook(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("New webhook"),
            "res_model": "gental.mcp.webhook.subscription",
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "current",
        }

    def _compute_areas_installed(self):
        installed = areas.installed_areas(self.env)
        for rec in self:
            for area in areas.AREAS:
                rec[f"area_{area}_installed"] = area in installed

    @api.depends_context("lang")
    def _compute_display_name(self):
        for rec in self:
            # The one settings record: its breadcrumb shows the model's (translated) name, not "model,1".
            rec.display_name = self.env["ir.model"]._get(self._name).name

    @api.depends("public_url")
    def _compute_mcp_endpoint(self):
        for rec in self:
            rec.mcp_endpoint = rec.mcp_url()

    @api.depends("public_url")
    def _compute_url_unpinned(self):
        frozen = get_param_bool(self.env["ir.config_parameter"].sudo(), "web.base.url.freeze")
        for rec in self:
            rec.url_unpinned = not (rec.public_url or "").strip() and not frozen

    def _compute_identity_secret(self):
        secret = get_param(self.env["ir.config_parameter"].sudo(), IDENTITY_SECRET_PARAM) or False
        for rec in self:
            rec.identity_secret = secret
            rec.identity_secret_set = bool(secret)
            rec.identity_secret_hint = f"••••{secret[-4:]}" if secret else False

    @private
    def set_identity_secret(self, secret):
        """Store (or clear, with a falsy ``secret``) the customer identity secret. Never logged."""
        self.ensure_one()
        secret = (secret or "").strip()
        if secret and len(secret) < IDENTITY_SECRET_MIN_LENGTH:
            raise UserError(_("This secret is too short. Paste the full secret from Gental's connect dialog."))
        set_param(self.env["ir.config_parameter"].sudo(), IDENTITY_SECRET_PARAM, secret or False)
        self.invalidate_recordset(["identity_secret", "identity_secret_set", "identity_secret_hint"])
        self.sudo()._message_log(
            body=_("Customer identity secret updated.") if secret else _("Customer identity secret cleared.")
        )

    def action_clear_identity_secret(self):
        self.ensure_one()
        check_write_access(self)
        self.set_identity_secret(False)
        return True

    @api.constrains("public_url")
    def _check_public_url(self):
        for rec in self:
            url = (rec.public_url or "").strip()
            if not url:
                continue
            try:
                parts = urlsplit(url)
                parts.port  # noqa: B018 - raises ValueError on an invalid port
            except ValueError:
                parts = None
            if (
                parts is None
                or parts.scheme.lower() != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.path not in ("", "/")
                or parts.query
                or parts.fragment
                or "#" in url
            ):
                raise ValidationError(_(
                    "The public URL must be https and the root address only, for example "
                    "https://odoo.example.com (no path). Leave it empty to use Odoo's own address."
                ))

    @api.constrains("document_link_hours")
    def _check_document_link_hours(self):
        for rec in self:
            if not 1 <= rec.document_link_hours <= DOCUMENT_LINK_MAX_HOURS:
                raise ValidationError(_("Document links must stay valid between 1 and 720 hours (30 days)."))

    @private
    @api.model
    def quote_secret(self):
        """The per-database signing secret (generated at install, recreated if missing). Never shown."""
        params = self.env["ir.config_parameter"].sudo()
        secret = get_param(params, QUOTE_SECRET_PARAM)
        if not secret:
            secret = new_secret("gq", 32)
            set_param(params, QUOTE_SECRET_PARAM, secret)
        return secret

    @api.model_create_multi
    def create(self, vals_list):
        if len(vals_list) > 1 or self.sudo().with_context(active_test=False).search_count([], limit=1):
            raise ValidationError(_("Gental MCP has a single settings record."))
        return super().create(vals_list)

    def write(self, vals):
        if "connect_group_ids" not in vals:
            return super().write(vals)
        before = _connected_users(self.env)
        res = super().write(vals)
        revoke_lost(self.env, before)
        return res

    @private
    @api.model
    def get(self):
        """The settings record (sudo), recreated if it was deleted."""
        rec = self.env.ref("gental_mcp.config_main", raise_if_not_found=False)
        if not rec:
            rec = self.sudo().search([], limit=1) or self.sudo().create({})
            self.env["ir.model.data"].sudo().create({
                "module": "gental_mcp",
                "name": "config_main",
                "model": self._name,
                "res_id": rec.id,
                "noupdate": True,
            })
        return rec.sudo()

    @private
    @api.model
    def action_open_config(self):
        """Window action on the settings record (the Configuration menu's server action calls this)."""
        return {
            "type": "ir.actions.act_window",
            "name": _("Configuration"),
            "res_model": self._name,
            "res_id": self.get().id,
            "view_mode": "form",
            "views": [[False, "form"]],
            "target": "current",
        }

    @private
    def may_connect(self, user):
        """True when ``user`` is active and in one of the groups allowed to connect."""
        return bool(user) and bool(_connected_users(self.env, user))

    @private
    def base_url(self):
        """The address assistants use, normalized: the Public URL when set, else Odoo's ``web.base.url``.

        Read on every call, so a changed ``web.base.url`` takes effect at once.
        """
        self.ensure_one()
        url = (self.public_url or "").strip()
        if not url:
            url = get_param(self.env["ir.config_parameter"].sudo(), "web.base.url") or ""
        return normalize_url(url)

    @private
    def mcp_url(self):
        """The MCP endpoint, ``{base}/mcp``: the OAuth ``resource`` tokens are bound to."""
        self.ensure_one()
        return self.base_url() + "/mcp"

    def action_check_reachability(self):
        """GET the protected-resource metadata of the effective URL and report the result."""
        self.ensure_one()
        base = self.base_url()
        if not base.lower().startswith("https://"):
            raise UserError(_("The public URL must start with https:// so assistants can reach it."))
        url = base + REACHABILITY_PATH
        try:
            response = requests.get(url, timeout=5, allow_redirects=False)
            ok = response.status_code == 200
            detail = _("HTTP %s", response.status_code)
        except requests.RequestException as exc:
            ok, detail = False, type(exc).__name__
        self.reachability = "ok" if ok else "failed"
        if ok:
            message = _("%s answered. Assistants can reach this Odoo.", url)
        else:
            message = _("%(url)s did not answer correctly (%(detail)s). Check the public URL and your proxy.",
                        url=url, detail=detail)
        warning = proxy_warning(
            self.env, request.httprequest.remote_addr if request else None, odoo_config.get("proxy_mode")
        )
        if warning:
            message = f"{message} {warning}"  # a hint only: a LAN or local setup can be fine
        if ok and not (self.public_url or "").strip():
            # Pin the address that just worked: web.base.url changes when an administrator signs in
            # from another address (unless frozen), which would invalidate every token.
            self.public_url = base
            message = f"{message} " + _("Saved %s as the Public URL so it can't change by itself.", base)
        self._message_log(body=message)  # never needs an author email (17.0/18.0 message_post does)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Reachability check"),
                "message": message,
                "type": "success" if ok else "warning",
                "sticky": not ok,
            },
        }

    @api.model
    def _cron_purge_logs(self):
        """Daily: apply the Inbox retention and drop expired tokens, codes and old rate counters."""
        config = self.get()
        now = fields.Datetime.now()
        if config.log_retention_days > 0:
            old_logs = self.env["gental.mcp.log"].sudo().search(
                [("create_date", "<", now - timedelta(days=config.log_retention_days))]
            )
            old_logs.unlink()
        cutoff = now - timedelta(days=EXPIRED_GRACE_DAYS)
        for model in ("gental.mcp.token", "gental.mcp.auth.code"):
            self.env[model].sudo().search([("expires_at", "<", cutoff)]).unlink()
        day_ago_minute = int(time.time() // 60) - 24 * 60
        self.env["gental.mcp.rate"].sudo().search([("window", "<", day_ago_minute)]).unlink()
        return True


class GentalMcpOrigin(models.Model):
    _name = "gental.mcp.origin"
    _description = "Gental MCP Allowed Origin"
    _rec_name = "origin"

    config_id = fields.Many2one("gental.mcp.config", required=True, ondelete="cascade")
    origin = fields.Char(required=True, help="Scheme and host, for example https://claude.ai")
