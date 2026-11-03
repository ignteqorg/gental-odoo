from odoo.tests import tagged

from odoo.addons.gental_mcp.core import meta as gmeta

from .common import GentalMcpCase, PortableHttpCase, use_base_url


class McpRpcMixin:
    """Calls ``/mcp`` with an API key of ``cls.agent_user`` (set ``self.key`` in ``setUp``)."""

    def setUp(self):
        super().setUp()
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("test", self.agent_user)

    def rpc(self, method, params=None):
        msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        resp = self.url_open("/mcp", json=msg, headers={
            "Authorization": f"Bearer {self.key}", "Accept": "application/json, text/event-stream",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def tools(self):
        return {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}

    def call(self, name, arguments=None, meta=None, locale="en"):
        call_meta = {gmeta.LOCALE: locale, **(meta or {})}
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}, "_meta": call_meta})
        self.assertIn("result", result, result)
        return result["result"]

    def ok(self, name, arguments=None, meta=None, locale="en"):
        result = self.call(name, arguments, meta, locale)
        self.assertFalse(result["isError"], result)
        return result


@tagged("post_install", "-at_install", "gental_mcp")
class TestContactsTools(McpRpcMixin, GentalMcpCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = cls._make_user(
            "gmcp_contacts_agent", "MCP Contacts Agent",
            ["base.group_user", "base.group_partner_manager", "gental_mcp.group_mcp_connect"],
        )
        use_base_url(cls.env, cls.base_url())
        cls.env.company.country_id = cls.env.ref("base.sa")
        cls.Partner = cls.env["res.partner"]
        cls.phone_field = "mobile" if "mobile" in cls.Partner._fields else "phone"
        cls.ahmed = cls.Partner.create({
            "name": "أحمد العتيبي", "country_id": cls.env.ref("base.sa").id,
            cls.phone_field: "0500000001", "email": "ahmed@example.com", "ref": "C-001",
        })
        cls.other = cls.Partner.create({"name": "Sara Khalid", "email": "sara@example.com"})

    def test_contact_company_is_the_company_it_belongs_to(self):
        """``company`` names the company a contact belongs to, and is empty for a company or a contact on its
        own. 20.0 relates ``commercial_company_name`` to the commercial partner's name, so a contact on its
        own would otherwise show its own name there."""
        parent = self.Partner.create({"name": "GMX Parent Co", "is_company": True})
        self.Partner.create({"name": "GMX Child Person", "parent_id": parent.id})
        self.Partner.create({"name": "GMX Solo Person"})
        found = self.ok("find_contact", {"query": "GMX"})["structuredContent"]["contacts"]
        companies = {c["name"]: c["company"] for c in found}
        self.assertEqual(companies["GMX Child Person"], "GMX Parent Co")
        self.assertEqual(companies["GMX Solo Person"], "")
        self.assertEqual(companies["GMX Parent Co"], "")

    def test_company_name_offered_only_where_odoo_has_it(self):
        """``res.partner.company_name`` is gone in 20.0: the schema leaves it out there, and a call that still
        sends it is refused before anything is written. Where the field exists, it is offered and saved."""
        properties = self.tools()["upsert_contact"]["inputSchema"]["properties"]
        args = {"name": "GMX Company Field", "company_name": "GMX Co"}
        if "company_name" in self.Partner._fields:
            self.assertIn("company_name", properties)
            created = self.ok("upsert_contact", args)
            self.assertEqual(self.Partner.browse(created["structuredContent"]["id"]).company_name, "GMX Co")
            return
        self.assertNotIn("company_name", properties)
        result = self.call("upsert_contact", args)
        self.assertTrue(result["isError"], result)
        self.assertIn("company_name", result["content"][0]["text"])
        self.assertFalse(self.Partner.search([("name", "=", "GMX Company Field")]))

    def test_tools_listed_with_annotations(self):
        tools = self.tools()
        self.assertTrue(tools["find_contact"]["annotations"]["readOnlyHint"])
        upsert = tools["upsert_contact"]["annotations"]
        self.assertFalse(upsert["readOnlyHint"])
        self.assertTrue(upsert["idempotentHint"])
        self.assertFalse(upsert["destructiveHint"])

    def test_find_contact_arabic_query(self):
        result = self.ok("find_contact", {"query": "أحمد"}, locale="ar")
        contacts = result["structuredContent"]["contacts"]
        self.assertEqual([c["id"] for c in contacts], [self.ahmed.id])
        self.assertEqual(contacts[0]["name"], "أحمد العتيبي")
        self.assertEqual(contacts[0]["phone"], "0500000001")
        self.assertEqual(contacts[0]["email"], "ahmed@example.com")
        self.assertEqual(contacts[0]["ref"], "C-001")
        self.assertIn("أحمد العتيبي", result["content"][0]["text"])
        # By phone (any format, Arabic-Indic digits too), email and reference.
        for query in ("+966 50 000 0001", "٠٥٠٠٠٠٠٠٠١", "AHMED@example", "C-001"):
            found = self.ok("find_contact", {"query": query})["structuredContent"]["contacts"]
            self.assertEqual([c["id"] for c in found], [self.ahmed.id], query)
        nothing = self.ok("find_contact", {"query": "zz-nobody"})
        self.assertEqual(nothing["structuredContent"]["contacts"], [])
        self.assertTrue(self.call("find_contact", {"query": "a"})["isError"])  # too short

    def test_upsert_matches_by_phone_no_duplicate(self):
        before = self.Partner.search_count([])
        result = self.ok("upsert_contact", {
            "name": "أحمد العتيبي", "phone": "+966500000001", "city": "الرياض", "email": "new@example.com",
        })
        self.assertEqual(result["structuredContent"], {"id": self.ahmed.id, "status": "matched"})
        self.assertEqual(self.Partner.search_count([]), before, "no duplicate")
        self.assertEqual(self.ahmed.city, "الرياض", "an empty field is filled")
        self.assertEqual(self.ahmed.email, "ahmed@example.com", "a filled field is never overwritten")
        self.assertNotIn("_meta", result)
        # Matched by email (case-insensitive) when no phone matches; a phone is never added to it.
        by_email = self.ok("upsert_contact", {"name": "Sara Khalid", "email": "SARA@example.com", "city": "جدة",
                                              "phone": "0544445555"})
        self.assertEqual(by_email["structuredContent"], {"id": self.other.id, "status": "matched"})
        self.assertEqual((self.other.city, self.other.phone), ("جدة", False))
        # No match: created, phone stored in E.164.
        created = self.ok("upsert_contact", {"name": "New Customer", "phone": "0555555555"})
        partner = self.Partner.browse(created["structuredContent"]["id"])
        self.assertEqual(created["structuredContent"]["status"], "created")
        self.assertEqual((partner.name, partner.phone), ("New Customer", "+966555555555"))
        self.assertTrue(self.call("upsert_contact", {"name": "Bad", "phone": "12"})["isError"])

    def test_upsert_name_differs_reveals_nothing(self):
        """Same phone, another name: nothing written (not even empty fields), and neither the saved name nor the
        contact id reaches the model; only the staff reason (``_meta``) names it."""
        self.other.write({"phone": "+966511112222"})
        has_company_name = "company_name" in self.Partner._fields  # gone in 20.0
        renamed = self.ok("upsert_contact", dict(
            {"name": "Someone Else", "phone": "0511112222", "street": "Hacker St"},
            **({"company_name": "Evil Co"} if has_company_name else {}),
        ))
        self.assertEqual(renamed["structuredContent"], {"status": "needs_review"})
        self.assertNotIn("Sara", renamed["content"][0]["text"])
        self.assertNotIn(str(self.other.id), renamed["content"][0]["text"])
        self.assertEqual(renamed["_meta"][gmeta.FLAG], "staff_review")
        self.assertIn("Sara Khalid", renamed["_meta"][gmeta.FLAG_REASON])
        self.assertEqual((self.other.name, self.other.street), ("Sara Khalid", False))
        if has_company_name:
            self.assertFalse(self.other.company_name)
        bare = self.Partner.create({"name": "Mona", "phone": "+966533334444"})
        self.ok("upsert_contact", {"name": "Not Mona", "phone": "0533334444", "email": "attacker@example.com"})
        self.assertFalse(bare.email, "an email is never added to a contact whose name differs")

    def test_upsert_email_only_for_the_verified_customer(self):
        """Same name and phone, empty email: unverified chat data never adds the email (invoices would go there);
        a verified identity whose phone is this contact's number does."""
        from .test_curated_framework import IDENTITY_SECRET, identity_meta

        bare = self.Partner.create({"name": "Mona", "phone": "+966533334444", "country_id": self.env.ref("base.sa").id})
        args = {"name": "Mona", "phone": "0533334444", "email": "mona@example.com"}
        self.assertEqual(self.ok("upsert_contact", args)["structuredContent"], {"id": bare.id, "status": "matched"})
        self.assertFalse(bare.email)
        self.env["gental.mcp.config"].get().set_identity_secret(IDENTITY_SECRET)
        self.ok("upsert_contact", args, meta=identity_meta(phone="+966500000009"))  # someone else verified
        self.assertFalse(bare.email)
        self.ok("upsert_contact", args, meta=identity_meta(phone="+966533334444"))
        self.assertEqual(bare.email, "mona@example.com")

    def test_upsert_two_matches_flags_staff_review(self):
        twin = self.Partner.create({"name": "أحمد (نسخة)", self.phone_field: "+966 500 000 001"})
        before = self.Partner.search_count([])
        result = self.ok("upsert_contact", {"name": "أحمد", "phone": "0500000001", "city": "الدمام"})
        self.assertEqual(result["structuredContent"], {"status": "needs_review"})
        self.assertNotIn("أحمد العتيبي", result["content"][0]["text"])
        self.assertEqual(result["_meta"][gmeta.FLAG], "staff_review")
        self.assertIn(f"#{twin.id}", result["_meta"][gmeta.FLAG_REASON])
        self.assertEqual(self.Partner.search_count([]), before)
        self.assertFalse(self.ahmed.city or twin.city, "no write")

    def test_find_contact_phone_is_exact(self):
        """A number whose digits merely contain the query's (another country's number) is not a match."""
        egypt = self.Partner.create({"name": "رقم مشابه", "phone": "+20 1500000001"})
        found = self.ok("find_contact", {"query": "0500000001"})["structuredContent"]["contacts"]
        self.assertEqual([c["id"] for c in found], [self.ahmed.id])
        self.assertNotIn(egypt.id, [c["id"] for c in found])

    def test_upsert_action_key_once(self):
        meta = {gmeta.ACTION_KEY: "contact-key-1"}
        first = self.ok("upsert_contact", {"name": "Walk-in Customer"}, meta=meta)
        second = self.ok("upsert_contact", {"name": "Walk-in Customer"}, meta=meta)
        self.assertEqual(first["structuredContent"]["id"], second["structuredContent"]["id"])
        self.assertEqual(self.Partner.search_count([("name", "=", "Walk-in Customer")]), 1)
        third = self.ok("upsert_contact", {"name": "Walk-in Customer"}, meta={gmeta.ACTION_KEY: "contact-key-2"})
        self.assertNotEqual(third["structuredContent"]["id"], first["structuredContent"]["id"])

    def test_required_fields(self):
        from odoo.addons.gental_mcp.services import curated

        fields = curated.CuratedToolProvider(self.env).required_fields()
        self.assertIn("phone", fields["res.partner"])
        self.assertIn("amount_total", fields["sale.order"])
