from unittest.mock import patch

from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.tools import ToolDenied
from odoo.addons.gental_mcp.services import areas, curated

from .common import GentalFullCase

# One tool per area: (tool, arguments, a declared model and field the tool reads).
FIELD_RIGHTS_CASES = (
    ("contacts", "find_contact", {"query": "أحمد"}, "res.partner", "email"),
    ("sales", "search_products", {"query": "قهوة"}, "product.product", "default_code"),
    ("invoicing", "list_invoices", {}, "account.move", "invoice_date"),
    ("crm", "pipeline_summary", {}, "crm.lead", "expected_revenue"),
    ("inventory", "stock_availability", {"query": "قهوة"}, "product.product", "free_qty"),
    ("pos", "pos_sales_summary", {}, "pos.order", "amount_total"),
)


class TestFullProfile(GentalFullCase):
    def test_every_area_installed_and_shared_data(self):
        self.assertEqual(areas.installed_areas(self.env), set(areas.AREAS))
        self.assertEqual(self.company.currency_id.name, "SAR")
        self.assertEqual(self.company.country_id.code, "SA")
        self.assertEqual((self.coffee.name, self.coffee.list_price), ("قهوة عربية", 25.0))
        self.assertEqual(self.cardamom.name, "Cardamom")
        self.assertEqual(self.customer.name, "أحمد العتيبي")

    def test_every_tool_schema_portable_with_all_apps(self):
        self.assert_tool_schemas_portable()

    def test_every_tool_titled_in_both_languages_with_all_apps(self):
        """Every curated tool (all apps installed) and every one of its properties has a title in Arabic and in
        English; the two listings differ only in those texts."""
        en, _ar = self.assert_tools_localized()
        self.assertLessEqual({n for n in curated.REGISTRY if not n.startswith("gmcp_probe")}, set(en))

    def test_declared_field_outside_rights_refuses_in_every_area(self):
        """R27 for each area: a declared field the acting user may not read (``groups`` set to system
        administrators) refuses the tool before it runs, on every Odoo version."""
        self.assertEqual({case[0] for case in FIELD_RIGHTS_CASES}, set(areas.AREAS))
        user = self._make_user("gmcp_rights_all", "MCP All Areas", [
            "base.group_user", "base.group_partner_manager", "gental_mcp.group_mcp_connect",
            "account.group_account_invoice", "sales_team.group_sale_manager", "stock.group_stock_user",
            "point_of_sale.group_pos_user",
        ])
        config = self.env["gental.mcp.config"].get()
        config.write({f"area_{area}_enabled": True for area in areas.AREAS})  # POS is off by default
        client = self.env["gental.mcp.client"].create({"name": "rights", "kind": "api_key", "user_id": user.id})
        env = self.env(user=user)
        provider = curated.CuratedToolProvider(env)
        for area, name, args, model, field in FIELD_RIGHTS_CASES:
            with self.subTest(area=area, tool=name):
                tool = curated.REGISTRY[name]()
                self.assertEqual(tool.area, area)
                self.assertIn(field, curated._flat(tool.declared_fields()[model]))
                ctx = CallContext(env, user.id, client.id, "en", {})
                self.assertFalse(provider.call(name, args, ctx).is_error)
                # A fresh environment, as each request has: 20.0 memoizes granted field reads per environment.
                fresh = self.env(user=user, context=dict(self.env.context, gmcp_r27_area=area))
                with patch.object(self.env[model]._fields[field], "groups", "base.group_system"):
                    with self.assertRaises(ToolDenied) as caught:
                        curated.CuratedToolProvider(fresh).call(
                            name, args, CallContext(fresh, user.id, client.id, "en", {}))
                self.assertEqual(str(caught.exception), tool.field_access_refusal(ctx))
