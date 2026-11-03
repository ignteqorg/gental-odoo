from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..compat import check_read_access
from ..core.crypto import new_secret
from ..core.tools import ToolError
from ..services import domain_guard, field_guard, net_guard
from .exposed_model import parse_domain
from .res_users import _connected_users

SECRET_PREFIX = "gmwh"
TEST_EVENT = "test.ping"
# Values that change what a subscription watches (``gental.mcp.webhook.watch._sync``).
WATCHED_KEYS = frozenset({"event_ids", "exposed_model_ids", "active"})
# Deliveries still queued when an archived subscription is restored are sent only if they are younger than this;
# older ones are marked expired (the receiver would get stale events).
UNARCHIVE_MAX_AGE = timedelta(hours=24)
# Values the filter is validated against (``_check_filter_domain``, ``_check_filter_for_editor``).
FILTER_KEYS = frozenset({"filter_domain", "event_ids", "exposed_model_ids", "run_as_user_id"})
# Circuit breaker: after this many failed attempts in a row, the dispatch cron leaves a subscription alone for
# ``BREAKER_PAUSE``. After the pause one delivery is tried; another failure pauses it again, a success resets it.
BREAKER_FAILURES = 5
BREAKER_PAUSE = timedelta(minutes=30)
MANAGER_GROUP = "gental_mcp.group_mcp_manager"


def url_host(url):
    """The host of ``url`` (the part that is safe to list and log), or ``""``."""
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


def _new_webhook_secret():
    return new_secret(SECRET_PREFIX)


class GentalMcpWebhookSubscription(models.Model):
    """Where to send which events, signed with the subscription's secret (spec §10)."""

    _name = "gental.mcp.webhook.subscription"
    _description = "Gental MCP Webhook Subscription"
    _inherit = ["mail.thread"]

    name = fields.Char(required=True, tracking=True)
    # The path or query of a webhook URL often carries a token: never tracked in the chatter, never logged, and
    # listed by host only (``url_host``).
    url = fields.Char(
        string="URL", required=True, groups=MANAGER_GROUP, help="An https address on the public internet.",
    )
    url_host = fields.Char(string="URL Host", compute="_compute_url_host")
    event_ids = fields.Many2many(
        "gental.mcp.webhook.event", "gental_mcp_webhook_subscription_event_rel", "subscription_id", "event_id",
        string="Events", required=True, domain="[('available', '=', True)]",
    )
    exposed_model_ids = fields.Many2many(
        "gental.mcp.exposed.model", "gental_mcp_webhook_subscription_exposed_rel", "subscription_id",
        "exposed_model_id", string="Models",
        help="For the “Record created” and “Record updated” events: the exposed models to watch.",
    )
    # The ids the Payload Fields picker filters on.
    exposed_ir_model_ids = fields.Many2many("ir.model", string="Watched Models", compute="_compute_exposed_ir_models")
    payload_field_ids = fields.Many2many(
        "ir.model.fields", "gental_mcp_webhook_subscription_payload_field_rel", "subscription_id", "field_id",
        string="Payload Fields", domain="[('model_id', 'in', exposed_ir_model_ids)]",
        help="For the “Record created” and “Record updated” events: the fields sent. Empty sends the record's "
             "name and a few key fields. Hidden fields and fields the run-as user can't read are never sent.",
    )
    filter_domain = fields.Char(
        string="Filter", default="[]",
        help="Only records matching this filter are sent. Every chosen event must be on the same model.",
    )
    # An HMAC key: signing needs the plaintext, so it is stored as is, readable by managers only and never
    # written to the chatter or the logs.
    secret = fields.Char(
        string="Signing Secret", default=lambda self: _new_webhook_secret(), copy=False,
        groups="gental_mcp.group_mcp_manager",
    )
    run_as_user_id = fields.Many2one(
        "res.users", string="Run As", required=True, default=lambda self: self.env.user, ondelete="restrict",
        tracking=True, help="Payloads only hold what this user can read.",
    )
    active = fields.Boolean(default=True, tracking=True)
    # Set by the scanner; shown translated in the reader's language.
    warning_code = fields.Selection([
        ("no_access", "The run-as user isn't allowed to read these records, so nothing is being sent."),
        ("scan_failed", "Some changes couldn't be checked. The server log has the details."),
        ("not_connected", "The run-as user is archived or no longer allowed to connect, so nothing is being sent."),
        ("payload_errors", "Some records couldn't be read for sending and were skipped. The server log has the details."),
    ], readonly=True, copy=False)
    warning = fields.Char(compute="_compute_warning")
    # Circuit breaker, kept by the sender: failed attempts in a row, and until when sending is paused after too many.
    consecutive_failures = fields.Integer(readonly=True, copy=False)
    paused_until = fields.Datetime(readonly=True, copy=False)
    delivery_ids = fields.One2many("gental.mcp.webhook.delivery", "subscription_id", string="Deliveries", copy=False)
    failing_count = fields.Integer(
        compute="_compute_failing_count", help="Deliveries that failed, or are still retrying after 3 attempts.",
    )

    show_models = fields.Boolean(compute="_compute_event_scope")
    filter_model = fields.Char(compute="_compute_event_scope", help="The model the filter applies to, when there is one.")

    @api.depends("url")
    def _compute_url_host(self):
        for rec in self:
            rec.url_host = url_host(rec.url)

    @api.depends("exposed_model_ids")
    def _compute_exposed_ir_models(self):
        for rec in self:
            rec.exposed_ir_model_ids = rec.exposed_model_ids.model_id

    @api.depends("event_ids", "exposed_model_ids")
    def _compute_event_scope(self):
        for rec in self:
            rec.show_models = any(rec.event_ids.mapped("generic"))
            models_ = rec._event_models()
            rec.filter_model = next(iter(models_)) if len(models_) == 1 else False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_run_as_value(vals.get("run_as_user_id", self.env.uid))
        records = super().create([self._normalize_vals(vals) for vals in vals_list])
        records._check_events()  # constraints skip fields left out of the values
        records._check_run_as_policy()
        records._check_filter_for_editor()
        self.env["gental.mcp.webhook.watch"]._sync(records)
        return records

    def write(self, vals):
        self._check_own_subscriptions()
        if "run_as_user_id" in vals:
            self._check_run_as_value(vals["run_as_user_id"])
        restored = self.sudo().filtered(lambda rec: not rec.active) if vals.get("active") else self.browse()
        res = super().write(self._normalize_vals(vals))
        if "run_as_user_id" in vals or "url" in vals:
            self._check_run_as_policy()
        if FILTER_KEYS & set(vals):
            self._check_filter_for_editor()
        if WATCHED_KEYS & set(vals):
            self.env["gental.mcp.webhook.watch"]._sync(self)
        if restored:
            self.env["gental.mcp.webhook.delivery"]._expire_stale(restored, UNARCHIVE_MAX_AGE)
        return res

    def unlink(self):
        self._check_own_subscriptions()
        return super().unlink()

    def _may_run_as_anyone(self):
        return self.env.su or self.env.user.has_group("base.group_system")

    def _check_run_as_value(self, user_id):
        """Refuse a non-administrator choosing someone else as the run-as user (before anything is written)."""
        if self._may_run_as_anyone():
            return
        if isinstance(user_id, models.BaseModel):
            user_id = user_id.id
        if (user_id or False) != self.env.uid:
            raise ValidationError(
                _("Only administrators can set up a webhook that runs as another user. Run it as yourself.")
            )

    def _check_own_subscriptions(self):
        """A non-administrator may change or delete only the subscriptions that run as them.

        Anything else (events, models, filter, URL, secret) would let a manager read data as that user through
        the Outbox. Read with sudo so the refusal is this message, not a record-rule error.
        """
        if self._may_run_as_anyone():
            return
        if any(rec.run_as_user_id != self.env.user for rec in self.sudo()):
            raise ValidationError(_("Only administrators can change a webhook that runs as another user."))

    @api.model
    def _normalize_vals(self, vals):
        """Store the URL exactly as it is validated."""
        if isinstance(vals.get("url"), str):
            vals = dict(vals, url=vals["url"].strip())
        return vals

    @api.constrains("url")
    def _check_url(self):
        for rec in self:
            try:
                net_guard.check_url(rec.url or "")
            except ValueError:
                raise ValidationError(
                    _("The webhook URL must start with https:// and point to an address on the public internet.")
                ) from None

    @api.constrains("event_ids", "exposed_model_ids")
    def _check_events(self):
        for rec in self:
            if not rec.event_ids:
                raise ValidationError(_("Choose at least one event."))
            generic = any(rec.event_ids.mapped("generic"))
            if generic and not rec.exposed_model_ids:
                raise ValidationError(_("Choose the models to watch for “Record created” or “Record updated”."))
            if not generic and rec.exposed_model_ids:
                raise ValidationError(_("Models are only used with “Record created” or “Record updated”."))

    @api.constrains("run_as_user_id")
    def _check_run_as_user(self):
        for rec in self:
            if not _connected_users(self.env, rec.run_as_user_id):
                raise ValidationError(
                    _("The run-as user must be active and in a group allowed to connect to Gental MCP.")
                )

    def _check_run_as_policy(self):
        """Only administrators may send another user's data: a manager runs subscriptions as themselves.

        The URL counts too, or a manager could point someone else's subscription at their own receiver.
        Called from ``create`` / ``write`` because it depends on who is writing (constraints may run as
        superuser).
        """
        if self.env.su or self.env.user.has_group("base.group_system"):
            return
        for rec in self:
            if rec.run_as_user_id != self.env.user:
                raise ValidationError(
                    _("Only administrators can set up a webhook that runs as another user. Run it as yourself.")
                )

    @api.constrains("filter_domain", "event_ids", "exposed_model_ids", "run_as_user_id")
    def _check_filter_domain(self):
        for rec in self:
            try:
                domain = parse_domain(rec.filter_domain)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                raise ValidationError(_("The filter is not a valid domain.")) from None
            if not domain:
                continue
            models_ = rec._event_models()
            if len(models_) != 1:
                raise ValidationError(_("A filter works only when every chosen event is on the same model."))
            rec._validate_filter(next(iter(models_)), domain)

    def _check_filter_for_editor(self):
        """The filter may use only fields the editor may filter on too: it tests values, so an administrator
        running a subscription as someone else must not probe fields hidden from themselves.

        Called from ``create`` / ``write`` because constraints run as superuser.
        """
        if self.env.su:
            return
        for rec in self:
            if rec.sudo().run_as_user_id == self.env.user:
                continue  # the constraint already checked this user's fields
            domain = parse_domain(rec.filter_domain)
            models_ = rec._event_models()
            if domain and len(models_) == 1:
                rec._validate_filter(next(iter(models_)), domain, editor=self.env.user)

    def _event_models(self):
        """Models the chosen events apply to: the curated events' own, plus the exposed ones for generic events."""
        self.ensure_one()
        models_ = {event.model for event in self.event_ids if not event.generic}
        if any(self.event_ids.mapped("generic")):
            models_ |= set(self.exposed_model_ids.mapped("model"))
        return models_

    def _validate_filter(self, model, domain, editor=None):
        """Same rules as the assistants' domains, for the fields the run-as user (and the ``editor``, when
        given) may filter on."""
        self.ensure_one()
        if model not in self.env:
            raise ValidationError(_("The app for this event is not installed."))
        exposed = SimpleNamespace(model=model, hidden=self._hidden_fields(model))
        run_as_env = self.env(user=self.run_as_user_id.id, su=False)
        try:
            allowed = field_guard.filterable_fields(run_as_env, exposed)
        except AccessError:
            raise ValidationError(_("The run-as user can't read these records.")) from None
        if editor is not None:
            try:
                editable = field_guard.filterable_fields(self.env(user=editor.id, su=False), exposed)
            except AccessError:
                raise ValidationError(_("You can't read these records, so you can't filter them.")) from None
            allowed = {name: info for name, info in allowed.items() if name in editable}
        relational = {name for name, info in allowed.items() if info["type"] in field_guard.RELATIONAL_TYPES}
        try:
            domain_guard.validate(domain, allowed, env=self.env, relational=relational)
        except ToolError as exc:
            raise ValidationError(_("The filter is not allowed: %s", exc)) from None

    @api.model
    def _hidden_fields(self, model):
        """Field names hidden on ``model`` by any allowlist row, archived rows included.

        Archiving a row stops exposing the model to assistants; it must not un-hide fields from webhooks.
        Sudo: managers may not read every row; only names leave this method.
        """
        rows = self.env["gental.mcp.exposed.model"].sudo().with_context(active_test=False).search(
            [("model", "=", model)]
        )
        return frozenset(rows.hidden_field_ids.mapped("name"))

    def _compute_failing_count(self):
        Delivery = self.env["gental.mcp.webhook.delivery"]
        for rec in self:
            rec.failing_count = Delivery.search_count([
                ("subscription_id", "=", rec.id),
                "|", ("state", "=", "failed"), "&", ("state", "=", "queued"), ("attempts", ">=", 3),
            ]) if rec.id else 0

    @api.depends("warning_code", "paused_until")
    @api.depends_context("lang")
    def _compute_warning(self):
        labels = dict(self._fields["warning_code"]._description_selection(self.env))
        now = fields.Datetime.now()
        for rec in self:
            if rec.warning_code:
                rec.warning = labels.get(rec.warning_code, False)
            elif rec.paused_until and rec.paused_until > now:
                rec.warning = _(
                    "The receiver keeps failing, so sending is paused for a while. It resumes on its own."
                )
            else:
                rec.warning = False

    @api.model
    def _attention_domain(self):
        """Subscriptions with a warning: one set by the scanner, or a paused sender."""
        return ["|", ("warning_code", "!=", False), ("paused_until", ">", fields.Datetime.now())]

    @api.model
    def _set_scan_warnings(self, warnings):
        """Write the scanner's ``{subscription id: warning code or False}``, only where it changed (sudo)."""
        for rec in self.sudo().browse(list(warnings)).exists():
            code = warnings[rec.id] or False
            if (rec.warning_code or False) != code:
                rec.warning_code = code

    def _record_send_outcome(self, ok, now):
        """Keep the circuit breaker after one attempt (sudo: bookkeeping, called by the sender).

        A success resets it. After ``BREAKER_FAILURES`` failures in a row, the cron leaves the subscription's
        deliveries alone for ``BREAKER_PAUSE``; each further failure pauses it again.
        """
        rec = self.sudo()
        rec.ensure_one()
        if ok:
            if rec.consecutive_failures or rec.paused_until:
                rec.write({"consecutive_failures": 0, "paused_until": False})
            return
        failures = rec.consecutive_failures + 1
        vals = {"consecutive_failures": failures}
        if failures >= BREAKER_FAILURES:
            vals["paused_until"] = now + BREAKER_PAUSE
        rec.write(vals)

    def action_open_deliveries(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("gental_mcp.action_webhook_deliveries")
        action["domain"] = [("subscription_id", "=", self.id)]
        action["name"] = _("Deliveries")
        return action

    def action_rotate_secret(self):
        """Replace the signing secret. The chatter notes the rotation, never the value."""
        for rec in self:
            rec.secret = _new_webhook_secret()
            rec._message_log(body=_("Signing secret rotated. Update it in the receiving system."))
        return True

    def action_send_test(self):
        """Queue a ``test.ping`` delivery and send it now; a notification tells how it went."""
        self.ensure_one()
        Delivery = self.env["gental.mcp.webhook.delivery"]
        Delivery._check_manager()
        check_read_access(self)
        if not self.active:
            raise UserError(_("This webhook is archived. Restore it before sending a test."))
        delivery = Delivery.enqueue(self, TEST_EVENT, self._name, self.id, {"message": "Gental MCP test"})
        delivery._dispatch_now()
        delivery = delivery.sudo()  # bookkeeping row, read back for the notification only
        delivery.invalidate_recordset()
        if delivery.state == "delivered":
            kind, message = "success", _("Test delivered (HTTP %s).", delivery.last_status_code)
        else:
            reason = delivery.last_error or self.sudo().warning or _("It's queued and will be sent shortly.")
            kind, message = "warning", _("Test not delivered: %s", reason)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Webhook test"), "message": message, "type": kind, "sticky": False},
        }
