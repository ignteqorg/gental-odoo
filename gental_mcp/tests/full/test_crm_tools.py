from datetime import date

from odoo.addons.gental_mcp.core import meta
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.tools import ToolError
from odoo.addons.gental_mcp.services import curated

from ..common import hide_records
from .common import GentalFullCase


class TestCrmTools(GentalFullCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sales_user = cls._make_user(
            "gmcp_sales", "MCP Sales",
            ["base.group_user", "base.group_partner_manager", "sales_team.group_sale_salesman",
             "gental_mcp.group_mcp_connect"],
        )
        cls.client = cls.env["gental.mcp.client"].create({"name": "crm", "kind": "api_key", "user_id": cls.sales_user.id})
        cls.user_env = cls.env(user=cls.sales_user)
        cls.provider = curated.CuratedToolProvider(cls.user_env)
        cls.stages = cls.env["crm.stage"].search([], limit=3)

    def ctx(self, key=None, locale="en"):
        call_meta = {meta.ACTION_KEY: key} if key else {}
        return CallContext(self.user_env, self.sales_user.id, self.client.id, locale, call_meta)

    def call(self, name, args, key=None, locale="en"):
        return self.provider.call(name, args, self.ctx(key, locale))

    @staticmethod
    def amount(row):
        return sum(m["amount"] for m in row["expected_revenue"])

    def make_lead(self, **values):
        return self.user_env["crm.lead"].create(dict({"name": "gmcp lead", "type": "opportunity"}, **values))

    def test_create_lead_once_per_key(self):
        args = {"name": "طلب تموين", "type": "opportunity", "expected_revenue": 1500.0,
                "partner_id": self.customer.id, "email": "a@example.com"}
        first = self.call("create_lead", args, key="lead-1")
        again = self.call("create_lead", args, key="lead-1")
        self.assertEqual(first.structured["id"], again.structured["id"])
        leads = self.env["crm.lead"].search([("name", "=", "طلب تموين")])
        self.assertEqual(len(leads), 1)
        self.assertEqual((leads.type, leads.expected_revenue, leads.partner_id), ("opportunity", 1500.0, self.customer))
        self.assertEqual(set(first.structured), {
            "id", "name", "type", "stage", "expected_revenue", "partner_id", "user_id", "tag_ids"})
        self.assertIn("Opportunity", first.text)
        self.assertNotIn("Opportunity", self.call("create_lead", {"name": "x", "type": "lead"}, locale="ar").text)

    def test_create_lead_rejects_invisible_partner(self):
        other = self.env["res.company"].create({"name": "gmcp other co"})
        hidden = self.env["res.partner"].create({"name": "gmcp hidden", "company_id": other.id})
        with self.assertRaises(ToolError):
            self.call("create_lead", {"name": "x", "type": "lead", "partner_id": hidden.id})

    def test_update_lead_stage(self):
        lead = self.make_lead(stage_id=self.stages[0].id)
        result = self.call("update_lead", {
            "lead_id": lead.id, "stage_id": self.stages[1].id, "expected_revenue": 900.0,
            "user_id": self.sales_user.id,
        })
        self.assertEqual(lead.stage_id, self.stages[1])
        self.assertEqual((lead.expected_revenue, lead.user_id), (900.0, self.sales_user))
        self.assertEqual(result.structured["stage"], {"id": self.stages[1].id, "name": self.stages[1].name})
        tag = self.env["crm.tag"].create({"name": "gmcp tag"})
        self.call("update_lead", {"lead_id": lead.id, "tag_ids": [tag.id]})
        self.assertEqual(lead.tag_ids, tag)
        with self.assertRaises(ToolError):
            self.call("update_lead", {"lead_id": lead.id})
        with self.assertRaises(ToolError):
            self.call("update_lead", {"lead_id": lead.id, "stage_id": 0})
        self.assertTrue(curated.REGISTRY["update_lead"]().annotations().idempotent)

    def test_log_note_escapes_html(self):
        lead = self.make_lead()
        result = self.call("log_note", {"model": "crm.lead", "record_id": lead.id, "body": "hi <script>x</script>\nline2"},
                           key="note-1")
        message = self.env["mail.message"].browse(result.structured["id"])
        self.assertIn("&lt;script&gt;", message.body)
        self.assertNotIn("<script>", message.body)
        self.assertEqual(message.subtype_id, self.env.ref("mail.mt_note"))
        self.assertEqual(message.author_id, self.sales_user.partner_id)
        self.assertFalse(message.partner_ids)
        again = self.call("log_note", {"model": "crm.lead", "record_id": lead.id, "body": "hi <script>x</script>\nline2"},
                          key="note-1")
        self.assertEqual(again.structured["id"], message.id)
        for bad in ({"model": "res.users", "record_id": self.sales_user.id, "body": "x"},
                    {"model": "crm.lead", "record_id": lead.id, "body": "   "}):
            with self.assertRaises(ToolError):
                self.call("log_note", bad)

    def test_schedule_activity_deadline(self):
        lead = self.make_lead()
        args = {"model": "crm.lead", "record_id": lead.id, "activity_type": "mail.mail_activity_data_call",
                "summary": "اتصال متابعة", "date_deadline": "2026-12-01"}
        result = self.call("schedule_activity", args, key="act-1")
        self.assertEqual(self.call("schedule_activity", args, key="act-1").structured["id"], result.structured["id"])
        activity = lead.activity_ids
        self.assertEqual(len(activity), 1)
        self.assertEqual((activity.date_deadline, activity.summary, activity.user_id),
                         (date(2026, 12, 1), "اتصال متابعة", self.sales_user))
        self.assertEqual(activity.activity_type_id, self.env.ref("mail.mail_activity_data_call"))
        portal = self._make_user("gmcp_portal_a", "Portal A", ["base.group_portal"])
        for user_id in (portal.id, 0):
            with self.assertRaises(ToolError):
                self.call("schedule_activity", dict(args, user_id=user_id))
        with self.assertRaises(ToolError):
            self.call("schedule_activity", dict(args, date_deadline="soon"))

    def test_pipeline_summary_counts(self):
        stage_a, stage_b = self.stages[0], self.stages[1]
        base = self.call("pipeline_summary", {}).structured
        self.make_lead(stage_id=stage_a.id, expected_revenue=100.0, user_id=self.sales_user.id)
        self.make_lead(stage_id=stage_a.id, expected_revenue=50.0, user_id=self.sales_user.id)
        self.make_lead(stage_id=stage_b.id, expected_revenue=10.0, user_id=False)
        self.make_lead(name="plain lead", type="lead", stage_id=stage_b.id, expected_revenue=999.0, user_id=self.sales_user.id)
        now = self.call("pipeline_summary", {}).structured
        by_stage = {s["stage_id"]: s for s in now["stages"]}
        old = {s["stage_id"]: s for s in base["stages"]}
        self.assertEqual(by_stage[stage_a.id]["count"], old[stage_a.id]["count"] + 2)
        self.assertEqual(self.amount(by_stage[stage_a.id]), self.amount(old[stage_a.id]) + 150.0)
        self.assertEqual(by_stage[stage_b.id]["count"], old[stage_b.id]["count"] + 1)
        mine = self.call("pipeline_summary", {"user_id": self.sales_user.id}).structured
        self.assertEqual(mine["count"], 2)
        self.assertEqual(self.amount(mine), 150.0)
        self.assertEqual([m["currency"] for m in mine["expected_revenue"]], ["SAR"])
        # Stage-less opportunities are counted; won stages are not listed.
        self.make_lead(stage_id=False, expected_revenue=5.0, user_id=self.sales_user.id)
        mine = self.call("pipeline_summary", {"user_id": self.sales_user.id}).structured
        self.assertEqual(mine["count"], 3)
        self.assertIn(None, [s["stage_id"] for s in mine["stages"]])
        won = self.env["crm.stage"].search([("is_won", "=", True)])
        self.assertFalse(won & self.env["crm.stage"].browse(s["stage_id"] for s in mine["stages"] if s["stage_id"]))

    def test_log_note_model_enum_lists_installed_models(self):
        spec = self.provider.list_tools(self.ctx())
        note = next(s for s in spec if s.name == "log_note")
        self.assertEqual([o["const"] for o in note.input_schema["properties"]["model"]["oneOf"]],
                         ["crm.lead", "sale.order", "res.partner", "account.move"])

    def test_required_fields_declared(self):
        fields = self.provider.required_fields()
        self.assertIn("expected_revenue", fields["crm.lead"])
        self.assertIn("date_deadline", fields["mail.activity"])

    def test_create_lead_type_defaults_to_lead_and_revenue_is_bounded(self):
        created = self.call("create_lead", {"name": "default type"})
        self.assertEqual(created.structured["type"], "lead")
        schema = curated.REGISTRY["create_lead"]().input_schema(self.ctx())
        self.assertEqual(schema["required"], ["name"])
        self.assertEqual([o["const"] for o in schema["properties"]["type"]["oneOf"]], ["lead", "opportunity"])
        for bad in ({"type": "deal"}, {"expected_revenue": 1e13}, {"expected_revenue": float("nan")}):
            with self.assertRaises(ToolError):
                self.call("create_lead", dict({"name": "x"}, **bad))

    def test_create_lead_description_is_escaped(self):
        result = self.call("create_lead", {"name": "d", "description": "a <b>x</b>\nb"})
        lead = self.env["crm.lead"].browse(result.structured["id"])
        self.assertIn("&lt;b&gt;", lead.description)
        self.assertIn("<br>", lead.description)

    def test_update_lead_refuses_recomputed_team_the_user_cannot_see(self):
        hidden_name = "gmcp hidden team"
        hide_records(self.env, "crm.team", "[('name', '!=', '%s')]" % hidden_name, "gmcp hide team")
        assignee = self._make_user("gmcp_member", "MCP Member", ["base.group_user", "sales_team.group_sale_salesman"])
        team = self.env["crm.team"].create({"name": hidden_name, "company_id": False, "user_id": assignee.id})
        self.env["crm.team.member"].create({"crm_team_id": team.id, "user_id": assignee.id})
        lead = self.make_lead(user_id=False, team_id=False)
        self.assertFalse(lead.team_id)
        with self.assertRaises(ToolError):
            self.call("update_lead", {"lead_id": lead.id, "user_id": assignee.id})
        lead.invalidate_recordset()
        self.assertFalse(lead.user_id)
        self.assertFalse(lead.team_id)

    def test_update_lead_assignee_must_be_internal(self):
        portal = self._make_user("gmcp_portal_b", "Portal B", ["base.group_portal"])
        lead = self.make_lead()
        with self.assertRaises(ToolError):
            self.call("update_lead", {"lead_id": lead.id, "user_id": portal.id})

    def test_schedule_activity_date_must_be_strict(self):
        lead = self.make_lead()
        with self.assertRaises(ToolError):
            self.call("schedule_activity", {
                "model": "crm.lead", "record_id": lead.id, "activity_type": "mail.mail_activity_data_todo",
                "summary": "x", "date_deadline": "2026-1-5"})

    def test_notes_on_invoices_only_for_customer_invoices(self):
        """log_note / schedule_activity on account.move: customer invoices and credit notes only; a vendor bill is
        "not found"."""
        from odoo import Command

        billing = self._make_user("gmcp_crm_billing", "MCP CRM Billing", [
            "base.group_user", "gental_mcp.group_mcp_connect", "account.group_account_invoice"])
        env = self.env(user=billing)
        provider = curated.CuratedToolProvider(env)

        def call(name, args):
            return provider.call(name, args, CallContext(env, billing.id, self.client.id, "en", {}))

        def move(move_type, partner):
            return self.env["account.move"].create({
                "move_type": move_type, "partner_id": partner.id, "invoice_date": date.today(),
                "invoice_line_ids": [Command.create({"name": "x", "quantity": 1, "price_unit": 10.0})],
            })

        invoice = move("out_invoice", self.customer)
        bill = move("in_invoice", self.env["res.partner"].create({"name": "مورد"}))
        note = {"model": "account.move", "body": "تمت المتابعة"}
        activity = {"model": "account.move", "activity_type": "mail.mail_activity_data_todo", "summary": "Follow up",
                    "date_deadline": date.today().isoformat()}
        self.assertEqual(call("log_note", dict(note, record_id=invoice.id)).structured["record_id"], invoice.id)
        self.assertEqual(call("schedule_activity", dict(activity, record_id=invoice.id)).structured["record_id"],
                         invoice.id)
        for name, args in (("log_note", note), ("schedule_activity", activity)):
            with self.assertRaises(ToolError) as caught:
                call(name, dict(args, record_id=bill.id))
            self.assertEqual(str(caught.exception), curated.message("not_visible", "en"), name)
        self.assertFalse(bill.activity_ids)
        self.assertFalse(bill.message_ids.filtered(lambda m: "تمت المتابعة" in str(m.body)))
