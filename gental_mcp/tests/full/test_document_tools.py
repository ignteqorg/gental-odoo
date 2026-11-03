import re
import time
from unittest.mock import patch

from odoo import Command, fields

from odoo.addons.gental_mcp.core import meta
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.crypto import hmac_b64url
from odoo.addons.gental_mcp.core.tools import ToolDenied, ToolError
from odoo.addons.gental_mcp.services import curated

from ..common import PortableHttpCase, use_base_url
from .common import GentalFullCase


class TestDocumentTools(GentalFullCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env["gental.mcp.config"].get()
        use_base_url(cls.env, cls.base_url())
        cls.env["gental.mcp.tool.setting"]._sync_from_registry()
        cls.staff = cls._make_user("gmcp_staff", "MCP Staff", [
            "base.group_user", "gental_mcp.group_mcp_connect",
            "account.group_account_invoice", "sales_team.group_sale_manager", "stock.group_stock_user",
        ])
        cls.invoice = cls.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": cls.customer.id,
            "invoice_line_ids": [Command.create({"product_id": cls.coffee.id, "quantity": 2, "price_unit": 25.0})],
        })
        cls.invoice.action_post()

    def setUp(self):
        super().setUp()
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("test", self.staff)

    # -- helpers ------------------------------------------------------------------

    def rpc_raw(self, name, arguments, key=None):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        resp = self.url_open("/mcp", json=msg, headers={
            "Authorization": f"Bearer {key or self.key}", "Accept": "application/json, text/event-stream",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp

    def call_direct(self, name, arguments, user=None, locale="en"):
        """Call a tool in the test process with real PDF rendering (wkhtmltopdf), as Odoo's own report
        tests do; inside an HTTP request Odoo's test mode would render HTML instead."""
        user = user or self.staff
        env = self.env(user=user, context=dict(self.env.context, force_report_rendering=True))
        ctx = CallContext(env=env, user_id=user.id, client_id=self.client.id, locale=locale)
        provider = curated.CuratedToolProvider(env)
        ctx.tool_spec = next(s for s in provider.list_tools(ctx) if s.name == name)
        return provider.call(name, arguments, ctx)

    def download(self, url):
        resp = self.url_open(url, allow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        return resp.content

    # -- tests --------------------------------------------------------------------

    def test_document_tools_listed_as_read(self):
        resp = self.url_open("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={
            "Authorization": f"Bearer {self.key}", "Accept": "application/json, text/event-stream",
        })
        tools = {t["name"]: t for t in resp.json()["result"]["tools"]}
        for name, arg in (("get_order_document", "order_id"), ("get_invoice_document", "invoice_id"),
                          ("get_delivery_document", "picking_id")):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)
            self.assertEqual(tools[name]["inputSchema"]["required"], [arg])

    def test_report_xmlids_exist(self):
        for xmlid, model in (("sale.action_report_saleorder", "sale.order"),
                             ("account.account_invoices", "account.move"),
                             ("account.account_invoices_without_payment", "account.move"),
                             ("stock.action_report_delivery", "stock.picking")):
            self.assertEqual(self.env.ref(xmlid).model, model)

    def test_invoice_pdf_generated_and_listed(self):
        result = self.call_direct("get_invoice_document", {"invoice_id": self.invoice.id})
        self.assertFalse(result.is_error)
        url = result.structured["url"]
        self.assertIn(url, result.text)
        self.assertIn(result.structured["expires_at"][:10], result.text)
        self.assertEqual(result.resource_links, [{
            "type": "resource_link", "uri": url, "name": result.structured["name"], "mimeType": "application/pdf",
        }])
        self.assertTrue(result.structured["name"].endswith(".pdf"))

        doc = self.env["gental.mcp.document"].search([("res_model", "=", "account.move"),
                                                      ("res_id", "=", self.invoice.id)])
        self.assertEqual(len(doc), 1)
        self.assertEqual((doc.tool, doc.client_id, doc.user_id, doc.state),
                         ("get_invoice_document", self.client, self.staff, "active"))
        self.assertEqual(doc.report_xmlid, "account.account_invoices")
        listed = self.env(user=self.manager_user)["gental.mcp.document"].search([])
        self.assertIn(doc, listed)

        pdf = self.download(url)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(doc.download_count, 1)

    def test_salesperson_gets_invoice_without_payment_report(self):
        # The full invoice report is limited to accounting groups; a salesperson who can read the invoice
        # gets the same invoice without the payments block.
        seller = self._make_user("gmcp_seller", "MCP Seller", [
            "base.group_user", "gental_mcp.group_mcp_connect", "sales_team.group_sale_salesman_all_leads",
        ])
        self.assertFalse(seller.has_group("account.group_account_invoice"))
        self.assertFalse(seller.has_group("account.group_account_readonly"))
        self.invoice.invoice_user_id = seller
        self.assertTrue(self.env(user=seller)["account.move"].search([("id", "=", self.invoice.id)]))

        result = self.call_direct("get_invoice_document", {"invoice_id": self.invoice.id}, user=seller)
        self.assertFalse(result.is_error, result.text)
        doc = self.env["gental.mcp.document"].search([("user_id", "=", seller.id)])
        self.assertEqual(doc.report_xmlid, "account.account_invoices_without_payment")
        self.assertTrue(self.download(result.structured["url"]).startswith(b"%PDF"))

        # Accountants still get the full report.
        self.call_direct("get_invoice_document", {"invoice_id": self.invoice.id})
        doc = self.env["gental.mcp.document"].search([("user_id", "=", self.staff.id)])
        self.assertEqual(doc.report_xmlid, "account.account_invoices")

    def test_document_respects_record_rules(self):
        # A staff member of another company: the invoice's multi-company rule hides it from them.
        other_company = self.env["res.company"].create({"name": "Other Co", "country_id": self.company.country_id.id})
        outsider = self._make_user("gmcp_outsider", "MCP Outsider", [
            "base.group_user", "gental_mcp.group_mcp_connect", "account.group_account_invoice",
        ])
        outsider.write({"company_ids": [Command.set([other_company.id])], "company_id": other_company.id})
        self.assertFalse(self.env(user=outsider)["account.move"].search([("id", "=", self.invoice.id)]))
        _client, key = self.env["gental.mcp.client"].create_api_key("outsider", outsider)

        before = self.env["gental.mcp.document"].search_count([])
        result = self.rpc_raw("get_invoice_document", {"invoice_id": self.invoice.id}, key=key).json()["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn("/mcp/files/", result["content"][0]["text"])
        self.assertEqual(self.env["gental.mcp.document"].search_count([]), before)

        # A user with no access to invoices at all gets an error result too, never a PDF.
        _client, key = self.env["gental.mcp.client"].create_api_key("agent", self.agent_user)
        result = self.rpc_raw("get_invoice_document", {"invoice_id": self.invoice.id}, key=key).json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(self.env["gental.mcp.document"].search_count([]), before)

    def test_arabic_partner_pdf_nonempty_and_text_literal(self):
        order = self.env["sale.order"].create({
            "partner_id": self.customer.id,
            "order_line": [Command.create({"product_id": self.coffee.id, "product_uom_qty": 1})],
        })
        order.action_confirm()
        picking = order.picking_ids[:1]
        self.assertTrue(picking)

        result = self.call_direct("get_delivery_document", {"picking_id": picking.id}, locale="ar")
        self.assertFalse(result.is_error)
        self.assertIn("أحمد العتيبي", result.structured["name"])  # the slip's own file name has the customer
        self.assertIn("أحمد العتيبي", result.text)
        self.assertIn("الرابط", result.text)
        pdf = self.download(result.structured["url"])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)
        # Odoo's report fonts (Lato) have no Arabic glyphs; the Arabic name and product are drawn with the
        # image's Arabic-capable fallback font, DejaVu Sans, which is embedded (no tofu boxes).
        fonts = {name.split(b"+")[-1] for name in re.findall(rb"/BaseFont\s*/([A-Za-z0-9+_-]+)", pdf)}
        self.assertIn(b"DejaVuSans", fonts)

        # Over the wire the tool text keeps the Arabic literally (ensure_ascii=False).
        Report = type(self.env["ir.actions.report"])
        with patch.object(Report, "_render_qweb_pdf", lambda *a, **k: (b"%PDF-1.4 stub", "pdf")):
            raw = self.rpc_raw("get_delivery_document", {"picking_id": picking.id}).text
        self.assertIn("أحمد العتيبي", raw)
        self.assertNotIn("\\u0623", raw)

    def test_my_invoice_document_own_invoices_only(self):
        """🔒 my_invoice_document: the verified customer's own posted invoices only; another customer's invoice,
        a draft or the customer's own vendor bill is "not found", exactly like an unknown id. In-process (real PDF), as call_direct."""
        secret = "gental-identity-secret-for-tests-0123456789"
        self.config.set_identity_secret(secret)

        def identity(phone="+966500000001"):
            ts, at = int(time.time()), "2026-11-02T10:00:00.0000000Z"
            sig = hmac_b64url(secret, f"whatsapp|{phone}|{at}|{ts}")
            return {meta.IDENTITY: {"channel": "whatsapp", "phone": phone, "verifiedAt": at, "ts": ts, "sig": sig}}

        def make(partner, post=True, move_type="out_invoice"):
            move = self.env["account.move"].create({
                "move_type": move_type, "partner_id": partner.id, "invoice_date": fields.Date.today(),
                "invoice_line_ids": [Command.create({"product_id": self.coffee.id, "quantity": 1, "price_unit": 25.0})],
            })
            if post:
                move.action_post()
            return move

        foreign = make(self.env["res.partner"].create({"name": "عميل ثاني"}))
        draft = make(self.customer, post=False)
        vendor_bill = make(self.customer, move_type="in_invoice")  # the matched customer's own posted bill
        env = self.env(user=self.staff, context=dict(self.env.context, force_report_rendering=True))
        provider = curated.CuratedToolProvider(env)

        def call(invoice_id, call_meta=None):
            ctx = CallContext(env, self.staff.id, self.client.id, "en", dict(call_meta or {}))
            ctx.tool_spec = curated.REGISTRY["my_invoice_document"]().spec(ctx)
            return provider.call("my_invoice_document", {"invoice_id": invoice_id}, ctx)

        result = call(self.invoice.id, identity())
        self.assertIn("/mcp/files/", result.structured["url"])
        doc = self.env["gental.mcp.document"].search([("res_model", "=", "account.move"), ("res_id", "=", self.invoice.id)])
        self.assertEqual(doc.tool, "my_invoice_document")
        not_found = curated.message("not_visible", "en")
        for invoice_id in (foreign.id, draft.id, vendor_bill.id, 2147483647):
            with self.assertRaises(ToolError) as caught:
                call(invoice_id, identity())
            self.assertEqual(str(caught.exception), not_found, invoice_id)
        with self.assertRaises(ToolError):
            call(self.invoice.id, identity("+966511111111"))
        with self.assertRaises(ToolDenied):
            call(self.invoice.id)
        tool = curated.REGISTRY["my_invoice_document"]()
        self.assertTrue(tool.requires_identity and tool.annotations().read_only)
        self.assertEqual(set(tool.input_schema(None)["properties"]), {"invoice_id"})
