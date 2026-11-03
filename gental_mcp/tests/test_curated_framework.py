import time

from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from odoo.addons.gental_mcp.core import meta as gmeta

from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.crypto import hmac_b64url
from odoo.addons.gental_mcp.core.tools import ToolError, ToolResult
from odoo.addons.gental_mcp.services import areas, curated

from .common import GentalMcpCase, PortableHttpCase, use_base_url

OBJECT = {"type": "object", "properties": {}}


class _ProbeRead(curated.CuratedTool):
    name = "gmcp_probe_read"
    area = "contacts"
    kind = "read"
    title_en = "Probe read"
    title_ar = "فحص قراءة"
    description = "Reads nothing."

    def input_schema(self, ctx):
        return OBJECT

    def run(self, args, ctx):
        return ToolResult("probed", structured={"uid": ctx.env.uid})


class _ProbeSales(_ProbeRead):
    name = "gmcp_probe_sales"
    area = "sales"


class _ProbeMine(_ProbeRead):
    name = "gmcp_probe_mine"
    requires_identity = True


class _ProbeStrict(_ProbeRead):
    name = "gmcp_probe_strict"

    def input_schema(self, ctx):
        return {"type": "object", "required": ["q"], "properties": {"q": {"type": "string"}}}


class _ProbeNoRights(_ProbeRead):
    name = "gmcp_probe_no_rights"

    def run(self, args, ctx):
        ctx.env["res.users"].create({"name": "x", "login": "gmcp_probe_x"})  # agents may not create users


class _ProbeUserError(_ProbeRead):
    name = "gmcp_probe_user_error"

    def run(self, args, ctx):
        raise UserError("Odoo detail about partner Secret Person")


class _ProbeHiddenField(_ProbeRead):
    """Declares a field limited to ``base.group_no_one`` (agents don't have it)."""
    name = "gmcp_probe_hidden_field"

    def required_fields(self):
        return {"gental.mcp.token": ["kind", "token_hash"], "no.such.model": ["x"]}

    def run(self, args, ctx):
        raise AssertionError("run() must not be reached")


PROBES = (_ProbeRead, _ProbeSales, _ProbeMine, _ProbeStrict, _ProbeNoRights, _ProbeHiddenField, _ProbeUserError)
IDENTITY_SECRET = "gental-identity-secret-for-tests-0123456789"


def identity_meta(secret=IDENTITY_SECRET, ts=None, phone="+966500000001"):
    ts = int(time.time()) if ts is None else ts
    verified_at = "2026-11-02T10:00:00.0000000Z"
    sig = hmac_b64url(secret, f"whatsapp|{phone}|{verified_at}|{ts}")
    return {gmeta.IDENTITY: {"channel": "whatsapp", "phone": phone, "verifiedAt": verified_at, "ts": ts, "sig": sig}}


@tagged("post_install", "-at_install", "gental_mcp")
class TestCuratedFramework(GentalMcpCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for probe in PROBES:
            curated.register(probe)
        cls.config = cls.env["gental.mcp.config"].get()
        use_base_url(cls.env, cls.base_url())

    @classmethod
    def tearDownClass(cls):
        for probe in PROBES:
            curated.REGISTRY.pop(probe.name, None)
        super().tearDownClass()

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

    def call_tool(self, name, arguments=None, meta=None):
        params = {"name": name, "arguments": arguments or {}}
        if meta is not None:
            params["_meta"] = meta
        return self.rpc("tools/call", params)

    def call_log(self, tool):
        return self.env["gental.mcp.log"].sudo().search(
            [("client_id", "=", self.client.id), ("kind", "=", "call"), ("tool", "=", tool)]
        )

    def test_access_error_logged_as_denied(self):
        """Wall 3 (the user's own rights) refusing a curated tool is a denial, with a generic localized text:
        Odoo's own message stays in the server log."""
        with self.assertLogs("odoo.addons.gental_mcp.services.curated", "INFO") as logs:
            result = self.call_tool("gmcp_probe_no_rights")["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], curated.message("refused", "ar"))
        self.assertEqual(self.call_log("gmcp_probe_no_rights")[:1].status, "denied")
        self.assertIn("gmcp_probe_no_rights", logs.output[0])

    def test_odoo_error_text_never_reaches_the_model(self):
        with self.assertLogs("odoo.addons.gental_mcp.services.curated", "INFO") as logs:
            result = self.call_tool("gmcp_probe_user_error")["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], curated.message("failed", "ar"))
        self.assertNotIn("Secret Person", self.call_log("gmcp_probe_user_error")[:1].result_text)
        self.assertEqual(self.call_log("gmcp_probe_user_error")[:1].status, "error")
        self.assertIn("Secret Person", logs.output[0])

    def test_audience_on_every_tool(self):
        """Every tool says whether a public agent may use it (``_meta["ai.gental/audience"]``); staff tools say so
        in their description and title, and every 🔒 tool is public-safe."""
        self.assertTrue(curated.PUBLIC_SAFE_TOOLS <= set(curated.REGISTRY), curated.PUBLIC_SAFE_TOOLS - set(curated.REGISTRY))
        ctx = CallContext(self.env(user=self.agent_user), self.agent_user.id, self.client.id, "ar")
        for name in sorted(curated.REGISTRY):
            if name.startswith("gmcp_probe"):
                continue
            tool = curated.REGISTRY[name]()
            with self.subTest(tool=name):
                spec = tool.spec(ctx).to_mcp()
                audience = spec["_meta"][gmeta.AUDIENCE]
                self.assertIn(audience, ("public", "staff"))
                if tool.requires_identity:
                    self.assertEqual(audience, "public")
                staff = audience == "staff"
                self.assertEqual(spec["description"].startswith("Staff tool: "), staff)
                self.assertEqual(spec["title"].startswith("للموظفين: "), staff)
        listed = self.tools()
        self.assertEqual(listed["find_contact"]["_meta"][gmeta.AUDIENCE], "staff")
        self.assertEqual(listed["upsert_contact"]["_meta"][gmeta.AUDIENCE], "public")
        self.assertTrue(listed["find_contact"]["description"].startswith("Staff tool: "))

    def test_id_arguments_capped(self):
        """Every record id argument (``*_id``, items of ``*_ids``) of the installed tools has
        ``maximum: 2147483647`` (Postgres ``integer``)."""
        ctx = CallContext(self.env(user=self.agent_user), self.agent_user.id, self.client.id, "en")
        installed = areas.installed_areas(self.env)
        seen = []

        def walk(tool_name, schema):
            for prop, sub in (schema.get("properties") or {}).items():
                if sub.get("type") == "array":
                    items = sub.get("items") or {}
                    if prop.endswith("_ids"):
                        check(tool_name, prop, items)
                    walk(tool_name, items)
                elif prop.endswith("_id"):
                    check(tool_name, prop, sub)

        def check(tool_name, prop, sub):
            seen.append((tool_name, prop))
            with self.subTest(tool=tool_name, argument=prop):
                self.assertEqual((sub["type"], sub["minimum"], sub["maximum"]), ("integer", 1, curated.ID_MAX))

        for name in sorted(curated.REGISTRY):
            tool = curated.REGISTRY[name]()
            if tool.area in installed and not name.startswith("gmcp_probe"):
                walk(name, tool.input_schema(ctx))
        self.assertTrue(seen)

    def test_declared_field_outside_rights_is_denied_before_run(self):
        """Field groups of ``required_fields()`` are checked on every version (17.0/18.0 don't enforce them on
        attribute reads of computed fields), before ``run``."""
        result = self.assert_denied("gmcp_probe_hidden_field")
        self.assertEqual(result["content"][0]["text"], "ما عندك الصلاحيات اللي تحتاجها هذي الأداة في أودو.")
        self.assertFalse(self.call_tool("gmcp_probe_read")["result"]["isError"])

    def assert_denied(self, tool, meta=None):
        result = self.call_tool(tool, meta=meta)["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(self.call_log(tool)[:1].status, "denied")
        return result

    def test_area_hidden_when_app_missing(self):
        if "sales" in areas.installed_areas(self.env):
            self.skipTest("The Sales app is installed in this database.")
        names = self.tools()
        self.assertNotIn("search_products", names)
        self.assertNotIn("gmcp_probe_sales", names)
        # A tool of a missing app is unknown, not merely switched off.
        self.assertIn("error", self.call_tool("gmcp_probe_sales"))

    def test_contacts_area_always_available(self):
        self.assertIn("contacts", areas.installed_areas(self.env))
        tool = self.tools()["gmcp_probe_read"]
        self.assertEqual(tool["title"], "للموظفين: فحص قراءة")  # default language is Arabic; a staff tool
        self.assertEqual(tool["description"], "Staff tool: Reads nothing.")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        result = self.call_tool("gmcp_probe_read")["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {"uid": self.agent_user.id})
        self.assertEqual(self.call_log("gmcp_probe_read").tool_kind, "read")

    def test_area_toggle_hides_tools(self):
        self.config.area_contacts_enabled = False
        self.assertNotIn("gmcp_probe_read", self.tools())
        self.assert_denied("gmcp_probe_read")

    def test_tool_setting_off_hides_and_denies(self):
        Setting = self.env["gental.mcp.tool.setting"]
        Setting._sync_from_registry()
        Setting.sudo().search([("name", "in", ["gmcp_probe_read", "gmcp_probe_strict"])]).enabled = False
        names = self.tools()
        self.assertNotIn("gmcp_probe_read", names)
        self.assertNotIn("gmcp_probe_strict", names)
        self.assert_denied("gmcp_probe_read")
        # Missing required arguments: still denied (not "Invalid arguments"), and the schema isn't revealed.
        result = self.assert_denied("gmcp_probe_strict")
        self.assertNotIn("Invalid arguments", result["content"][0]["text"])

    def test_identity_tools_unlisted_without_secret(self):
        names = self.tools()
        self.assertIn("gmcp_probe_read", names)
        self.assertNotIn("gmcp_probe_mine", names)
        self.assert_denied("gmcp_probe_mine")

    def test_identity_tool_needs_valid_signature_when_secret_set(self):
        self.config.set_identity_secret(IDENTITY_SECRET)
        self.assertIn("gmcp_probe_mine", self.tools())
        self.assert_denied("gmcp_probe_mine")  # no identity
        self.assert_denied("gmcp_probe_mine", identity_meta(ts=int(time.time()) - 301))  # expired
        self.assert_denied("gmcp_probe_mine", identity_meta(secret="x" * 43))  # bad signature
        result = self.call_tool("gmcp_probe_mine", meta=identity_meta())["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.call_log("gmcp_probe_mine")[:1].status, "ok")

    def test_tool_settings_synced_from_registry(self):
        Setting = self.env["gental.mcp.tool.setting"].sudo()
        stale = Setting.create({"name": "gmcp_gone", "area": "contacts"})
        Setting._sync_from_registry()
        Setting.search([("name", "=", "gmcp_probe_read")]).enabled = False
        Setting._sync_from_registry()  # keeps the owner's choice
        rows = {s.name: s for s in Setting.search([])}
        self.assertEqual(set(rows), set(curated.REGISTRY))
        self.assertFalse(stale.exists())
        self.assertFalse(rows["gmcp_probe_read"].enabled)
        self.assertTrue(rows["gmcp_probe_sales"].enabled)
        self.assertEqual((rows["gmcp_probe_sales"].area, rows["gmcp_probe_sales"].kind), ("sales", "read"))

    def test_curated_page_loads(self):
        arch = self.env["gental.mcp.config"].with_user(self.manager_user).get_views([(False, "form")])
        arch = arch["views"]["form"]["arch"]
        self.assertIn('name="curated"', arch)
        self.assertIn("area_pos_enabled", arch)
        self.assertIn("tool_setting_ids", arch)
        self.env["gental.mcp.tool.setting"]._sync_from_registry()
        config = self.config.with_user(self.manager_user)
        self.assertFalse(config.area_pos_enabled)
        self.assertTrue(config.area_contacts_installed)
        with Form(config) as form:
            form.area_contacts_enabled = False
        self.assertFalse(self.config.area_contacts_enabled)
        self.assertIn("gmcp_probe_read", config.tool_setting_ids.mapped("name"))

    def test_visible_records_checks_ids_as_acting_user(self):
        partner = self.env["res.partner"].create({"name": "gmcp visible"})
        other_company = self.env["res.company"].create({"name": "gmcp other company"})
        # Hidden by the multi-company record rule: the agent only works in the main company.
        hidden = self.env["res.partner"].create({"name": "gmcp other", "company_id": other_company.id})
        ctx = CallContext(self.env(user=self.agent_user), self.agent_user.id, self.client.id, "en")
        found = curated.visible_records(ctx, "res.partner", [partner.id])
        self.assertEqual((found, found.env.uid, found.env.su), (partner, self.agent_user.id, False))
        for ids in ([partner.id, hidden.id], [partner.id, 0]):
            with self.assertRaises(ToolError):
                curated.visible_records(ctx, "res.partner", ids)
        self.assertFalse(curated.visible_records(ctx, "res.partner", []))
