from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.gental_mcp import compat
from odoo.addons.gental_mcp.models.mcp_config import proxy_warning

from .common import GentalMcpCase, use_base_url

ACTIONS = [
    "gental_mcp.action_clients",
    "gental_mcp.action_logs_inbox",
    "gental_mcp.action_api_key_wizard",
]


@tagged("post_install", "-at_install", "gental_mcp")
class TestScreens(GentalMcpCase):
    def test_wizard_creates_key_and_shows_once(self):
        Wizard = self.env["gental.mcp.api.key.wizard"].with_user(self.manager_user)
        wizard = Wizard.create({"name": "Desk key"})
        self.assertEqual(wizard.user_id, self.manager_user)
        self.assertFalse(wizard.can_pick_user)
        action = wizard.action_create()
        plaintext = action["context"]["gmcp_new_key"]
        self.assertTrue(plaintext.startswith("gmcp_"))
        # The second step is an unsaved record carrying the key in its context only.
        shown = Wizard.with_context(**action["context"]).new({})
        self.assertEqual((shown.state, shown.key), ("done", plaintext))
        # Opening the wizard again, or re-reading the saved record, never reveals it.
        self.assertFalse(Wizard.browse(wizard.id).key)
        self.assertFalse(Wizard.new({}).key)
        self.assertEqual(Wizard.new({}).state, "form")
        client = self.env["gental.mcp.client"].find_by_api_key(plaintext)
        self.assertEqual((client.name, client.user_id), ("Desk key", self.manager_user))
        self.assertNotEqual(client.key_hash, plaintext)

    def test_wizard_key_for_another_user_needs_admin(self):
        """A manager can't mint a key acting as someone else (e.g. the admin); a system admin can."""
        admin = self.env.ref("base.user_admin")
        Wizard = self.env["gental.mcp.api.key.wizard"].with_user(self.manager_user)
        for target in (self.agent_user, admin):
            wizard = Wizard.create({"name": "Escalate", "user_id": target.id})
            with self.subTest(target=target.login), self.assertRaises(AccessError):
                wizard.action_create()
        admin_wizard = self.env["gental.mcp.api.key.wizard"].with_user(admin).create(
            {"name": "For the agent", "user_id": self.agent_user.id}
        )
        self.assertTrue(admin_wizard.can_pick_user)
        plaintext = admin_wizard.action_create()["context"]["gmcp_new_key"]
        self.assertEqual(self.env["gental.mcp.client"].find_by_api_key(plaintext).user_id, self.agent_user)

    def test_purge_cron_respects_retention(self):
        config = self.env["gental.mcp.config"].get()
        config.log_retention_days = 30
        Log = self.env["gental.mcp.log"].sudo()
        old, fresh = Log.create([{"tool": "old"}, {"tool": "fresh"}])
        self.env.cr.execute(
            "UPDATE gental_mcp_log SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=31), old.id),
        )
        client = self.env["gental.mcp.client"].sudo().create({"name": "c", "kind": "oauth", "client_id": "c-purge"})
        now = fields.Datetime.now()
        Token = self.env["gental.mcp.token"].sudo()
        common = {"client_id": client.id, "user_id": self.agent_user.id, "kind": "access", "family": "f"}
        stale = Token.create(dict(common, token_hash="h1", expires_at=now - timedelta(days=8)))
        recent = Token.create(dict(common, token_hash="h2", expires_at=now - timedelta(days=1)))
        Code = self.env["gental.mcp.auth.code"].sudo()
        code_common = {
            "client_id": client.id, "user_id": self.agent_user.id, "redirect_uri": "https://x/cb",
            "code_challenge": "c",
        }
        stale_code = Code.create(dict(code_common, code_hash="c1", expires_at=now - timedelta(days=8)))
        recent_code = Code.create(dict(code_common, code_hash="c2", expires_at=now - timedelta(days=1)))
        Rate = self.env["gental.mcp.rate"].sudo()
        old_rate, new_rate = Rate.create([
            {"key": "k-old", "window": 5, "count": 1},
            {"key": "k-new", "window": 10 ** 9, "count": 1},
        ])
        self.env.flush_all()
        self.env.ref("gental_mcp.cron_purge_logs").ir_actions_server_id.run()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists() or stale_code.exists())
        self.assertTrue(recent.exists() and recent_code.exists())
        self.assertFalse(old_rate.exists())
        self.assertTrue(new_rate.exists())

    def test_reachability_rejects_http_url(self):
        config = self.env["gental.mcp.config"].get()
        use_base_url(self.env, "http://odoo.example.com")
        with patch("requests.get") as get, self.assertRaises(UserError):
            config.action_check_reachability()
        get.assert_not_called()

    def test_reachability_reports_result(self):
        config = self.env["gental.mcp.config"].get()
        config.public_url = "https://odoo.example.com/"
        with patch("requests.get", return_value=MagicMock(status_code=200)) as get:
            result = config.action_check_reachability()
        get.assert_called_once_with(
            "https://odoo.example.com/.well-known/oauth-protected-resource", timeout=5, allow_redirects=False
        )
        self.assertEqual(result["params"]["type"], "success")
        with patch("requests.get", side_effect=requests.ConnectionError()):
            result = config.action_check_reachability()
        self.assertEqual(result["params"]["type"], "warning")

    def test_successful_check_pins_public_url(self):
        """Ruling R21: web.base.url moves when an admin signs in elsewhere, so a check that worked saves it."""
        config = self.env["gental.mcp.config"].get()
        use_base_url(self.env, "https://ERP.example.com/")
        with patch("requests.get", side_effect=requests.ConnectionError()):
            config.action_check_reachability()
        self.assertFalse(config.public_url)  # a failed check pins nothing
        with patch("requests.get", return_value=MagicMock(status_code=200)):
            result = config.action_check_reachability()
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual(config.public_url, "https://erp.example.com")
        compat.set_param(self.env["ir.config_parameter"].sudo(), "web.base.url", "http://127.0.0.1:8069")
        self.assertEqual(config.mcp_url(), "https://erp.example.com/mcp")  # an admin login can't move it now
        config.public_url = "https://other.example.com"
        with patch("requests.get", return_value=MagicMock(status_code=200)):
            config.action_check_reachability()
        self.assertEqual(config.public_url, "https://other.example.com")  # an override is never replaced

    def test_unpinned_url_warning(self):
        config = self.env["gental.mcp.config"].get()
        params = self.env["ir.config_parameter"].sudo()
        use_base_url(self.env, "https://odoo.example.com")
        compat.set_param(params, "web.base.url.freeze", False)
        config.invalidate_recordset(["url_unpinned"])
        self.assertTrue(config.url_unpinned)
        compat.set_param(params, "web.base.url.freeze", "True")
        config.invalidate_recordset(["url_unpinned"])
        self.assertFalse(config.url_unpinned)
        compat.set_param(params, "web.base.url.freeze", "False")  # read as a boolean, not as a non-empty string
        config.invalidate_recordset(["url_unpinned"])
        self.assertTrue(config.url_unpinned)
        compat.set_param(params, "web.base.url.freeze", False)
        config.public_url = "https://odoo.example.com"
        self.assertFalse(config.url_unpinned)
        arch = self.env["gental.mcp.config"].get_views([(False, "form")])["views"]["form"]["arch"]
        self.assertIn("url_unpinned_warning", arch)

    def test_proxy_warning(self):
        """A private remote address with proxy_mode off means a proxy Odoo doesn't know about."""
        self.assertIn("proxy_mode = True", proxy_warning(self.env, "10.0.0.7", False))
        self.assertIn("172.18.0.2", proxy_warning(self.env, "172.18.0.2", False))
        self.assertEqual(proxy_warning(self.env, "10.0.0.7", True), "")
        self.assertEqual(proxy_warning(self.env, "93.184.216.34", False), "")
        self.assertEqual(proxy_warning(self.env, None, False), "")
        self.assertEqual(proxy_warning(self.env, "not-an-ip", False), "")

    def test_views_load_for_manager(self):
        env = self.env(user=self.manager_user)
        for xmlid in ACTIONS:
            action = env.ref(xmlid).sudo()
            views = env[action.res_model].get_views([(False, "list"), (False, "form"), (False, "search")]
                                                    if action.res_model != "gental.mcp.api.key.wizard"
                                                    else [(False, "form")])
            self.assertTrue(views["views"], xmlid)
        config = env["gental.mcp.config"].get()
        views = env["gental.mcp.config"].get_views([(False, "form")])
        self.assertIn("mcp_tabs", views["views"]["form"]["arch"])
        server_action = env.ref("gental_mcp.server_action_config").sudo()
        self.assertEqual(server_action.with_user(self.manager_user).run()["res_id"], config.id)
        # The menu tree is visible to a manager, hidden from a plain connect user.
        menus = env["ir.ui.menu"].load_menus(False)
        self.assertIn(str(env.ref("gental_mcp.menu_clients").id), {str(k) for k in menus})
        agent_menus = self.env(user=self.agent_user)["ir.ui.menu"].load_menus(False)
        self.assertNotIn(env.ref("gental_mcp.menu_clients").id, [m.get("id") for m in agent_menus.values()])

    def test_ar_translation_present(self):
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["gental_mcp"], ["ar_001"], overwrite=True)
        menu = self.env.ref("gental_mcp.menu_clients").with_context(lang="ar_001")
        self.assertNotEqual(menu.name, "Clients")
        self.assertTrue(menu.name)
        arabic = compat.translate("ar_001", "Gental MCP has a single settings record.")
        action = self.env["gental.mcp.config"].with_context(lang="ar_001").action_open_config()
        self.assertEqual(action["name"], "الإعدادات")
        self.assertNotEqual(arabic, "Gental MCP has a single settings record.")
        field = self.env["gental.mcp.config"].with_context(lang="ar_001").fields_get(["public_url"])
        self.assertNotEqual(field["public_url"]["string"], "Public URL")
