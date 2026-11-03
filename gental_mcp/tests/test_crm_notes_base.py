from odoo.tests import tagged

from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.tools import ToolError
from odoo.addons.gental_mcp.services import areas, curated

from .common import GentalMcpCase


@tagged("post_install", "-at_install", "gental_mcp")
class TestCrmNotesBase(GentalMcpCase):
    def test_log_note_model_enum_filters_uninstalled(self):
        if areas.installed_areas(self.env) != {"contacts"}:
            self.skipTest("Business apps are installed in this database.")
        env = self.env(user=self.agent_user)
        ctx = CallContext(env, self.agent_user.id, 0, "en")
        specs = {s.name: s for s in curated.CuratedToolProvider(env).list_tools(ctx)}
        for tool in ("log_note", "schedule_activity"):
            options = specs[tool].input_schema["properties"]["model"]["oneOf"]
            self.assertEqual([o["const"] for o in options], ["res.partner"])
            self.assertTrue(all(o["title"] for o in options))
        self.assertNotIn("create_lead", specs)
        with self.assertRaises(ToolError):
            curated.CuratedToolProvider(env).call(
                "log_note", {"model": "crm.lead", "record_id": 1, "body": "x"}, ctx)
