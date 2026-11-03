from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import Form, tagged

from .test_webhook_subscription import GETADDRINFO, HOOK_URL, WebhookCase, public_addrinfo


@tagged("post_install", "-at_install", "gental_mcp")
class TestWebhookRetention(WebhookCase):
    """The webhook purge: old deliveries, orphan snapshots and snapshots outside the scan window (plan 3, task 5)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Delivery = cls.env["gental.mcp.webhook.delivery"]
        cls.Watch = cls.env["gental.mcp.webhook.watch"]
        cls.Snapshot = cls.env["gental.mcp.webhook.snapshot"]
        cls.config = cls.env["gental.mcp.config"].get()
        with patch(GETADDRINFO, side_effect=public_addrinfo):
            cls.sub = cls.env["gental.mcp.webhook.subscription"].create({
                "name": "ERP sync",
                "url": HOOK_URL,
                "event_ids": [(6, 0, cls.env.ref("gental_mcp.webhook_event_contact_created").ids)],
                "run_as_user_id": cls.agent_user.id,
            })

    def delivery(self, state, age_days, n=[0]):
        n[0] += 1
        row = self.Delivery.sudo().create({
            "subscription_id": self.sub.id, "event_code": "contact.created", "event_uuid": f"ev-{self._testMethodName}-{n[0]}",
            "model": "res.partner", "res_id": 1, "body": "{}", "state": state,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE gental_mcp_webhook_delivery SET write_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=age_days), row.id),
        )
        row.invalidate_recordset(["write_date"])
        return row

    def test_default_is_90_days(self):
        self.assertEqual(self.config.webhook_log_retention_days, 90)

    def test_purge_respects_retention(self):
        old_ok, new_ok = self.delivery("delivered", 91), self.delivery("delivered", 89)
        old_failed, old_queued = self.delivery("failed", 91), self.delivery("queued", 91)
        self.assertEqual(self.Delivery.purge(), 2)
        self.assertFalse((old_ok | old_failed).exists())
        self.assertTrue((new_ok | old_queued).exists())

    def test_zero_keeps_everything(self):
        self.config.webhook_log_retention_days = 0
        old = self.delivery("delivered", 400)
        self.Delivery.purge()
        self.assertTrue(old.exists())

    def test_cron_runs_the_purge(self):
        old = self.delivery("delivered", 91)
        cron = self.env.ref("gental_mcp.cron_webhook_purge")
        self.assertEqual((cron.model_name, cron.code, cron.interval_type), (self.Delivery._name, "model._cron_purge()", "days"))
        self.Delivery._cron_purge()
        self.assertFalse(old.exists())

    def watch(self, event):
        """A watch of ``res.partner`` for ``event`` (its own model doesn't matter here)."""
        return self.Watch.sudo().create({
            "subscription_id": self.sub.id, "event_id": event.id, "model": "res.partner",
            "cursor": fields.Datetime.now(),
        })

    def event(self, kind):
        return self.env["gental.mcp.webhook.event"].search([("kind", "=", kind), ("generic", "=", False)], limit=1)

    def test_purge_removes_orphan_snapshots(self):
        watch = self.Watch.sudo().search([("subscription_id", "=", self.sub.id)], limit=1)
        self.assertEqual(watch.event_id.kind, "created")
        keep = self.env["res.partner"].create({"name": "Keeps"})
        gone = self.env["res.partner"].create({"name": "Goes"})
        self.Snapshot._store(watch.id, {keep.id: "x", gone.id: "x"})
        # A snapshot of an existing record in the scan window stays; a deleted record's goes.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE gental_mcp_webhook_snapshot SET value = %s", (fields.Datetime.now().isoformat() + "Z",),
        )
        gone.unlink()
        self.Delivery.purge()
        self.assertEqual(self.Snapshot.search([("watch_id", "=", watch.id)]).mapped("res_id"), [keep.id])

    def test_purge_prunes_old_created_snapshots_but_not_transitions(self):
        created = self.Watch.sudo().search([("subscription_id", "=", self.sub.id)], limit=1)
        transition = self.watch(self.event("transition"))
        partner = self.env["res.partner"].create({"name": "Old"})
        recent = self.env["res.partner"].create({"name": "Recent"})
        old_iso = (fields.Datetime.now() - timedelta(days=3)).isoformat(timespec="seconds") + "Z"
        new_iso = fields.Datetime.now().isoformat(timespec="seconds") + "Z"
        self.Snapshot._store(created.id, {partner.id: old_iso, recent.id: new_iso})
        self.Snapshot._store(transition.id, {partner.id: '"posted"'})
        self.Snapshot.invalidate_model()
        self.Delivery.purge()
        self.assertEqual(self.Snapshot.search([("watch_id", "=", created.id)]).mapped("res_id"), [recent.id])
        self.assertEqual(self.Snapshot.search([("watch_id", "=", transition.id)]).mapped("res_id"), [partner.id])

    def test_retention_field_on_config_form(self):
        with Form(self.config) as form:
            form.webhook_log_retention_days = 30
        self.assertEqual(self.config.webhook_log_retention_days, 30)

    def queued_since(self, age):
        row = self.delivery("queued", 0)
        self.env.cr.execute(
            "UPDATE gental_mcp_webhook_delivery SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - age, row.id),
        )
        row.invalidate_recordset(["create_date"])
        return row

    def test_purge_removes_old_queued_deliveries_of_archived_webhooks(self):
        old, recent = self.queued_since(timedelta(days=91)), self.queued_since(timedelta(days=10))
        self.assertEqual(self.Delivery.purge(), 0, "an active webhook's queue is never purged")
        self.sub.active = False
        self.assertEqual(self.Delivery.purge(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_restored_webhook_expires_stale_queued_deliveries(self):
        stale, fresh = self.queued_since(timedelta(hours=30)), self.queued_since(timedelta(hours=2))
        failed = self.delivery("failed", 3)
        self.sub.active = False
        self.sub.active = True
        self.assertEqual((stale.state, fresh.state, failed.state), ("expired", "queued", "failed"))
        # Expired deliveries go with the finished ones.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE gental_mcp_webhook_delivery SET write_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=91), stale.id),
        )
        self.Delivery.purge()
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
