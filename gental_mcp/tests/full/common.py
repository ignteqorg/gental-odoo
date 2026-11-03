from unittest import SkipTest

from odoo.tests import tagged

from ..common import GentalMcpCase

BUSINESS_MODULES = ("sale_management", "account", "crm", "stock", "point_of_sale")


@tagged("post_install", "-at_install", "gental_full")
class GentalFullCase(GentalMcpCase):
    """Base for tests that need the business apps (``PROFILE=full scripts/test.sh``).

    Shared data: the company is in Saudi Arabia and uses SAR, two products («قهوة عربية» at 25 and
    Cardamom) and a customer «أحمد العتيبي» whose mobile is 0500000001.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        installed = cls.env["ir.module.module"].sudo().search(
            [("name", "in", BUSINESS_MODULES), ("state", "=", "installed")]
        ).mapped("name")
        missing = sorted(set(BUSINESS_MODULES) - set(installed))
        if missing:
            raise SkipTest(f"Needs the business apps (PROFILE=full); missing: {', '.join(missing)}")

        saudi = cls.env.ref("base.sa")
        sar = cls.env.ref("base.SAR")
        sar.active = True
        cls.company = cls.env.company
        cls.company.write({"country_id": saudi.id, "currency_id": sar.id})
        cls.env["product.pricelist"].search(
            [("company_id", "in", [cls.company.id, False])]
        ).currency_id = sar

        Template = cls.env["product.template"]
        storable = {"is_storable": True} if "is_storable" in Template._fields else {"type": "product"}
        cls.coffee = Template.create(dict(storable, name="قهوة عربية", list_price=25.0)).product_variant_id
        cls.cardamom = Template.create(dict(storable, name="Cardamom", list_price=15.0)).product_variant_id

        Partner = cls.env["res.partner"]
        phone_field = "mobile" if "mobile" in Partner._fields else "phone"
        cls.customer = Partner.create({"name": "أحمد العتيبي", "country_id": saudi.id, phone_field: "0500000001"})
