import json
from datetime import timedelta
from unittest.mock import patch

from odoo import Command
from odoo.tests import tagged

from odoo.addons.gental_mcp.compat import set_param, user_groups_field
from odoo.addons.gental_mcp.services import field_guard, webhook_detect
from odoo.addons.gental_mcp.services.generic_tools import KEY_FIELDS

from .common import hide_records
from .test_webhook_subscription import HOOK_URL, WebhookCase

BODY_KEYS = {"id", "event", "occurredAt", "database", "model", "recordId", "data"}


@tagged("post_install", "-at_install", "gental_mcp")
class TestWebhookDetect(WebhookCase):
    """Polling detection: watches, snapshots and the delivery outbox (plan 3, task 2).

    A test runs in one transaction, so every ``create_date`` / ``write_date`` written by the ORM is the
    transaction time ``T``, which is also when a new subscription's watches start. Older history is made
    with SQL (``stamp``).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Delivery = cls.env["gental.mcp.webhook.delivery"]
        cls.Watch = cls.env["gental.mcp.webhook.watch"]
        cls.Partner = cls.env["res.partner"]
        cls.now = cls.env.cr.now()

    # -- helpers ------------------------------------------------------------------

    def event(self, code):
        return self.env.ref(f"gental_mcp.webhook_event_{code.replace('.', '_')}")

    def subscribe(self, events, **vals):
        events = self.env["gental.mcp.webhook.event"].browse(
            [e.id if hasattr(e, "id") else self.event(e).id for e in events]
        )
        return self.env["gental.mcp.webhook.subscription"].create({
            "name": "ERP sync",
            "url": HOOK_URL,
            "event_ids": [Command.set(events.ids)],
            "run_as_user_id": self.agent_user.id,
            **vals,
        })

    def expose(self, model, hidden=()):
        ir_model = self.env["ir.model"]._get(model)
        hidden_fields = self.env["ir.model.fields"].search(
            [("model_id", "=", ir_model.id), ("name", "in", list(hidden))]
        )
        exposed = self.env["gental.mcp.exposed.model"].with_context(active_test=False).search(
            [("model_id", "=", ir_model.id)]
        )
        vals = {"hidden_field_ids": [Command.set(hidden_fields.ids)], "active": True}
        if exposed:
            exposed.write(vals)
            return exposed
        return exposed.create({"model_id": ir_model.id, **vals})

    def stamp(self, record, created=None, written=None):
        """Backdate a record's ``create_date`` / ``write_date`` (the ORM always writes the transaction time)."""
        self.env.flush_all()
        if created is not None:
            self.env.cr.execute(f'UPDATE "{record._table}" SET create_date = %s WHERE id = %s', (created, record.id))
        if written is not None:
            self.env.cr.execute(f'UPDATE "{record._table}" SET write_date = %s WHERE id = %s', (written, record.id))
        record.invalidate_recordset(["create_date", "write_date"])

    def scan(self):
        count = webhook_detect.scan(self.env)
        self.env.invalidate_all()
        return count

    def deliveries(self, sub, record=None):
        domain = [("subscription_id", "=", sub.id)]
        if record is not None:
            domain += [("model", "=", record._name), ("res_id", "=", record.id)]
        return self.Delivery.search(domain)

    def body(self, delivery):
        return json.loads(delivery.body)

    # -- watches ------------------------------------------------------------------

    def test_watches_follow_subscription(self):
        self.expose("res.partner")
        self.expose("res.country")
        sub = self.subscribe(["record.created", "contact.created"], exposed_model_ids=[
            Command.set(self.env["gental.mcp.exposed.model"].search(
                [("model", "in", ["res.partner", "res.country"])]).ids),
        ])
        watches = self.Watch.search([("subscription_id", "=", sub.id)])
        self.assertEqual(
            sorted((w.event_id.code, w.model) for w in watches),
            [("contact.created", "res.partner"), ("record.created", "res.country"),
             ("record.created", "res.partner")],
        )
        self.assertTrue(all(w.started_at == w.cursor for w in watches))
        sub.event_ids = [Command.set(self.event("record.created").ids)]
        self.assertEqual(len(self.Watch.search([("subscription_id", "=", sub.id)])), 2)
        sub.active = False
        self.assertFalse(self.Watch.search([("subscription_id", "=", sub.id)]))
        sub.active = True
        self.assertEqual(len(self.Watch.search([("subscription_id", "=", sub.id)])), 2)

    def test_subscription_delivery_counts(self):
        sub = self.subscribe(["contact.created"])
        for state, attempts in (("failed", 8), ("queued", 3), ("queued", 1), ("delivered", 1)):
            self.Delivery.enqueue(sub, "contact.created", "res.partner", 1, {}).sudo().write(
                {"state": state, "attempts": attempts}
            )
        self.assertEqual(len(sub.delivery_ids), 4)
        self.assertEqual(sub.failing_count, 2)

    # -- detection ----------------------------------------------------------------

    def test_contact_created_emits_once(self):
        sub = self.subscribe(["contact.created"])
        partner = self.Partner.create({"name": "مؤسسة الريان"})
        self.scan()
        sent = self.deliveries(sub, partner)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent.event_code, "contact.created")
        self.assertEqual(sent.state, "queued")
        self.assertEqual(self.body(sent)["data"]["name"], "مؤسسة الريان")
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 1)

    def test_cron_scan_commits_each_watch_without_cron_progress_api(self):
        # 17.0/18.0 have no ir.cron._commit_progress: under the cron the scan commits each watch itself and
        # stops on its own budget. Run as a cron without a progress id, which takes that path on every version.
        subs = self.subscribe(["contact.created"]) | self.subscribe(["contact.created"])
        watches = self.Watch.search([("subscription_id", "in", subs.ids)])
        self.assertEqual(len(watches), 2)
        with patch("odoo.addons.gental_mcp.models.webhook_watch.running_as_cron", return_value=True), \
                patch.object(self.env.cr, "commit") as commit:
            webhook_detect.scan(self.env)
            self.assertEqual(commit.call_count, 2)
            self.assertTrue(all(watches.mapped("scanned_at")))
            watches.write({"scanned_at": False})
            commit.reset_mock()
            with patch.object(webhook_detect, "SCAN_BUDGET_SECONDS", 0):
                webhook_detect.scan(self.env)
            self.assertEqual(commit.call_count, 1)  # out of time after the first watch
            self.assertEqual(len(watches.filtered("scanned_at")), 1)

    def test_existing_records_do_not_fire_on_new_subscription(self):
        partner = self.Partner.create({"name": "Old customer"})
        self.stamp(partner, created=self.now - timedelta(minutes=5), written=self.now - timedelta(minutes=5))
        sub = self.subscribe(["contact.created"])
        partner.phone = "0500000009"  # an edit is not a creation
        self.scan()
        self.assertFalse(self.deliveries(sub, partner))

    def test_record_updated_on_exposed_model(self):
        exposed = self.expose("res.partner")
        partner = self.Partner.create({"name": "Updated Co"})
        self.stamp(partner, created=self.now - timedelta(hours=1), written=self.now - timedelta(hours=1))
        sub = self.subscribe(["record.updated"], exposed_model_ids=[Command.set(exposed.ids)])
        self.scan()
        self.assertFalse(self.deliveries(sub, partner))
        partner.phone = "0511111111"
        self.scan()
        sent = self.deliveries(sub, partner)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent.event_code, "record.updated")
        self.assertEqual(self.body(sent)["data"]["phone"], "0511111111")

    def payload_fields(self, model, names):
        return self.env["ir.model.fields"].search([("model", "=", model), ("name", "in", names)])

    def test_hidden_field_not_in_payload(self):
        exposed = self.expose("res.partner", hidden=["vat"])
        chosen = self.payload_fields("res.partner", ["vat", "phone", "image_1920", "signup_token", "name"])
        sub = self.subscribe(["record.created"], exposed_model_ids=[Command.set(exposed.ids)],
                             payload_field_ids=[Command.set(chosen.ids)])
        partner = self.Partner.create({"name": "Hidden VAT", "vat": "300000000000003", "phone": "0522222222"})
        self.scan()
        data = self.body(self.deliveries(sub, partner))["data"]
        self.assertNotIn("vat", data)
        self.assertEqual(data["phone"], "0522222222")
        self.assertFalse({name for name in data if field_guard._SECRET.search(name)})
        self.assertNotIn("image_1920", data)  # binary

    def test_hidden_field_of_archived_allowlist_row_not_in_payload(self):
        exposed = self.expose("res.partner", hidden=["phone"])
        exposed.active = False
        sub = self.subscribe(["contact.created"])
        partner = self.Partner.create({"name": "Archived row", "phone": "0533333333"})
        self.scan()
        data = self.body(self.deliveries(sub, partner))["data"]
        self.assertNotIn("phone", data)
        self.assertEqual(data["name"], "Archived row")

    def test_two_changes_between_scans_emit_one_event_with_latest(self):
        if "crm.lead" in self.env:
            self._two_lead_stage_changes()
        else:
            self._two_partner_type_changes()

    def _two_lead_stage_changes(self):
        reader = self._make_user("gmcp_crm_reader", "CRM Reader", [
            "base.group_user", "gental_mcp.group_mcp_connect", "sales_team.group_sale_salesman_all_leads",
        ])
        stages = self.env["crm.stage"].create([{"name": f"Stage {n}", "sequence": 100 + n} for n in range(3)])
        lead = self.env["crm.lead"].create({"name": "Big order", "type": "opportunity", "stage_id": stages[0].id})
        sub = self.subscribe(["lead.stage_changed"], run_as_user_id=reader.id)
        self.scan()
        self.assertFalse(self.deliveries(sub, lead), "a new lead has not changed stage")
        lead.stage_id = stages[1]
        lead.stage_id = stages[2]
        self.scan()
        sent = self.deliveries(sub, lead)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.body(sent)["data"]["stage_id"], {"id": stages[2].id, "name": "Stage 2"})
        self.scan()
        self.assertEqual(len(self.deliveries(sub, lead)), 1)

    def _two_partner_type_changes(self):
        # A test-only transition event: any change of the partner's address type.
        event = self.env["gental.mcp.webhook.event"].create({
            "code": "test.partner_type", "name": "Partner type changed", "model": "res.partner",
            "kind": "transition", "state_field": "type", "target_values": "[]",
            "payload_fields": '["name", "type"]',
        })
        partner = self.Partner.create({"name": "Typed", "type": "contact"})
        sub = self.subscribe([event])
        self.scan()
        self.assertFalse(self.deliveries(sub, partner))
        partner.type = "invoice"
        partner.type = "delivery"
        self.scan()
        sent = self.deliveries(sub, partner)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.body(sent)["data"]["type"], "delivery")
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 1)

    def test_filter_domain_applies(self):
        # ``employee``: a plain flag on every version (20.0 computes ``is_company`` from the VAT).
        sub = self.subscribe(["contact.created"], filter_domain="[('employee', '=', True)]")
        company = self.Partner.create({"name": "Al Rayyan Est.", "employee": True})
        person = self.Partner.create({"name": "Sara", "employee": False})
        self.scan()
        self.assertEqual(len(self.deliveries(sub, company)), 1)
        self.assertFalse(self.deliveries(sub, person))

    def test_lookback_catches_old_write_date(self):
        sub = self.subscribe(["contact.created"])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        watch.write({"started_at": self.now - timedelta(minutes=30), "cursor": self.now})
        partner = self.Partner.create({"name": "Long transaction"})
        late = self.now - timedelta(minutes=5)  # committed late: older than the cursor
        self.stamp(partner, created=late, written=late)
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 1)
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 1)

    def test_batches_page_through_the_window(self):
        sub = self.subscribe(["contact.created"])
        partners = self.Partner.create([{"name": f"Batch {n}"} for n in range(5)])
        count = webhook_detect.scan(self.env, batch_size=2)
        self.assertGreaterEqual(count, 5)
        self.assertEqual(len(self.Delivery.search([
            ("subscription_id", "=", sub.id), ("res_id", "in", partners.ids)])), 5)

    def test_scan_cap_resumes_where_it_stopped(self):
        sub = self.subscribe(["contact.created"])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        partners = self.Partner.create([{"name": f"Capped {n}"} for n in range(5)])
        cursor = watch.cursor
        with patch.object(webhook_detect, "MAX_BATCHES", 2):
            webhook_detect.scan(self.env, batch_size=2)
            self.env.invalidate_all()
            self.assertTrue(watch.resume_write_date, "4 rows read, more left: the scan stops and keeps its place")
            self.assertEqual(watch.cursor, cursor, "the cursor moves only once the window is done")
            for _ in range(10):
                if not watch.resume_write_date:
                    break
                webhook_detect.scan(self.env, batch_size=2)
                self.env.invalidate_all()
        self.assertFalse(watch.resume_write_date)
        self.assertEqual(len(self.Delivery.search([
            ("subscription_id", "=", sub.id), ("res_id", "in", partners.ids)])), 5)

    def test_payload_with_only_hidden_fields_reads_nothing(self):
        self.expose("res.partner", hidden=["vat"])
        event = self.env["gental.mcp.webhook.event"].create({
            "code": "test.vat_only", "name": "VAT only", "model": "res.partner",
            "kind": "created", "target_values": "[]", "payload_fields": '["vat"]',
        })
        sub = self.subscribe([event])
        partner = self.Partner.create({"name": "VAT only", "vat": "300000000000003"})
        self.scan()
        data = self.body(self.deliveries(sub, partner))["data"]
        self.assertEqual(data, {"id": partner.id})

    def partner_type_event(self, targets='["invoice"]'):
        return self.env["gental.mcp.webhook.event"].create({
            "code": "test.partner_invoice_address", "name": "Became an invoice address", "model": "res.partner",
            "kind": "transition", "state_field": "type", "target_values": targets,
            "payload_fields": '["name", "type"]',
        })

    def test_old_record_already_in_target_state_only_sets_baseline(self):
        old = self.Partner.create({"name": "Old invoice address", "type": "invoice"})
        self.stamp(old, created=self.now - timedelta(hours=1), written=self.now - timedelta(hours=1))
        sub = self.subscribe([self.partner_type_event()])
        self.scan()  # baseline
        old.name = "Old invoice address (renamed)"
        new = self.Partner.create({"name": "New invoice address", "type": "invoice"})
        self.scan()
        self.assertFalse(self.deliveries(sub, old))
        self.assertEqual(len(self.deliveries(sub, new)), 1)

    def test_old_record_reaching_target_after_subscription_fires(self):
        old = self.Partner.create({"name": "Old contact", "type": "contact"})
        self.stamp(old, created=self.now - timedelta(hours=1), written=self.now - timedelta(hours=1))
        sub = self.subscribe([self.partner_type_event()])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        self.assertFalse(watch.baseline_done)
        self.scan()
        self.assertTrue(watch.baseline_done)
        old.type = "invoice"
        self.scan()
        self.assertEqual(len(self.deliveries(sub, old)), 1)
        self.scan()
        self.assertEqual(len(self.deliveries(sub, old)), 1)

    def finish_baseline(self, watch):
        for _ in range(200):
            if watch.baseline_done:
                return
            webhook_detect.scan(self.env, batch_size=2)
            self.env.invalidate_all()
        self.fail("the baseline never finished")

    def test_baseline_continues_across_scans_and_fires_nothing_meanwhile(self):
        olds = self.Partner.create([{"name": f"Old {n}", "type": "contact"} for n in range(5)])
        for partner in olds:
            self.stamp(partner, created=self.now - timedelta(hours=1), written=self.now - timedelta(hours=1))
        sub = self.subscribe([self.partner_type_event()])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        with patch.object(webhook_detect, "MAX_BATCHES", 1):
            webhook_detect.scan(self.env, batch_size=2)
            self.env.invalidate_all()
            self.assertFalse(watch.baseline_done)
            self.assertTrue(watch.baseline_after_id)
            # Ruling R25: a transition while the baseline runs becomes part of it and is not sent.
            olds[-1].type = "invoice"
            during = self.Partner.create({"name": "Created during baseline", "type": "invoice"})
            self.finish_baseline(watch)
        self.assertFalse(self.deliveries(sub), "nothing fires while baselining")
        snapshots = self.env["gental.mcp.webhook.snapshot"].search([("watch_id", "=", watch.id)])
        self.assertLessEqual(set((olds | during).ids), set(snapshots.mapped("res_id")))
        self.scan()
        self.assertFalse(self.Delivery.search([("subscription_id", "=", sub.id)]))
        after = self.Partner.create({"name": "Created after baseline", "type": "contact"})
        self.scan()
        after.type = "invoice"
        self.scan()
        self.assertEqual(len(self.deliveries(sub, after)), 1)
        self.assertFalse(self.Delivery.search([
            ("subscription_id", "=", sub.id), ("res_id", "in", (olds | during).ids)]))

    def test_posted_record_edited_while_baselining_does_not_fire(self):
        olds = self.Partner.create([{"name": f"Invoice address {n}", "type": "invoice"} for n in range(5)])
        for partner in olds:
            self.stamp(partner, created=self.now - timedelta(hours=1), written=self.now - timedelta(hours=1))
        sub = self.subscribe([self.partner_type_event()])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        with patch.object(webhook_detect, "MAX_BATCHES", 1):
            webhook_detect.scan(self.env, batch_size=2)
            self.env.invalidate_all()
            self.assertFalse(watch.baseline_done)
            olds[-1].name = "Invoice address (edited while baselining)"
            self.finish_baseline(watch)
        self.scan()
        self.assertFalse(self.Delivery.search([("subscription_id", "=", sub.id), ("res_id", "in", olds.ids)]))
        new = self.Partner.create({"name": "New invoice address", "type": "invoice"})
        self.scan()
        self.assertEqual(len(self.deliveries(sub, new)), 1)

    def test_unreadable_state_field_flags_subscription(self):
        # ``presence_ids`` is limited to administrators (field groups); the run-as user can read users.
        event = self.env["gental.mcp.webhook.event"].create({
            "code": "test.user_presence", "name": "Presence changed", "model": "res.users",
            "kind": "transition", "state_field": "presence_ids", "target_values": "[]", "payload_fields": "[]",
        })
        sub = self.subscribe([event])
        self.scan()
        self.assertEqual(sub.warning_code, "no_access")
        self.assertFalse(self.env["gental.mcp.webhook.snapshot"].search(
            [("watch_id", "in", self.Watch.search([("subscription_id", "=", sub.id)]).ids)]))

    def test_least_recently_scanned_watch_goes_first(self):
        first = self.subscribe(["contact.created"], name="First")
        second = self.subscribe(["contact.created"], name="Second")
        w1 = self.Watch.search([("subscription_id", "=", first.id)])
        w2 = self.Watch.search([("subscription_id", "=", second.id)])
        w1.write({"scanned_at": self.now})
        w2.write({"scanned_at": self.now - timedelta(minutes=5)})
        order = [job.watch_id for job in self.Watch._scan_jobs() if job.watch_id in (w1.id, w2.id)]
        self.assertEqual(order, [w2.id, w1.id])
        self.scan()
        self.assertTrue(w1.scanned_at and w2.scanned_at)

    def test_draft_invoice_posted_after_subscription_fires_once(self):
        if "account.move" not in self.env:
            self.skipTest("account is not installed")
        accountant = self._make_user("gmcp_accountant3", "Accountant", [
            "base.group_user", "gental_mcp.group_mcp_connect", "account.group_account_invoice",
        ])
        draft = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.Partner.create({"name": "Draft customer"}).id,
            "invoice_line_ids": [Command.create({"name": "Coffee", "quantity": 1, "price_unit": 25.0})],
        })
        self.stamp(draft, created=self.now - timedelta(hours=1), written=self.now - timedelta(hours=1))
        sub = self.subscribe(["invoice.posted"], run_as_user_id=accountant.id)
        self.scan()  # baseline: the draft is recorded as draft
        draft.action_post()
        self.scan()
        self.assertEqual(len(self.deliveries(sub, draft)), 1)
        self.scan()
        self.assertEqual(len(self.deliveries(sub, draft)), 1)

    def test_old_posted_invoice_edited_does_not_fire(self):
        if "account.move" not in self.env:
            self.skipTest("account is not installed")
        accountant = self._make_user("gmcp_accountant2", "Accountant", [
            "base.group_user", "gental_mcp.group_mcp_connect", "account.group_account_invoice",
        ])
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.Partner.create({"name": "Old customer"}).id,
            "invoice_line_ids": [Command.create({"name": "Coffee", "quantity": 1, "price_unit": 25.0})],
        })
        invoice.action_post()
        self.stamp(invoice, created=self.now - timedelta(hours=1), written=self.now - timedelta(hours=1))
        sub = self.subscribe(["invoice.posted"], run_as_user_id=accountant.id)
        self.scan()  # baseline
        invoice.ref = "PO-17"
        self.scan()
        self.assertFalse(self.deliveries(sub, invoice))

    def test_enqueue_is_idempotent_per_event(self):
        sub = self.subscribe(["contact.created"])
        first = self.Delivery.enqueue(sub, "contact.created", "res.partner", 1, {}, "event-1")
        again = self.Delivery.enqueue(sub, "contact.created", "res.partner", 1, {}, "event-1")
        self.assertEqual(first, again)

    def test_watch_comes_back_with_its_exposed_model(self):
        exposed = self.expose("res.partner")
        sub = self.subscribe(["record.created"], exposed_model_ids=[Command.set(exposed.ids)])
        exposed.active = False
        self.scan()
        self.assertFalse(self.Watch.search([("subscription_id", "=", sub.id)]))
        exposed.active = True
        self.scan()
        self.assertEqual(self.Watch.search([("subscription_id", "=", sub.id)]).model, "res.partner")

    def test_run_as_user_without_access_skips_record(self):
        sub = self.subscribe(["contact.created"])
        partner = self.Partner.create({"name": "Secret client"})
        hide_records(self.env, "res.partner", f"[('id', '!=', {partner.id})]", "hide one partner")
        visible = self.Partner.create({"name": "Visible client"})
        self.scan()
        self.assertFalse(self.deliveries(sub, partner))
        self.assertEqual(len(self.deliveries(sub, visible)), 1)
        # Hidden records are routine (multi-company, own-documents rules): no warning (ruling R22).
        self.assertFalse(sub.warning)

    def test_other_company_record_not_sent_without_warning(self):
        sub = self.subscribe(["contact.created"])
        other = self.env["res.company"].create({"name": "Other Co"})
        partner = self.Partner.create({"name": "Other company client", "company_id": other.id})
        self.scan()
        self.assertFalse(self.deliveries(sub, partner))
        self.assertFalse(sub.warning)

    def test_unreadable_model_flags_subscription(self):
        event = self.env["gental.mcp.webhook.event"].create({
            "code": "test.param_created", "name": "Parameter created", "model": "ir.config_parameter",
            "kind": "created", "target_values": "[]", "payload_fields": "[]",
        })
        sub = self.subscribe([event])
        set_param(self.env["ir.config_parameter"], "gental_mcp.test_param", "1")
        self.scan()
        self.assertFalse(self.deliveries(sub))
        self.assertEqual(sub.warning_code, "no_access")
        self.assertTrue(sub.warning)
        self.assertNotEqual(sub.with_context(lang="en_US").warning, False)

    def test_run_as_user_no_longer_connected_stops_sending(self):
        sub = self.subscribe(["contact.created"])
        self.assertFalse(sub.warning)
        self.agent_user.write({user_groups_field(self.env): [
            Command.unlink(self.env.ref("gental_mcp.group_mcp_connect").id)]})
        partner = self.Partner.create({"name": "After removal"})
        self.scan()
        self.assertFalse(self.deliveries(sub, partner))
        self.assertEqual(sub.warning_code, "not_connected")

    def test_warning_cleared_after_clean_scan(self):
        sub = self.subscribe(["contact.created"])
        sub.sudo().warning_code = "scan_failed"
        self.assertTrue(sub.warning)
        self.scan()
        self.assertFalse(sub.warning_code)
        self.assertFalse(sub.warning)

    def test_model_uninstalled_watcher_skipped(self):
        sub = self.subscribe(["contact.created"])
        self.Watch.sudo().create({
            "subscription_id": sub.id, "event_id": self.event("contact.created").id, "model": "x_gone.model",
            "cursor": self.now, "started_at": self.now,
        })
        partner = self.Partner.create({"name": "Still fires"})
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 1)

    def test_invoice_paid_detected_via_recompute(self):
        if "account.move" not in self.env:
            self.skipTest("account is not installed")
        accountant = self._make_user("gmcp_accountant", "Accountant", [
            "base.group_user", "gental_mcp.group_mcp_connect", "account.group_account_invoice",
        ])
        sub = self.subscribe(["invoice.paid"], run_as_user_id=accountant.id)
        customer = self.Partner.create({"name": "Paying customer"})
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": customer.id,
            "invoice_line_ids": [Command.create({"name": "Coffee", "quantity": 2, "price_unit": 25.0})],
        })
        invoice.action_post()
        self.scan()
        self.assertFalse(self.deliveries(sub, invoice))
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids,
        ).create({}).action_create_payments()
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.scan()
        sent = self.deliveries(sub, invoice)
        self.assertEqual(len(sent), 1)
        data = self.body(sent)["data"]
        self.assertEqual(data["payment_state"], invoice.payment_state)
        self.assertEqual(data["partner_id"], {"id": customer.id, "name": customer.display_name})

    # -- body ---------------------------------------------------------------------

    def test_body_shape(self):
        sub = self.subscribe(["contact.created"])
        partner = self.Partner.create({"name": "Shape", "country_id": self.env.ref("base.sa").id})
        self.scan()
        sent = self.deliveries(sub, partner)
        body = self.body(sent)
        self.assertEqual(set(body), BODY_KEYS)
        self.assertEqual(body["event"], "contact.created")
        self.assertEqual(body["model"], "res.partner")
        self.assertEqual(body["recordId"], partner.id)
        self.assertEqual(body["database"], self.env.cr.dbname)
        self.assertEqual(body["id"], sent.event_uuid)
        self.assertNotEqual(sent.event_uuid, sent.delivery_uuid)
        self.assertTrue(body["occurredAt"].endswith("Z"))
        self.assertEqual(body["data"]["country_id"]["id"], self.env.ref("base.sa").id)
        self.assertEqual(sent.attempts, 0)
        self.assertTrue(sent.next_attempt_at)

    def test_body_keeps_arabic_literally(self):
        sub = self.subscribe(["contact.created"])
        partner = self.Partner.create({"name": "مؤسسة الريان"})
        self.scan()
        self.assertIn("مؤسسة الريان", self.deliveries(sub, partner).body)

    def test_one_event_shares_its_id_across_subscriptions(self):
        first = self.subscribe(["contact.created"])
        second = self.subscribe(["contact.created"], name="Second")
        partner = self.Partner.create({"name": "Fan out"})
        self.scan()
        a, b = self.deliveries(first, partner), self.deliveries(second, partner)
        self.assertEqual(a.event_uuid, b.event_uuid)
        self.assertNotEqual(a.delivery_uuid, b.delivery_uuid)

    def test_body_capped_256kb(self):
        exposed = self.expose("res.partner")
        sub = self.subscribe(["record.created"], exposed_model_ids=[Command.set(exposed.ids)],
                             payload_field_ids=[Command.set(self.payload_fields("res.partner", ["comment"]).ids)])
        partner = self.Partner.create({"name": "Huge", "comment": "<p>%s</p>" % ("x" * 300_000)})
        self.scan()
        sent = self.deliveries(sub, partner)
        body = self.body(sent)
        self.assertEqual(body["data"], {"truncated": True})
        self.assertEqual(set(body), BODY_KEYS)
        self.assertLessEqual(len(sent.body.encode()), 256 * 1024)

    # -- review fixes: transitions between targets, bounded work, cursor cap, payload defaults ----------------

    def test_moving_between_two_target_states_fires_once(self):
        # Like invoice.paid: ``in_payment`` then ``paid`` is one event.
        sub = self.subscribe([self.partner_type_event('["invoice", "delivery"]')])
        partner = self.Partner.create({"name": "Two targets", "type": "contact"})
        self.scan()
        partner.type = "invoice"
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 1)
        partner.type = "delivery"
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 1)
        partner.type = "contact"
        self.scan()
        partner.type = "delivery"  # left the targets and came back: a new event
        self.scan()
        self.assertEqual(len(self.deliveries(sub, partner)), 2)

    def test_watch_stops_between_batches_when_the_run_is_almost_over(self):
        sub = self.subscribe(["contact.created"])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        partners = self.Partner.create([{"name": f"Timed {n}"} for n in range(5)])
        cursor = watch.cursor
        with patch.object(webhook_detect, "_out_of_time", return_value=True):
            webhook_detect.scan(self.env, batch_size=2)
            self.env.invalidate_all()
            # One batch of 2 (other partners written during the install may come first), then the watch saves
            # its place.
            self.assertLessEqual(len(self.deliveries(sub)), 2)
            self.assertTrue(watch.resume_write_date)
            self.assertEqual(watch.cursor, cursor)
            for _ in range(50):
                if not watch.resume_write_date:
                    break
                webhook_detect.scan(self.env, batch_size=2)
                self.env.invalidate_all()
        self.assertFalse(watch.resume_write_date)
        self.assertEqual(len(self.Delivery.search([
            ("subscription_id", "=", sub.id), ("res_id", "in", partners.ids)])), 5)

    def test_time_left_follows_the_cron(self):
        self.assertEqual(self.Watch._time_left(0), float("inf"), "outside a cron there is no limit")
        with patch("odoo.addons.gental_mcp.models.webhook_watch.running_as_cron", return_value=True):
            self.assertEqual(self.Watch._time_left(0), 0)
            self.assertEqual(self.Watch._time_left(None), float("inf"))
        with patch("odoo.addons.gental_mcp.models.webhook_watch.time.monotonic", return_value=100.0):
            watch = self.Watch.with_context(ir_cron_progress_id=1, cron_end_time=130.0)
            self.assertEqual(watch._time_left(None), 30.0)

    def test_one_bad_record_does_not_freeze_the_watch(self):
        sub = self.subscribe(["contact.created"])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        good, bad, other = self.Partner.create([{"name": n} for n in ("Good", "Bad", "Other")])
        real = webhook_detect._read

        def read(records, names):
            if bad.id in records.ids:
                raise RuntimeError("broken compute")
            return real(records, names)

        with patch.object(webhook_detect, "_read", side_effect=read), \
                self.assertLogs("odoo.addons.gental_mcp.services.webhook_detect", "INFO"):
            self.scan()
        self.assertEqual(len(self.deliveries(sub, good)), 1)
        self.assertEqual(len(self.deliveries(sub, other)), 1)
        self.assertFalse(self.deliveries(sub, bad))
        self.assertEqual(sub.warning_code, "payload_errors")
        self.assertTrue(sub.warning)
        self.assertFalse(watch.resume_write_date)
        # The bad record got a snapshot: it is not read (and logged) again on every scan.
        self.scan()
        self.assertFalse(sub.warning_code)
        self.assertFalse(self.deliveries(sub, bad))

    def test_failed_detection_is_marked_and_not_retried_until_the_record_changes(self):
        sub = self.subscribe(["contact.created"])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        bad = self.Partner.create({"name": "Detect fails"})
        real = webhook_detect._detect

        def detect(job, row, snapshot, states):
            if row["id"] == bad.id:
                raise RuntimeError("broken")
            return real(job, row, snapshot, states)

        with patch.object(webhook_detect, "_detect", side_effect=detect) as spy:
            with self.assertLogs("odoo.addons.gental_mcp.services.webhook_detect", "ERROR"):
                self.scan()
            self.assertEqual(sub.warning_code, "payload_errors")
            snapshot = self.env["gental.mcp.webhook.snapshot"].search(
                [("watch_id", "=", watch.id), ("res_id", "=", bad.id)])
            self.assertTrue(snapshot.value.startswith(webhook_detect.ERROR_MARKER))
            spy.reset_mock()
            self.scan()  # same version of the record: skipped, nothing logged
            self.assertNotIn(bad.id, [c.args[1]["id"] for c in spy.call_args_list])
            self.assertFalse(sub.warning_code)
        # Written again (here: a later write_date), it is detected anew as if it had no snapshot.
        self.stamp(bad, written=self.now + timedelta(seconds=5))
        self.scan()
        self.assertEqual(len(self.deliveries(sub, bad)), 1)

    def test_generic_payload_defaults_to_key_fields(self):
        exposed = self.expose("res.partner")
        sub = self.subscribe(["record.created"], exposed_model_ids=[Command.set(exposed.ids)])
        partner = self.Partner.create({"name": "Key fields", "comment": "<p>long notes</p>", "phone": "0544444444"})
        self.scan()
        data = self.body(self.deliveries(sub, partner))["data"]
        self.assertLessEqual(set(data), {"id", "display_name", *KEY_FIELDS})
        self.assertLessEqual(len(data), 10)
        self.assertEqual((data["display_name"], data["phone"]), ("Key fields", "0544444444"))
        self.assertNotIn("comment", data)
        # Fields the subscription chose on this model replace the default.
        sub.payload_field_ids = [Command.set(self.payload_fields("res.partner", ["comment"]).ids)]
        other = self.Partner.create({"name": "Chosen fields", "comment": "<p>notes</p>"})
        self.scan()
        self.assertEqual(set(self.body(self.deliveries(sub, other))["data"]), {"id", "comment"})

    def test_cursor_stays_behind_the_oldest_open_transaction(self):
        sub = self.subscribe(["contact.created"])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        start = self.now - timedelta(hours=2)
        watch.write({"cursor": start, "started_at": start})
        self.Partner.create({"name": "During a long transaction"})
        oldest = self.now - timedelta(hours=1)
        with patch.object(type(self.Watch), "_oldest_open_transaction", return_value=oldest):
            self.scan()
        self.assertEqual(watch.cursor, oldest.replace(microsecond=0) - webhook_detect.OPEN_TRANSACTION_MARGIN)
        with patch.object(type(self.Watch), "_oldest_open_transaction", return_value=start - timedelta(hours=1)):
            self.scan()
        self.assertEqual(watch.cursor, oldest.replace(microsecond=0) - webhook_detect.OPEN_TRANSACTION_MARGIN,
                         "a cursor never moves back")
        with patch.object(type(self.Watch), "_oldest_open_transaction", return_value=None):
            self.scan()
        self.assertGreater(watch.cursor, oldest)
        # The real query runs (other connections of this database, if any).
        found = self.Watch._oldest_open_transaction()
        self.assertTrue(found is None or found.tzinfo is None)

    def test_occurred_at_is_when_the_record_changed(self):
        sub = self.subscribe(["contact.created"])
        watch = self.Watch.search([("subscription_id", "=", sub.id)])
        watch.write({"started_at": self.now - timedelta(minutes=30), "cursor": self.now})
        partner = self.Partner.create({"name": "Earlier"})
        earlier = (self.now - timedelta(minutes=4)).replace(microsecond=0)
        self.stamp(partner, created=earlier, written=earlier)
        self.scan()
        self.assertEqual(self.body(self.deliveries(sub, partner))["occurredAt"], earlier.isoformat() + "Z")
