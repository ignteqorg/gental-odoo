import time

from odoo import fields

from odoo.addons.gental_mcp.core import meta as gmeta
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.crypto import hmac_b64url
from odoo.addons.gental_mcp.core.tools import ToolDenied, ToolError
from odoo.addons.gental_mcp.services import curated

from .common import GentalFullCase

SECRET = "gental-identity-secret-for-tests-0123456789"


class TestInventoryPosTools(GentalFullCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls._make_user("gmcp_stock", "Stock User", [
            "base.group_user", "gental_mcp.group_mcp_connect", "stock.group_stock_user",
            "point_of_sale.group_pos_user", "sales_team.group_sale_salesman_all_leads",
        ])
        cls.env["gental.mcp.config"].get().area_pos_enabled = True
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse2 = cls.env["stock.warehouse"].create({"name": "Second", "code": "SEC2"})
        Quant = cls.env["stock.quant"]
        Quant.with_context(inventory_mode=True).create({
            "product_id": cls.coffee.id, "location_id": cls.warehouse.lot_stock_id.id, "inventory_quantity": 10.0,
        }).action_apply_inventory()
        Quant.with_context(inventory_mode=True).create({
            "product_id": cls.coffee.id, "location_id": cls.warehouse2.lot_stock_id.id, "inventory_quantity": 4.0,
        }).action_apply_inventory()
        cls.provider = curated.CuratedToolProvider(cls.env(user=cls.user))

    def ctx(self, locale="en", meta=None):
        return CallContext(self.env(user=self.user), self.user.id, 0, locale, dict(meta or {}))

    def call(self, name, args=None, **kw):
        ctx = self.ctx(**kw)
        return curated.CuratedToolProvider(ctx.env).call(name, args or {}, ctx)

    @staticmethod
    def identity(phone, secret=SECRET):
        ts, verified = int(time.time()), "2026-11-02T10:00:00.0000000Z"
        sig = hmac_b64url(secret, f"whatsapp|{phone}|{verified}|{ts}")
        return {gmeta.IDENTITY: {"channel": "whatsapp", "phone": phone, "verifiedAt": verified, "ts": ts, "sig": sig}}

    def outgoing(self, partner, origin, name_suffix=""):
        picking_type = self.warehouse.out_type_id
        move_name = {"name": self.coffee.display_name} if "name" in self.env["stock.move"]._fields else {}  # 17/18
        picking = self.env["stock.picking"].create({
            "picking_type_id": picking_type.id,
            "partner_id": partner.id,
            "origin": origin,
            "location_id": picking_type.default_location_src_id.id,
            "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            "move_ids": [(0, 0, dict(move_name, **{
                "product_id": self.coffee.id, "product_uom_qty": 2.0, "location_id": picking_type.default_location_src_id.id,
                "location_dest_id": self.env.ref("stock.stock_location_customers").id,
            }))],
        })
        picking.action_confirm()
        return picking

    # stock

    def test_stock_per_warehouse_and_forecast(self):
        self.outgoing(self.customer, "S-FORECAST")
        result = self.call("stock_availability", {"product_id": self.coffee.id})
        rows = {w["warehouse_id"]: w for w in result.structured["products"][0]["warehouses"]}
        self.assertEqual(rows[self.warehouse.id]["qty_available"], 10.0)
        self.assertEqual(rows[self.warehouse.id]["virtual_available"], 8.0)
        self.assertEqual(rows[self.warehouse2.id]["qty_available"], 4.0)
        self.assertEqual(rows[self.warehouse2.id]["virtual_available"], 4.0)
        only = self.call("stock_availability", {"query": "قهوة", "warehouse_id": self.warehouse2.id})
        self.assertEqual([w["warehouse_id"] for w in only.structured["products"][0]["warehouses"]], [self.warehouse2.id])
        self.assertEqual(set(rows[self.warehouse.id]), {
            "warehouse_id", "warehouse", "qty_available", "virtual_available", "free_qty"})
        with self.assertRaises(ToolError):
            self.call("stock_availability", {})
        with self.assertRaises(ToolError):
            self.call("stock_availability", {"product_id": self.coffee.id, "warehouse_id": 99999999})

    def test_delivery_status_for_order(self):
        order = self.env["sale.order"].create({"partner_id": self.customer.id})
        picking = self.outgoing(self.customer, order.name)
        if "sale_id" in picking._fields:
            picking.sale_id = order
        result = self.call("delivery_status", {"order_ref": order.name}, locale="ar")
        row = result.structured["deliveries"][0]
        self.assertEqual((row["name"], row["state"]), (picking.name, picking.state))
        self.assertIn("scheduled_date", row)
        self.assertIsNone(row["done_date"])
        empty = self.env["sale.order"].create({"partner_id": self.customer.id}).name
        self.assertEqual(self.call("delivery_status", {"order_ref": empty}).structured["deliveries"], [])
        self.assertEqual(self.call("delivery_status", {"order_ref": empty}, locale="ar").text, "ما لقيت أي توصيلات.")
        self.assertEqual(self.call("delivery_status", {"order_ref": empty}).text, "No deliveries found.")

    def test_delivery_status_needs_a_visible_order(self):
        other = self.env["res.company"].create({"name": "gmcp other company"})
        hidden_order = self.env["sale.order"].create({"partner_id": self.customer.id, "company_id": other.id})
        self.outgoing(self.customer, hidden_order.name)
        # An order the acting user can't see is "not found", the same answer as an unknown reference.
        with self.assertRaises(ToolError) as hidden:
            self.call("delivery_status", {"order_ref": hidden_order.name})
        with self.assertRaises(ToolError) as unknown:
            self.call("delivery_status", {"order_ref": "S99999"})
        self.assertEqual(str(hidden.exception), str(unknown.exception))

    def test_my_deliveries_identity(self):
        mine = self.outgoing(self.customer, "S-MINE")
        other = self.env["res.partner"].create({"name": "Other", "country_id": self.env.ref("base.sa").id})
        self.outgoing(other, "S-OTHER")
        config = self.env["gental.mcp.config"].get()
        config.set_identity_secret(SECRET)
        with self.assertRaises(ToolDenied):
            self.call("my_deliveries")
        with self.assertRaises(ToolDenied):
            self.call("my_deliveries", meta=self.identity("+966500000001", secret="x" * 43))
        result = self.call("my_deliveries", {"limit": 10}, meta=self.identity("+966500000001"))
        self.assertEqual([d["name"] for d in result.structured["deliveries"]], [mine.name])
        draft = self.outgoing(self.customer, "S-DRAFT")
        draft.action_cancel()
        again = self.call("my_deliveries", {"limit": 10}, meta=self.identity("+966500000001"))
        self.assertEqual([d["name"] for d in again.structured["deliveries"]], [mine.name])
        # The verified phone belongs to a contact under a company: the company's deliveries are returned.
        company = self.env["res.partner"].create({"name": "شركة الاختبار", "is_company": True})
        contact = self.env["res.partner"].create({
            "name": "Contact", "parent_id": company.id, "country_id": self.env.ref("base.sa").id,
            "phone": "0500000077",
        })
        theirs = self.outgoing(company, "S-COMPANY")
        by_contact = self.call("my_deliveries", meta=self.identity("+966500000077"))
        self.assertEqual([d["name"] for d in by_contact.structured["deliveries"]], [theirs.name])
        none = self.call("my_deliveries", meta=self.identity("+966500099999"))
        self.assertEqual(none.structured["deliveries"], [])
        schema = next(s for s in self.provider.list_tools(self.ctx()) if s.name == "my_deliveries").input_schema
        self.assertEqual(set(schema["properties"]), {"limit"})

    # point of sale

    def _make_pos_order(self, reference, qty=3.0, cardamom_qty=1.0):
        config = self.pos_config
        session = self.pos_session
        lines = [(0, 0, {
            "product_id": p.id, "full_product_name": p.name, "qty": q, "price_unit": p.list_price,
            "price_subtotal": p.list_price * q, "price_subtotal_incl": p.list_price * q,
        }) for p, q in ((self.coffee, qty), (self.cardamom, cardamom_qty))]
        total = self.coffee.list_price * qty + self.cardamom.list_price * cardamom_qty
        order = self.env["pos.order"].create({
            "session_id": session.id, "config_id": config.id, "company_id": self.company.id,
            "pos_reference": reference, "name": reference, "lines": lines,
            "partner_id": self.customer.id,  # newer 20.0 builds refuse a pay_later payment without a customer
            "amount_total": total, "amount_tax": 0.0, "amount_paid": total, "amount_return": 0.0,
            "state": "paid", "date_order": fields.Datetime.now(),
        })
        self.env["pos.payment"].create({
            "pos_order_id": order.id, "amount": total, "payment_method_id": self.pos_method.id, "session_id": session.id,
        })
        return order

    def setUp(self):
        super().setUp()
        Method = self.env["pos.payment.method"]
        method_vals = {"name": "Cash test"}
        if not Method._fields["type"].compute:  # 20.0: chosen; before, computed (no journal: pay_later)
            method_vals["type"] = "pay_later"
        self.pos_method = Method.create(method_vals)
        self.pos_config = self.env["pos.config"].create(
            {"name": "الفرع الرئيسي", "payment_method_ids": [(6, 0, self.pos_method.ids)]})
        self.pos_session = self.env["pos.session"].create({"config_id": self.pos_config.id, "user_id": self.env.uid})

    def test_pos_summary_today(self):
        self._make_pos_order("0001-001-0001")
        self._make_pos_order("0001-001-0002", qty=1.0, cardamom_qty=5.0)
        result = self.call("pos_sales_summary", {"config_id": self.pos_config.id})
        shop = result.structured["shops"][0]
        self.assertEqual((shop["config_id"], shop["order_count"], shop["amount_total"]), (self.pos_config.id, 2, 25 * 4 + 15 * 6 + 0.0))
        top = {p["name"]: p["qty"] for p in shop["top_products"]}
        self.assertEqual(top, {"قهوة عربية": 4.0, "Cardamom": 6.0})
        self.assertEqual(shop["top_products"][0]["name"], "Cardamom")
        self.assertNotIn("partner", str(result.structured))
        with self.assertRaises(ToolError):
            self.call("pos_sales_summary", {"date_from": "2026-01-01", "date_to": "2026-12-31"})
        with self.assertRaises(ToolError):
            self.call("pos_sales_summary", {"date_from": "2026-02-02", "date_to": "2026-02-01"})
        empty = self.call("pos_sales_summary", {"date_from": "2020-01-01", "date_to": "2020-01-02"})
        self.assertEqual(empty.structured["shops"], [])

    def test_find_pos_order_by_receipt(self):
        order = self._make_pos_order("Order 0001-001-0009")
        found = self.call("find_pos_order", {"receipt": "Order 0001-001-0009"})
        data = found.structured["orders"][0]
        self.assertEqual((data["state"], data["amount_total"]), ("paid", order.amount_total))
        self.assertEqual(data["payment_methods"], ["Cash test"])
        self.assertEqual({line["product"] for line in data["lines"]}, {"قهوة عربية", "Cardamom"})
        for wildcard in ("Order 0001-001-00", "%", "Order%"):
            self.assertEqual(self.call("find_pos_order", {"receipt": wildcard}).structured["orders"], [])

    def test_pos_area_off_by_default(self):
        config = self.env["gental.mcp.config"].get()
        config.area_pos_enabled = False
        names = [s.name for s in curated.CuratedToolProvider(self.env(user=self.user)).list_tools(self.ctx())]
        self.assertNotIn("pos_sales_summary", names)
        self.assertNotIn("find_pos_order", names)
        self.assertIn("stock_availability", names)
        with self.assertRaises(ToolDenied):
            self.call("find_pos_order", {"receipt": "x"})
        config.area_pos_enabled = True
        names = [s.name for s in self.provider.list_tools(self.ctx())]
        self.assertIn("pos_sales_summary", names)
        self.assertIn("find_pos_order", names)
        self.assertNotIn("my_deliveries", names)  # no identity secret
