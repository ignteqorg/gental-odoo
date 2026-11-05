"""Tool metadata in the request's language: ``_meta["ai.gental/locale"]``, else ``Accept-Language``, else the
module's default language. One language per response; tool names and argument keys never change."""
from odoo.tests import tagged

from odoo.addons.gental_mcp.core import meta as gmeta

from .common import GentalMcpCase, PortableHttpCase, use_base_url


@tagged("post_install", "-at_install", "gental_mcp")
class TestToolI18n(GentalMcpCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env["gental.mcp.config"].get()
        use_base_url(cls.env, cls.base_url())

    def setUp(self):
        super().setUp()
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("test", self.agent_user)

    def upsert_contact(self, locale=None, accept_language=None):
        """``upsert_contact`` from ``tools/list`` over HTTP, with an optional ``_meta`` locale and header."""
        params = {"_meta": {gmeta.LOCALE: locale}} if locale else {}
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json, text/event-stream"}
        if accept_language:
            headers["Accept-Language"] = accept_language
        resp = self.url_open("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
                             headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return next(t for t in resp.json()["result"]["tools"] if t["name"] == "upsert_contact")

    def assertArabic(self, tool):
        self.assertEqual(tool["title"], "إضافة جهة اتصال أو تحديثها")
        self.assertEqual(tool["inputSchema"]["properties"]["name"]["title"], "الاسم")
        self.assertEqual(tool["inputSchema"]["properties"]["phone"]["title"], "الجوال")

    def assertEnglish(self, tool):
        self.assertEqual(tool["title"], "Create or update a contact")
        self.assertEqual(tool["inputSchema"]["properties"]["name"]["title"], "Name")
        self.assertEqual(tool["inputSchema"]["properties"]["phone"]["title"], "Phone")

    def test_meta_locale_wins(self):
        self.assertArabic(self.upsert_contact("ar", accept_language="en-US"))
        self.assertEnglish(self.upsert_contact("en", accept_language="ar-SA"))

    def test_accept_language_then_default(self):
        self.config.default_lang = "en"
        self.assertArabic(self.upsert_contact(accept_language="ar-SA,ar;q=0.9,en;q=0.8"))
        self.assertEnglish(self.upsert_contact(accept_language="fr-FR, en;q=0.5"))
        self.assertEnglish(self.upsert_contact(accept_language="fr-FR"))  # neither: the default
        self.config.default_lang = "ar"
        self.assertArabic(self.upsert_contact())
        self.assertArabic(self.upsert_contact("xx"))

    def test_same_tools_and_keys_titled_in_each_language(self):
        """Base profile: Contacts and the generic tools (every property titled in both languages)."""
        en, _ar = self.assert_tools_localized()
        self.assertIn("upsert_contact", en)
        self.assertIn("odoo_call_method", en)
