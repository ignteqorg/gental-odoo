from datetime import timedelta
from unittest.mock import patch

from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.gental_mcp import compat

from .common import GentalMcpCase

EVENT = "invoice.paid"


@tagged("post_install", "-at_install", "gental_mcp")
class TestWebhookScreens(GentalMcpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        event = cls.env.ref("gental_mcp.webhook_event_invoice_paid")
        with patch("odoo.addons.gental_mcp.services.net_guard.check_url"):
            cls.sub = cls.env["gental.mcp.webhook.subscription"].with_user(cls.manager_user).create({
                "name": "Billing receiver",
                "url": "https://hooks.example.com/odoo",
                "event_ids": [(6, 0, event.ids)],
                "run_as_user_id": cls.manager_user.id,
            })

    def _delivery(self, state="queued", attempts=0, **vals):
        Delivery = self.env["gental.mcp.webhook.delivery"].sudo()
        return Delivery.create({
            "subscription_id": self.sub.id, "event_code": EVENT, "event_uuid": f"ev-{Delivery.search_count([])}",
            "model": "account.move", "res_id": 7, "body": '{"data": {"name": "INV/1"}}',
            "state": state, "attempts": attempts, **vals,
        })

    def test_views_load_for_manager(self):
        for xmlid, modes in (
            ("gental_mcp.action_webhook_subscriptions", ("list", "form")),
            ("gental_mcp.action_webhook_deliveries", ("list", "form")),
        ):
            model = self.env.ref(xmlid).res_model
            res = self.env[model].with_user(self.manager_user).get_views([[False, m] for m in modes] + [[False, "search"]])
            self.assertTrue(set(modes) <= set(res["views"]), xmlid)

    def test_webhooks_page_in_config_form(self):
        config = self.env["gental.mcp.config"].with_user(self.manager_user).get()
        arch = config.get_view(view_type="form")["arch"]
        tree = etree.fromstring(arch)
        pages = tree.xpath("//notebook[@name='mcp_tabs']/page[@name='webhooks']")
        self.assertEqual(len(pages), 1)
        self.assertEqual(config.webhook_subscription_ids, self.sub)

    def test_secret_not_in_lists_or_readable_by_connect_user(self):
        arch = self.env["gental.mcp.webhook.subscription"].get_view(view_type=compat.list_view_type(self.env))["arch"]
        self.assertNotIn("secret", arch)
        with self.assertRaises(AccessError):
            self.sub.with_user(self.agent_user).secret  # noqa: B018
        self.assertTrue(self.sub.with_user(self.manager_user).secret.startswith("gmwh"))

    def test_stats_counts(self):
        since = fields.Datetime.now() - timedelta(minutes=1)
        before = self.env["gental.mcp.webhook.delivery"].stats(since)
        self._delivery("delivered")
        self._delivery("delivered")
        self._delivery("failed", attempts=8)
        self._delivery("queued", attempts=3)
        after = self.env["gental.mcp.webhook.delivery"].stats(since)
        self.assertEqual(
            {k: after[k] - before[k] for k in after}, {"delivered": 2, "failing": 2, "queued": 1},
        )

    def test_dashboard_shows_webhook_counts(self):
        self._delivery("delivered")
        data = self.env["gental.mcp.dashboard"].with_user(self.manager_user).get_data(7)
        self.assertGreaterEqual(data["webhooks"]["delivered"], 1)
        self.assertEqual(set(data["webhooks"]), {"delivered", "failing", "queued"})

    def test_attention_items_lists_failed_and_warnings(self):
        self._delivery("failed", attempts=8)
        self.sub.sudo().warning_code = "no_access"
        items = self.env["gental.mcp.webhook.delivery"].attention_items()
        failed = [i for i in items if i["kind"] == "webhook_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("hooks.example.com", failed[0]["label"])
        self.assertEqual(failed[0]["count"], 1)
        self.assertEqual(failed[0]["action"]["res_model"], "gental.mcp.webhook.delivery")
        warned = [i for i in items if i["kind"] == "webhook_warning"]
        self.assertEqual(len(warned), 1)
        rows = self.env["gental.mcp.dashboard"].with_user(self.manager_user).get_data(7)["attention"]
        self.assertIn("webhook_failed", {r["kind"] for r in rows})

    def test_archived_run_as_user_flags_subscription(self):
        delivery = self._delivery("queued")
        self.manager_user.active = False
        delivery._dispatch_now()
        self.assertEqual(self.sub.sudo().warning_code, "not_connected")
        self.assertEqual(delivery.sudo().attempts, 0)

    def test_ar_event_names(self):
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["gental_mcp"], ["ar_001"], overwrite=True)
        event = self.env.ref("gental_mcp.webhook_event_invoice_paid").with_context(lang="ar_001")
        self.assertEqual(event.name, "تسدّدت فاتورة")

    def test_delivery_display_fields(self):
        delivery = self._delivery("failed", attempts=8, body='{"a": "ب"}', last_error="<b>boom</b>")
        self.assertEqual(delivery.url_host, "hooks.example.com")
        self.assertEqual(delivery.record_label, "account.move,7")
        self.assertIn('"a": "ب"', delivery.body_pretty)
        self.assertNotIn("Signature", delivery.headers_text)
