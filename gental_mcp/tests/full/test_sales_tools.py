import time
from unittest.mock import patch

from odoo.addons.gental_mcp.core import meta as gmeta
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.tools import ToolError
from odoo.addons.gental_mcp.core.crypto import hmac_b64url
from odoo.addons.gental_mcp.services import curated, quote

from ..test_contacts_tools import McpRpcMixin
from .common import GentalFullCase
from ..common import PortableHttpCase, use_base_url

IDENTITY_SECRET = "gental-identity-secret-for-tests-0123456789"


def identity_meta(phone="+966500000001", secret=IDENTITY_SECRET):
    ts = int(time.time())
    verified_at = "2026-11-02T10:00:00.0000000Z"
    sig = hmac_b64url(secret, f"whatsapp|{phone}|{verified_at}|{ts}")
    return {gmeta.IDENTITY: {"channel": "whatsapp", "phone": phone, "verifiedAt": verified_at, "ts": ts, "sig": sig}}


class TestSalesTools(McpRpcMixin, GentalFullCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = cls._make_user(
            "gmcp_sales_agent", "MCP Sales Agent",
            ["base.group_user", "sales_team.group_sale_salesman_all_leads", "stock.group_stock_user",
             "gental_mcp.group_mcp_connect"],
        )
        use_base_url(cls.env, cls.base_url())
        # 15% VAT on coffee so taxes show in the totals.
        cls.vat = cls.env["account.tax"].create({
            "name": "VAT 15%", "amount": 15.0, "type_tax_use": "sale", "company_id": cls.company.id,
        })
        cls.coffee.product_tmpl_id.write({"default_code": "COF-1", "taxes_id": [(6, 0, cls.vat.ids)]})
        cls.cardamom.product_tmpl_id.write({"default_code": "CAR-1", "taxes_id": [(5, 0, 0)]})
        warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.env["stock.quant"]._update_available_quantity(cls.coffee, warehouse.lot_stock_id, 7.0)
        cls.lines = [{"product_id": cls.coffee.id, "qty": 2}, {"product_id": cls.cardamom.id, "qty": 1}]

    def orders(self):
        return self.env["sale.order"].search([("partner_id", "=", self.customer.id)])

    def preview(self, lines=None, locale="en", meta=None, **extra):
        args = {"partner_id": self.customer.id, "lines": lines or self.lines, **extra}
        return self.ok("preview_quotation", args, meta=meta, locale=locale)["structuredContent"]

    def create(self, token=None, lines=None, key=None, **extra):
        meta = {}
        if token is not None:
            meta[gmeta.PREVIEW_QUOTE] = token
        if key is not None:
            meta[gmeta.ACTION_KEY] = key
        args = {"partner_id": self.customer.id, "lines": lines or self.lines, **extra}
        return self.call("create_quotation", args, meta=meta)

    def make_order(self, partner=None, user=None):
        return self.env["sale.order"].create({
            "partner_id": (partner or self.customer).id,
            "user_id": (user or self.agent_user).id,
            "order_line": [(0, 0, {"product_id": self.coffee.id, "product_uom_qty": 1})],
        })

    # -- products and preview --------------------------------------------------------------------

    def test_search_products_price_and_stock(self):
        products = self.ok("search_products", {"query": "قهوة"})["structuredContent"]["products"]
        self.assertEqual(len(products), 1)
        coffee = products[0]
        self.assertEqual((coffee["id"], coffee["name"], coffee["code"]), (self.coffee.id, "قهوة عربية", "COF-1"))
        self.assertEqual((coffee["price"], coffee["currency"]), (25.0, "SAR"))
        self.assertEqual(coffee["qty_available"], 7.0)
        by_code = self.ok("search_products", {"query": "car-1"})["structuredContent"]["products"]
        self.assertEqual([p["id"] for p in by_code], [self.cardamom.id])
        self.assertTrue(self.call("search_products", {"query": "قهوة", "limit": 21})["isError"])

    def _wholesale_trader(self, phone="0533330000"):
        """A customer on a wholesale price list (coffee at 20), with a phone a verified identity can match."""
        wholesale = self.env["product.pricelist"].create({
            "name": "Wholesale", "currency_id": self.company.currency_id.id,
            "item_ids": [(0, 0, {"applied_on": "0_product_variant", "product_id": self.coffee.id,
                                 "compute_price": "fixed", "fixed_price": 20.0})],
        })
        trader = self.env["res.partner"].create({
            "name": "تاجر جملة", "property_product_pricelist": wholesale.id, "phone": phone,
            "country_id": self.env.ref("base.sa").id,
        })
        return wholesale, trader

    def test_search_products_pricelist_rule(self):
        """R20 + R30 for search_products: a customer's own prices only for that verified customer or a sales
        manager; anyone else gets the standard price list, and a pricelist_id is refused."""
        self.env["gental.mcp.config"].get().set_identity_secret(IDENTITY_SECRET)
        wholesale, trader = self._wholesale_trader()
        verified = identity_meta(phone="+966533330000")

        def search(args, meta=None):
            return self.ok("search_products", dict(args, query="قهوة"), meta=meta)["structuredContent"]

        for args, meta in (({"pricelist_id": wholesale.id}, None),
                           ({"pricelist_id": wholesale.id, "partner_id": self.customer.id}, verified),
                           ({"pricelist_id": wholesale.id, "partner_id": trader.id}, None)):
            refused = self.call("search_products", dict(args, query="قهوة"), meta=meta)
            self.assertTrue(refused["isError"], args)
            self.assertIn("own price list", refused["content"][0]["text"])
        unverified = search({"partner_id": trader.id})
        self.assertEqual((unverified["products"][0]["price"], unverified["price_list"]), (25.0, "standard"))
        other = search({"partner_id": trader.id}, meta=identity_meta())  # verified as someone else
        self.assertEqual((other["products"][0]["price"], other["price_list"]), (25.0, "standard"))
        own = search({"partner_id": trader.id}, meta=verified)
        self.assertEqual((own["products"][0]["price"], own["price_list"]), (20.0, "customer"))
        named = search({"partner_id": trader.id, "pricelist_id": wholesale.id}, meta=verified)
        self.assertEqual(named["products"][0]["price"], 20.0)
        self.assertEqual(search({})["price_list"], "standard")
        self.agent_user = self._make_user(
            "gmcp_search_manager", "MCP Search Manager",
            ["base.group_user", "sales_team.group_sale_manager", "gental_mcp.group_mcp_connect"],
        )
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("manager", self.agent_user)
        self.assertEqual(search({"pricelist_id": wholesale.id})["products"][0]["price"], 20.0)
        self.assertEqual(search({"partner_id": trader.id})["products"][0]["price"], 20.0)

    def test_quote_prices_bound_to_verified_customer(self):
        """R30 for quotations: an unverified partner_id is quoted on the standard price list; the verified customer
        on their own; and a quote signed under one rule can't be created under the other."""
        self.env["gental.mcp.config"].get().set_identity_secret(IDENTITY_SECRET)
        _wholesale, trader = self._wholesale_trader()
        verified = identity_meta(phone="+966533330000")
        args = {"partner_id": trader.id, "lines": [{"product_id": self.coffee.id, "qty": 2}]}
        public = self.ok("preview_quotation", args)
        self.assertEqual(public["structuredContent"]["price_list"], "standard")
        self.assertEqual(public["structuredContent"]["total"], 57.5)  # 2 × 25 + 15% VAT
        self.assertIn("standard price list", public["content"][0]["text"])
        own = self.ok("preview_quotation", args, meta=verified)
        self.assertEqual((own["structuredContent"]["price_list"], own["structuredContent"]["total"]),
                         ("customer", 46.0))  # 2 × 20 + 15% VAT

        def create(token, meta=None):
            return self.call("create_quotation", args, meta=dict(meta or {}, **{gmeta.PREVIEW_QUOTE: token}))

        traders = lambda: self.env["sale.order"].search([("partner_id", "=", trader.id)])  # noqa: E731
        for token, meta in ((own["structuredContent"]["quote"], None),
                            (public["structuredContent"]["quote"], verified)):
            crossed = create(token, meta)
            self.assertTrue(crossed["isError"], crossed)
            self.assertIn("another price list", crossed["content"][0]["text"])
        self.assertFalse(traders())
        made = create(own["structuredContent"]["quote"], verified)
        self.assertFalse(made["isError"], made)
        self.assertEqual(made["structuredContent"]["total"], 46.0)
        unverified = create(public["structuredContent"]["quote"])
        self.assertFalse(unverified["isError"], unverified)
        self.assertEqual(unverified["structuredContent"]["total"], 57.5)
        order = traders().filtered(lambda o: o.amount_total == 57.5)
        self.assertNotEqual(order.pricelist_id, trader.property_product_pricelist, "the standard list is kept")

    def test_preview_returns_signed_quote_and_labels_ar(self):
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["gental_mcp"], ["ar_001"], overwrite=True)
        before = self.env["sale.order"].search_count([])
        data = self.preview(locale="ar")
        self.assertEqual(self.env["sale.order"].search_count([]), before, "a preview writes nothing")
        self.assertEqual(set(data), {"lines", "total", "quote", "price_list"})
        # 2 × 25 + 15% VAT = 57.5, plus 15 cardamom without tax.
        self.assertEqual(data["total"], 72.5)
        labels = [line["label"] for line in data["lines"]]
        self.assertEqual(labels[0], "قهوة عربية × 2")
        self.assertIn("الإجمالي", labels)
        self.assertIn("72.50 SAR", [line["value"] for line in data["lines"]])
        payload = quote.verify(self.env["gental.mcp.config"].quote_secret(), data["quote"], quote.now())
        self.assertEqual(payload["partner_id"], self.customer.id)
        self.assertEqual(payload["total"], 72.5)
        self.assertEqual([(l["product_id"], l["qty"], l["price_unit"]) for l in payload["lines"]],
                         [(self.coffee.id, 2, 25.0), (self.cardamom.id, 1, 15.0)])
        self.assertEqual(payload["lines"][0]["taxes"], self.vat.ids)
        self.assertLessEqual(payload["exp"] - quote.now(), 900)
        english = self.preview()
        self.assertIn("Total", [line["label"] for line in english["lines"]])

    # -- create ----------------------------------------------------------------------------------

    def test_create_with_valid_quote(self):
        token = self.preview()["quote"]
        result = self.create(token, note="Deliver after Asr")
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]
        order = self.env["sale.order"].browse(data["id"])
        self.assertEqual((data["name"], data["total"]), (order.name, 72.5))
        self.assertEqual((order.state, order.partner_id, order.amount_total), ("draft", self.customer, 72.5))
        self.assertEqual(order.order_line.mapped("product_uom_qty"), [2.0, 1.0])
        self.assertIn("Deliver after Asr", str(order.note))
        self.assertEqual(order.user_id, self.agent_user)

    def test_create_refuses_changed_price(self):
        token = self.preview()["quote"]
        self.coffee.list_price = 30.0
        result = self.create(token)
        self.assertTrue(result["isError"])
        self.assertIn("preview", result["content"][0]["text"].lower())
        self.assertFalse(self.orders(), "no order")

    def test_create_refuses_expired_quote(self):
        token = self.preview()["quote"]
        later = quote.now() + 901
        with patch.object(quote, "now", lambda: later):
            result = self.create(token)
        self.assertTrue(result["isError"])
        self.assertIn("expired", result["content"][0]["text"])
        self.assertFalse(self.orders())

    def test_create_refuses_quote_for_other_lines(self):
        token = self.preview()["quote"]
        for lines in ([{"product_id": self.coffee.id, "qty": 3}, {"product_id": self.cardamom.id, "qty": 1}],
                      [{"product_id": self.coffee.id, "qty": 2}]):
            self.assertTrue(self.create(token, lines=lines)["isError"], lines)
        other = self.env["res.partner"].create({"name": "Other customer"})
        result = self.call("create_quotation", {"partner_id": other.id, "lines": self.lines},
                           meta={gmeta.PREVIEW_QUOTE: token})
        self.assertTrue(result["isError"])
        tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        self.assertTrue(self.create(tampered)["isError"])
        self.assertFalse(self.orders())
        self.assertFalse(self.env["sale.order"].search([("partner_id", "=", other.id)]))

    def test_create_same_action_key_one_order(self):
        token = self.preview()["quote"]
        first = self.create(token, key="order-key-1")
        later = quote.now() + 3600
        with patch.object(quote, "now", lambda: later):  # a retry after the quote expired still finds it
            second = self.create(token, key="order-key-1")
        self.assertFalse(first["isError"], first)
        self.assertFalse(second["isError"], second)
        self.assertEqual(first["structuredContent"]["id"], second["structuredContent"]["id"])
        self.assertEqual(len(self.orders()), 1)
        # The key stays in the module's action-key table, never on the customer-visible order.
        self.assertFalse(self.orders().client_order_ref)
        self.assertEqual(self.env["gental.mcp.action.key"].sudo().search([("key", "=", "order-key-1")]).res_id,
                         self.orders().id)

    def test_create_without_quote_from_non_gental_client_ok(self):
        result = self.create()
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["total"], 72.5)
        self.assertEqual(len(self.orders()), 1)
        schema = self.tools()["create_quotation"]["inputSchema"]
        self.assertFalse({"price", "price_unit", "total", "amount_total", "expected_total"} & set(schema["properties"]))
        self.assertEqual(set(schema["properties"]["lines"]["items"]["properties"]), {"product_id", "qty"})

    # -- confirm, cancel, status -----------------------------------------------------------------

    def test_confirm_idempotent(self):
        order = self.make_order()
        first = self.ok("confirm_order", {"order_id": order.id})
        self.assertEqual((order.state, first["structuredContent"]["state"]), ("sale", "sale"))
        second = self.ok("confirm_order", {"order_id": order.id})
        self.assertEqual(second["structuredContent"]["state"], "sale")
        order._action_cancel()
        self.assertTrue(self.call("confirm_order", {"order_id": order.id})["isError"])

    def test_cancel_is_destructive_annotation(self):
        tools = self.tools()
        self.assertTrue(tools["cancel_order"]["annotations"]["destructiveHint"])
        self.assertFalse(tools["confirm_order"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["order_status"]["annotations"]["readOnlyHint"])
        order = self.make_order()
        result = self.ok("cancel_order", {"order_id": order.id, "reason": "Customer changed <b>their</b> mind\nCall back"})
        self.assertEqual((order.state, result["structuredContent"]["state"]), ("cancel", "cancel"))
        note = next(str(m.body) for m in order.message_ids if "Customer changed" in str(m.body))
        self.assertIn("Customer changed &lt;b&gt;their&lt;/b&gt; mind<br>Call back", note.replace("<br/>", "<br>"))
        self.assertFalse(self.ok("cancel_order", {"order_id": order.id}).get("isError"))
        # When Odoo asks for a confirmation wizard, nothing changes and staff are flagged.
        wizard = self.make_order()
        sale_order_cls = type(self.env["sale.order"])
        with patch.object(sale_order_cls, "action_cancel", lambda self: {"type": "ir.actions.act_window"}):
            flagged = self.ok("cancel_order", {"order_id": wizard.id})
        self.assertEqual(flagged["_meta"][gmeta.FLAG], "staff_review")
        self.assertEqual(wizard.state, "draft")

    def test_order_status(self):
        order = self.make_order()
        order.action_confirm()
        data = self.ok("order_status", {"order_ref": order.name.lower()})["structuredContent"]
        self.assertEqual((data["name"], data["state"], data["state_label"]), (order.name, "sale", "Sales Order"))
        self.assertIn("invoice_status", data)
        self.assertEqual(data["delivery_status"], order.delivery_status)
        self.assertTrue(self.call("order_status", {"order_ref": "S-NOPE"})["isError"])

    # -- my orders -------------------------------------------------------------------------------

    def test_my_orders_only_matched_partner(self):
        self.env["gental.mcp.config"].get().set_identity_secret(IDENTITY_SECRET)
        contact = self.env["res.partner"].create({"name": "Ahmed's assistant", "parent_id": self.customer.id})
        mine = self.make_order() | self.make_order(partner=contact)
        self.make_order(partner=self.env["res.partner"].create({"name": "Stranger"}))
        result = self.ok("my_orders", {"limit": 10}, meta=identity_meta())
        names = {o["name"] for o in result["structuredContent"]["orders"]}
        self.assertEqual(names, set(mine.mapped("name")))
        unknown = self.ok("my_orders", {}, meta=identity_meta(phone="+966511111111"))
        self.assertEqual(unknown["structuredContent"]["orders"], [])
        self.assertIn("match", unknown["content"][0]["text"])
        schema = self.tools()["my_orders"]["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"limit"})

    def test_my_orders_refused_without_identity(self):
        self.assertNotIn("my_orders", self.tools(), "unlisted without an identity secret")
        self.env["gental.mcp.config"].get().set_identity_secret(IDENTITY_SECRET)
        self.make_order()
        self.assertIn("my_orders", self.tools())
        for meta in (None, identity_meta(secret="x" * 43)):
            result = self.call("my_orders", {}, meta=meta)
            self.assertTrue(result["isError"])
            self.assertEqual(result["content"][0]["text"], "This needs a verified customer.")

    def test_record_rules_salesman_sees_own_orders_only(self):
        self.agent_user = self._make_user(
            "gmcp_own_salesman", "MCP Own Salesman",
            ["base.group_user", "sales_team.group_sale_salesman", "gental_mcp.group_mcp_connect"],
        )
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("own", self.agent_user)
        colleague = self._make_user("gmcp_colleague", "Colleague", ["base.group_user", "sales_team.group_sale_salesman"])
        own, theirs = self.make_order(user=self.agent_user), self.make_order(user=colleague)
        self.assertEqual(self.ok("order_status", {"order_ref": own.name})["structuredContent"]["name"], own.name)
        self.assertTrue(self.call("order_status", {"order_ref": theirs.name})["isError"])
        for tool in ("confirm_order", "cancel_order"):
            self.assertTrue(self.call(tool, {"order_id": theirs.id})["isError"], tool)
        self.assertEqual(theirs.state, "draft")

    # -- fix round 1 -----------------------------------------------------------------------------

    def test_create_refuses_equal_total_line_shift(self):
        token = self.preview()["quote"]
        # 2 × 26 + 15% VAT = 59.8, plus 12.7 = 72.5: the same total, other line prices.
        self.coffee.list_price = 26.0
        self.cardamom.list_price = 12.7
        self.assertEqual(self.preview()["total"], 72.5)
        result = self.create(token)
        self.assertTrue(result["isError"])
        self.assertIn("Prices changed", result["content"][0]["text"])
        self.assertFalse(self.orders())

    def test_pricelist_only_own_unless_sales_manager(self):
        wholesale = self.env["product.pricelist"].create({
            "name": "Wholesale", "currency_id": self.company.currency_id.id,
            "item_ids": [(0, 0, {"applied_on": "0_product_variant", "product_id": self.coffee.id,
                                 "compute_price": "fixed", "fixed_price": 20.0})],
        })
        own = self.customer.with_company(self.company).property_product_pricelist
        self.assertTrue(own)
        for tool in ("preview_quotation", "create_quotation"):
            refused = self.call(tool, {"partner_id": self.customer.id, "lines": self.lines,
                                       "pricelist_id": wholesale.id})
            self.assertTrue(refused["isError"], tool)
            self.assertIn("own price list", refused["content"][0]["text"])
        self.assertFalse(self.orders())
        self.assertTrue(self.call("preview_quotation", {"partner_id": self.customer.id, "lines": self.lines,
                                                        "pricelist_id": own.id})["isError"], "unverified customer")
        self.env["gental.mcp.config"].get().set_identity_secret(IDENTITY_SECRET)
        self.assertEqual(self.preview(pricelist_id=own.id, meta=identity_meta())["total"], 72.5)
        # A sales manager may choose another price list.
        self.agent_user = self._make_user(
            "gmcp_sales_manager", "MCP Sales Manager",
            ["base.group_user", "sales_team.group_sale_manager", "gental_mcp.group_mcp_connect"],
        )
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("manager", self.agent_user)
        token = self.preview(pricelist_id=wholesale.id)["quote"]
        created = self.create(token, pricelist_id=wholesale.id)
        self.assertFalse(created["isError"], created)
        # 2 × 20 + 15% VAT = 46, plus 15 cardamom.
        self.assertEqual(created["structuredContent"]["total"], 61.0)

    def test_quote_bound_to_client(self):
        token = self.preview()["quote"]
        _other_client, self.key = self.env["gental.mcp.client"].create_api_key("other", self.agent_user)
        result = self.create(token)
        self.assertTrue(result["isError"])
        self.assertFalse(self.orders())

    def test_cancel_confirmed_order(self):
        order = self.make_order()
        self.ok("confirm_order", {"order_id": order.id})
        self.assertEqual(order.state, "sale")
        result = self.ok("cancel_order", {"order_id": order.id})
        self.assertEqual((order.state, result["structuredContent"]["state"]), ("cancel", "cancel"))
        self.assertNotIn("_meta", result)
        self.assertTrue(all(p.state == "cancel" for p in order.picking_ids))

    def test_lines_schema_bounds(self):
        args = {"partner_id": self.customer.id}
        self.assertTrue(self.call("preview_quotation", dict(args, lines=[]))["isError"])
        too_many = [{"product_id": self.coffee.id, "qty": 1_000_001}]
        self.assertTrue(self.call("preview_quotation", dict(args, lines=too_many))["isError"])

    def test_order_document(self):
        self.agent_user = self._make_user(
            "gmcp_doc_salesman", "MCP Doc Salesman",
            ["base.group_user", "sales_team.group_sale_salesman", "gental_mcp.group_mcp_connect"],
        )
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("doc", self.agent_user)
        colleague = self._make_user("gmcp_doc_colleague", "Colleague", ["base.group_user", "sales_team.group_sale_salesman"])
        own, theirs = self.make_order(user=self.agent_user), self.make_order(user=colleague)
        self.assertTrue(self.tools()["get_order_document"]["annotations"]["readOnlyHint"])
        # In-process with real PDF rendering: inside an HTTP request Odoo's test mode renders HTML.
        env = self.env(user=self.agent_user, context=dict(self.env.context, force_report_rendering=True))
        provider = curated.CuratedToolProvider(env)

        def call(order):
            ctx = CallContext(env=env, user_id=self.agent_user.id, client_id=self.client.id, locale="en")
            ctx.tool_spec = next(s for s in provider.list_tools(ctx) if s.name == "get_order_document")
            return provider.call("get_order_document", {"order_id": order.id}, ctx)

        data = call(own).structured
        self.assertIn("/mcp/files/", data["url"])
        self.assertTrue(data["name"].endswith(".pdf"))
        doc = self.env["gental.mcp.document"].sudo().search([("res_model", "=", "sale.order"), ("res_id", "=", own.id)])
        self.assertEqual(len(doc), 1)
        with self.assertRaises(ToolError):
            call(theirs)

    def test_my_order_document_own_orders_only(self):
        """🔒 my_order_document: only the verified customer's own orders; another customer's id is "not found",
        exactly like an unknown one."""
        self.env["gental.mcp.config"].get().set_identity_secret(IDENTITY_SECRET)
        mine = self.make_order()
        stranger = self.make_order(partner=self.env["res.partner"].create({"name": "Stranger"}))
        env = self.env(user=self.agent_user, context=dict(self.env.context, force_report_rendering=True))
        provider = curated.CuratedToolProvider(env)

        def call(order_id, meta):
            ctx = CallContext(env=env, user_id=self.agent_user.id, client_id=self.client.id, locale="en",
                              meta=dict(meta or {}))
            ctx.tool_spec = curated.REGISTRY["my_order_document"]().spec(ctx)
            return provider.call("my_order_document", {"order_id": order_id}, ctx)

        data = call(mine.id, identity_meta()).structured
        self.assertIn("/mcp/files/", data["url"])
        with self.assertRaises(ToolError) as theirs:
            call(stranger.id, identity_meta())
        with self.assertRaises(ToolError) as unknown:
            call(2147483647, identity_meta())
        self.assertEqual(str(theirs.exception), str(unknown.exception))
        with self.assertRaises(ToolError):
            call(mine.id, identity_meta(phone="+966511111111"))  # no matching customer
        with self.assertRaises(ToolError):
            call(mine.id, None)  # no identity
        schema = self.tools()["my_order_document"]["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"order_id"})
