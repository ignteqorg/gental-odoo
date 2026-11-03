from odoo.tests import tagged

from .common import GentalMcpCase


@tagged("post_install", "-at_install", "gental_mcp")
class TestInstall(GentalMcpCase):
    def test_groups_exist_and_manager_implies_connect(self):
        mgr = self.env.ref("gental_mcp.group_mcp_manager")
        self.assertIn(self.env.ref("gental_mcp.group_mcp_connect"), mgr.implied_ids)

    def test_admin_is_manager(self):
        self.assertTrue(self.env.ref("base.user_admin").has_group("gental_mcp.group_mcp_manager"))

    def test_helper_users(self):
        self.assertTrue(self.agent_user.has_group("gental_mcp.group_mcp_connect"))
        self.assertFalse(self.agent_user.has_group("gental_mcp.group_mcp_manager"))
        self.assertTrue(self.manager_user.has_group("gental_mcp.group_mcp_connect"))

    def test_root_menu(self):
        self.assertEqual(self.env.ref("gental_mcp.menu_root").name, "Gental MCP")
