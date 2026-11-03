import time
from datetime import date, timedelta
from unittest.mock import patch

from odoo import Command

from odoo.addons.gental_mcp import compat
from odoo.addons.gental_mcp.core import meta
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.crypto import hmac_b64url
from odoo.addons.gental_mcp.core.tools import ToolDenied, ToolError, ToolOutcomeUnknown
from odoo.addons.gental_mcp.services import curated
from odoo.addons.gental_mcp.tools import invoicing

from .common import GentalFullCase

SECRET = "gental-identity-secret-for-tests-0123456789"
PHONE = "+966500000001"


def identity_meta(phone=PHONE):
    ts = int(time.time())
    verified_at = "2026-11-02T10:00:00.0000000Z"
    sig = hmac_b64url(SECRET, f"whatsapp|{phone}|{verified_at}|{ts}")
    return {meta.IDENTITY: {"channel": "whatsapp", "phone": phone, "verifiedAt": verified_at, "ts": ts, "sig": sig}}


class TestInvoicingTools(GentalFullCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.staff = cls._make_user("gmcp_billing", "MCP Billing", [
            "base.group_user", "gental_mcp.group_mcp_connect", "account.group_account_invoice",
        ])
        cls.client = cls.env["gental.mcp.client"].create({"name": "inv", "kind": "api_key", "user_id": cls.staff.id})
        cls.user_env = cls.env(user=cls.staff)
        cls.provider = curated.CuratedToolProvider(cls.user_env)
        cls.customer.email = "ahmed@example.com"
        cls.other = cls.env["res.partner"].create({"name": "عميل ثاني", "email": "other@example.com"})
        cls.today = date.today()
        cls.overdue = cls.make_invoice(cls.customer, due=cls.today - timedelta(days=10))
        cls.current = cls.make_invoice(cls.customer, due=cls.today + timedelta(days=20))
        cls.foreign = cls.make_invoice(cls.other, due=cls.today - timedelta(days=10))
        cls.draft = cls.make_invoice(cls.customer, due=cls.today - timedelta(days=10), post=False)

    @classmethod
    def make_invoice(cls, partner, due, post=True, move_type="out_invoice", qty=2):
        values = {
            "move_type": move_type,
            "partner_id": partner.id,
            "invoice_date": due - timedelta(days=15),
            "invoice_date_due": due,
            "invoice_line_ids": [Command.create({"product_id": cls.coffee.id, "quantity": qty, "price_unit": 25.0})],
        }
        move = cls.env["account.move"].create(values)
        if post:
            move.action_post()
        return move

    def ctx(self, call_meta=None, locale="en", user=None):
        user = user or self.staff
        return CallContext(self.env(user=user), user.id, self.client.id, locale, dict(call_meta or {}))

    def call(self, name, args, call_meta=None, locale="en", user=None):
        user = user or self.staff
        provider = curated.CuratedToolProvider(self.env(user=user))
        return provider.call(name, args, self.ctx(call_meta, locale, user))

    def ids(self, result, key="invoices"):
        return {row["id"] for row in result.structured[key]}

    # -- staff tools ------------------------------------------------------------------------------

    def test_list_invoices_filters_overdue(self):
        result = self.call("list_invoices", {"overdue_only": True, "partner_id": self.customer.id})
        self.assertEqual(self.ids(result), {self.overdue.id})
        row = result.structured["invoices"][0]
        self.assertTrue(row["overdue"])
        self.assertEqual(row["amount_residual"], self.overdue.amount_residual)
        self.assertEqual(row["currency"], self.overdue.currency_id.name)
        refund = self.make_invoice(self.customer, due=self.today - timedelta(days=10), move_type="out_refund")
        refund_row = self.call("invoice_details", {"invoice_id": refund.id}).structured
        self.assertFalse(refund_row["overdue"])
        everyone = self.ids(self.call("list_invoices", {"overdue_only": True, "limit": 50}))
        self.assertNotIn(refund.id, everyone)
        self.assertTrue({self.overdue.id, self.foreign.id} <= everyone)
        self.assertNotIn(self.current.id, everyone)
        drafts = self.ids(self.call("list_invoices", {"state": "draft", "partner_id": self.customer.id}))
        self.assertEqual(drafts, {self.draft.id})
        dated = self.ids(self.call("list_invoices", {
            "partner_id": self.customer.id, "state": "posted",
            "date_from": self.current.invoice_date.isoformat(), "date_to": self.current.invoice_date.isoformat()}))
        self.assertEqual(dated, {self.current.id})
        unpaid = self.ids(self.call("list_invoices", {"partner_id": self.customer.id, "payment_state": "not_paid"}))
        self.assertTrue({self.overdue.id, self.current.id} <= unpaid)
        for bad in ({"state": "open"}, {"payment_state": "x"}, {"date_from": "soon"}, {"limit": 51}):
            with self.assertRaises(ToolError):
                self.call("list_invoices", bad)
        schema = curated.REGISTRY["list_invoices"]().input_schema(self.ctx())
        self.assertEqual([o["const"] for o in schema["properties"]["state"]["oneOf"]], ["draft", "posted", "cancel"])
        self.assertTrue(all(o["title"] for o in schema["properties"]["payment_state"]["oneOf"]))

    def test_vendor_bills_never_listed(self):
        vendor = self.env["res.partner"].create({"name": "مورد"})
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": vendor.id, "invoice_date": self.today,
            "invoice_line_ids": [Command.create({"name": "x", "quantity": 1, "price_unit": 10.0})],
        })
        bill.action_post()
        self.assertNotIn(bill.id, self.ids(self.call("list_invoices", {"limit": 50})))
        self.assertNotIn(bill.id, self.ids(self.call("list_invoices", {"partner_id": vendor.id})))
        for name, args in (("invoice_details", {"invoice_id": bill.id}), ("invoice_details", {"number": bill.name}),
                           ("send_invoice", {"invoice_id": bill.id}),
                           ("get_invoice_document", {"invoice_id": bill.id})):
            with self.assertRaises(ToolError, msg=name):
                self.call(name, args)

    def test_details_portal_link_only_with_portal(self):
        result = self.call("invoice_details", {"number": self.overdue.name})
        data = result.structured
        self.assertEqual(data["id"], self.overdue.id)
        self.assertEqual(data["amount_total"], self.overdue.amount_total)
        self.assertEqual(data["amount_residual"], self.overdue.amount_residual)
        self.assertEqual(data["payment_state"], "not_paid")
        self.assertEqual(data["invoice_date_due"], self.overdue.invoice_date_due.isoformat())
        self.assertEqual(len(data["lines"]), 1)
        self.assertEqual(data["lines"][0]["quantity"], 2)
        self.assertIn("/my/invoices/%s" % self.overdue.id, data["portal_url"])
        self.assertIn("access_token=" + self.overdue.access_token, data["portal_url"])
        with patch.object(invoicing, "portal_available", return_value=False):
            without = self.call("invoice_details", {"invoice_id": self.overdue.id}).structured
        self.assertNotIn("portal_url", without)
        self.assertNotIn("portal_url", self.call("invoice_details", {"invoice_id": self.draft.id}).structured)
        paid = self.make_invoice(self.customer, due=self.today)
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=paid.ids).create({}).action_create_payments()
        self.assertNotIn(paid.payment_state, ("not_paid", "partial"))
        self.assertNotIn("portal_url", self.call("invoice_details", {"invoice_id": paid.id}).structured)
        with self.assertRaises(ToolError):
            self.call("invoice_details", {})

    def test_customer_balance_sums(self):
        result = self.call("customer_balance", {"partner_id": self.customer.id})
        data = result.structured
        self.assertEqual(data["receivable"], self.customer.credit)
        self.assertEqual(data["receivable"], self.overdue.amount_residual_signed + self.current.amount_residual_signed)
        self.assertEqual(data["overdue"], self.overdue.amount_residual_signed)
        self.assertEqual(data["overdue_count"], 1)
        self.assertEqual(data["currency"], self.env.company.currency_id.name)
        sales = self._make_user("gmcp_sales_only", "Sales only", [
            "base.group_user", "gental_mcp.group_mcp_connect", "sales_team.group_sale_salesman"])
        with self.assertRaises(ToolError):
            self.call("customer_balance", {"partner_id": self.customer.id}, user=sales)

    # -- verified customer tools -----------------------------------------------------------------

    def test_my_invoices_matched_partner_only(self):
        self.env["gental.mcp.config"].get().set_identity_secret(SECRET)
        contact = self.env["res.partner"].create({"name": "مكتب أحمد", "parent_id": self.customer.id, "type": "invoice"})
        child_invoice = self.make_invoice(contact, due=self.today + timedelta(days=5))
        result = self.call("my_invoices", {}, call_meta=identity_meta())
        self.assertEqual(self.ids(result), {self.overdue.id, self.current.id, child_invoice.id})
        self.assertNotIn(self.foreign.id, self.ids(result))
        self.assertNotIn(self.draft.id, self.ids(result))
        self.assertEqual(self.call("my_invoices", {"limit": 1}, call_meta=identity_meta()).structured["invoices"][0]["id"],
                         self.current.id)  # newest invoice date first
        unpaid = self.ids(self.call("my_invoices", {"unpaid_only": True}, call_meta=identity_meta()))
        self.assertEqual(unpaid, {self.overdue.id, self.current.id, child_invoice.id})
        nobody = self.call("my_invoices", {}, call_meta=identity_meta("+966511111111"))
        self.assertEqual(nobody.structured["invoices"], [])
        self.assertFalse(nobody.is_error)
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["gental_mcp"], ["ar_001"], overwrite=True)
        in_arabic = self.call("my_invoices", {}, call_meta=identity_meta("+966511111111"), locale="ar")
        self.assertEqual(in_arabic.text, "ما قدرت أربط حسابك بعميل في أودو، فما أقدر أعرض لك شي.")
        titles = curated.REGISTRY["list_invoices"]().input_schema(self.ctx(locale="ar"))["properties"]["state"]["oneOf"]
        self.assertEqual(titles[1]["title"], "مرحّلة")
        schema = curated.REGISTRY["my_invoices"]().input_schema(self.ctx())
        self.assertFalse({"partner_id", "phone"} & set(schema["properties"]))
        balance = self.call("my_balance", {}, call_meta=identity_meta()).structured
        self.assertEqual(balance["receivable"], self.customer.credit)
        self.assertEqual(self.call("my_balance", {}, call_meta=identity_meta("+966511111111")).structured, {})
        sales = self._make_user("gmcp_sales_my", "Sales my", [
            "base.group_user", "gental_mcp.group_mcp_connect", "sales_team.group_sale_salesman"])
        with self.assertRaises(ToolError) as caught:
            self.call("my_balance", {}, call_meta=identity_meta(), user=sales)
        self.assertEqual(str(caught.exception), "I can't check the balance right now. The team can help you with it.")

    def test_my_invoices_portal_url_for_open_invoices(self):
        self.env["gental.mcp.config"].get().set_identity_secret(SECRET)
        paid = self.make_invoice(self.customer, due=self.today)
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=paid.ids).create({}).action_create_payments()
        rows = {r["id"]: r for r in self.call("my_invoices", {"limit": 10}, call_meta=identity_meta()).structured["invoices"]}
        self.assertIn("access_token=" + self.overdue.access_token, rows[self.overdue.id]["portal_url"])
        self.assertIn("/my/invoices/%s" % self.current.id, rows[self.current.id]["portal_url"])
        self.assertNotIn("portal_url", rows[paid.id])
        # Staff listings never carry the customer's link.
        staff_rows = self.call("list_invoices", {"partner_id": self.customer.id}).structured["invoices"]
        self.assertFalse(any("portal_url" in r for r in staff_rows))

    def test_my_balance_refused_without_identity(self):
        names = {s.name for s in self.provider.list_tools(self.ctx())}
        self.assertFalse({"my_invoices", "my_balance"} & names)  # no secret: not listed
        self.env["gental.mcp.config"].get().set_identity_secret(SECRET)
        names = {s.name for s in self.provider.list_tools(self.ctx())}
        self.assertTrue({"my_invoices", "my_balance"} <= names)
        for name in ("my_balance", "my_invoices"):
            with self.assertRaises(ToolDenied) as caught:
                self.call(name, {})
            self.assertEqual(str(caught.exception), "This needs a verified customer.")
        self.assertEqual(curated.REGISTRY["my_balance"]().input_schema(self.ctx())["properties"], {})

    # -- send -------------------------------------------------------------------------------------

    def outgoing(self, invoice):
        return self.env["mail.mail"].search([("model", "=", "account.move"), ("res_id", "=", invoice.id)])

    def test_send_invoice_once_per_key(self):
        key = {meta.ACTION_KEY: "send-1"}
        first = self.call("send_invoice", {"invoice_id": self.current.id}, call_meta=key)
        again = self.call("send_invoice", {"invoice_id": self.current.id}, call_meta=key)
        self.assertEqual(first.structured["message_id"], again.structured["message_id"])
        mails = self.outgoing(self.current)
        self.assertEqual(len(mails), 1)
        self.assertIn(self.customer, mails.recipient_ids)
        self.assertNotIn(self.other, mails.recipient_ids)
        self.assertTrue(self.current.is_move_sent)
        self.assertEqual(first.structured["email"], "ahmed@example.com")
        # The same key on another invoice is a different send.
        self.call("send_invoice", {"invoice_id": self.overdue.id}, call_meta=key)
        self.assertEqual(len(self.outgoing(self.overdue)), 1)
        schema = curated.REGISTRY["send_invoice"]().input_schema(self.ctx())
        self.assertEqual(set(schema["properties"]), {"invoice_id"})

    def test_send_draft_refused(self):
        with self.assertRaises(ToolError):
            self.call("send_invoice", {"invoice_id": self.draft.id})
        self.assertFalse(self.outgoing(self.draft))
        no_email = self.env["res.partner"].create({"name": "بدون إيميل"})
        invoice = self.make_invoice(no_email, due=self.today)
        with self.assertRaises(ToolError):
            self.call("send_invoice", {"invoice_id": invoice.id})
        self.assertFalse(self.outgoing(invoice))

    def test_send_outcome_unknown_after_send_failure(self):
        wizard_cls = type(self.env[compat.invoice_send_model(self.env)])
        original = wizard_cls.action_send_and_print

        def send_then_fail(wizard, *args, **kwargs):
            original(wizard, *args, **kwargs)
            raise RuntimeError("smtp down")

        with patch.object(wizard_cls, "action_send_and_print", send_then_fail):
            with self.assertRaises(ToolOutcomeUnknown):
                self.call("send_invoice", {"invoice_id": self.current.id}, call_meta={meta.ACTION_KEY: "k2"})
        # The call's savepoint undid everything: no key, no queued email, not marked sent.
        self.assertFalse(self.env["gental.mcp.action.key"].search([("key", "=", "k2")]))
        self.assertFalse(self.outgoing(self.current))
        self.assertFalse(self.current.is_move_sent)

    def test_send_retryable_error_reraised(self):
        class Busy(Exception):
            pass

        def busy(wizard, *args, **kwargs):
            raise Busy()

        ctx = self.ctx({meta.ACTION_KEY: "k3"})
        ctx.is_retryable = lambda error: isinstance(error, Busy)
        with patch.object(type(self.env[compat.invoice_send_model(self.env)]), "action_send_and_print", busy):
            with self.assertRaises(Busy):
                self.provider.call("send_invoice", {"invoice_id": self.current.id}, ctx)

    def test_send_replay_before_preconditions(self):
        key = {meta.ACTION_KEY: "send-replay"}
        first = self.call("send_invoice", {"invoice_id": self.current.id}, call_meta=key)
        self.current.button_draft()
        again = self.call("send_invoice", {"invoice_id": self.current.id}, call_meta=key)
        self.assertEqual(again.structured["message_id"], first.structured["message_id"])
        self.assertIn("already", again.text)
        self.assertEqual(len(self.outgoing(self.current)), 1)

    def test_send_follower_gets_only_odoo_notification(self):
        """Documents Odoo's behaviour: the email is addressed to the invoice's customer only; a non-customer
        follower subscribed to Discussions is notified by Odoo's usual follower notification."""
        follower = self.env["res.partner"].create({"name": "متابع", "email": "follower@example.com"})
        self.current.message_subscribe(follower.ids, subtype_ids=self.env.ref("mail.mt_comment").ids)
        result = self.call("send_invoice", {"invoice_id": self.current.id})
        message = self.env["mail.message"].browse(result.structured["message_id"])
        self.assertEqual(message.partner_ids, self.customer)  # addressed recipients
        notified = message.notification_ids.filtered(lambda n: n.notification_type == "email").res_partner_id
        self.assertIn(self.customer, notified)
        self.assertIn(follower, notified)  # Odoo's follower notification, not an addressed recipient
        self.assertEqual(self.outgoing(self.current).recipient_ids, self.customer | follower)

    def test_send_ignores_template_recipients(self):
        """The invoice template's own recipients (``partner_to``, ``email_cc``) are never emailed: the mail goes
        to the customer, plus Odoo's usual follower notification."""
        follower = self.env["res.partner"].create({"name": "متابع", "email": "follower@example.com"})
        self.current.message_subscribe(follower.ids, subtype_ids=self.env.ref("mail.mt_comment").ids)
        template = self.env.ref("account.email_template_edi_invoice")
        template.write({"partner_to": str(self.other.id), "email_cc": "cc@example.com", "email_to": "to@example.com"})
        result = self.call("send_invoice", {"invoice_id": self.current.id})
        message = self.env["mail.message"].browse(result.structured["message_id"])
        self.assertEqual(message.partner_ids, self.customer)
        mails = self.outgoing(self.current)
        self.assertEqual(mails.recipient_ids, self.customer | follower)
        for mail in mails:
            self.assertFalse(mail.email_cc)
            self.assertNotIn("to@example.com", mail.email_to or "")
            self.assertNotIn("cc@example.com", mail.email_to or "")

    def test_required_fields_and_annotations(self):
        tools = {n: curated.REGISTRY[n]() for n in (
            "list_invoices", "invoice_details", "customer_balance", "my_invoices", "my_balance", "send_invoice")}
        for name, tool in tools.items():
            self.assertIn("account.move", tool.required_fields(), name)
            for model, fields in tool.required_fields().items():
                if model not in self.env:  # account.move.send.wizard is 18+ (17.0 sends with account.move.send)
                    continue
                for field in fields:
                    self.assertIn(field, self.env[model]._fields, f"{name}: {model}.{field}")
        self.assertFalse(tools["send_invoice"].annotations().read_only)
        self.assertTrue(all(tools[n].annotations().read_only for n in tools if n != "send_invoice"))
        self.assertTrue(tools["my_invoices"].requires_identity and tools["my_balance"].requires_identity)
