from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged

from .common import GentalMcpCase


@tagged("post_install", "-at_install", "gental_mcp")
class TestDashboard(GentalMcpCase):
    def _log(self, **vals):
        vals.setdefault("kind", "call")
        vals.setdefault("tool", "find_contact")
        vals.setdefault("status", "ok")
        vals.setdefault("duration_ms", 100)
        return self.env["gental.mcp.log"].sudo().create(vals)

    def _data(self, days=7):
        return self.env["gental.mcp.dashboard"].with_user(self.manager_user).get_data(days)

    def test_get_data_counts_and_error_rate(self):
        before = self._data()["calls"]["total"]
        for i in range(10):
            self._log(status="error" if i < 2 else "ok", tool="my_tool")
        data = self._data()
        self.assertEqual(data["calls"]["total"], before + 10)
        if before == 0:
            self.assertEqual(data["calls"]["error_rate"], 0.2)
        self.assertEqual(data["calls"]["per_day"][-1]["count"], before + 10)
        self.assertEqual(len(data["calls"]["per_day"]), 7)
        self.assertEqual(data["top_tools"][0]["tool"], "my_tool")
        self.assertEqual(set(data), {"status", "clients", "calls", "top_tools", "attention", "documents", "webhooks"})
        self.assertEqual(data["webhooks"], {"delivered": 0, "failing": 0, "queued": 0})
        self.assertEqual(data["documents"], [])
        self.assertTrue(data["status"]["mcp_url"].endswith("/mcp"))

    def test_recent_documents_newest_first(self):
        Document = self.env["gental.mcp.document"]
        client, _key = self.env["gental.mcp.client"].create_api_key("Reports", self.agent_user)
        for name in ("A.pdf", "B.pdf"):
            Document.store(name=name, content=b"%PDF-1.4", res_model="res.partner", res_id=1,
                           report_xmlid="base.report", tool="get_invoice_document", client_id=client.id,
                           user_id=self.agent_user.id)
        docs = self._data()["documents"]
        self.assertEqual([d["name"] for d in docs][:2], ["B.pdf", "A.pdf"])
        self.assertEqual(docs[0]["client"], "Reports")
        self.assertTrue(docs[0]["created"])

    def test_unknown_outcomes_counted_apart_from_errors(self):
        self.env["gental.mcp.log"].sudo().search([]).unlink()
        self._log(status="ok")
        self._log(status="error")
        self._log(status="unknown", tool="send_invoice")
        data = self._data()
        self.assertEqual((data["calls"]["total"], data["calls"]["error_rate"], data["calls"]["unknown"]),
                         (3, round(1 / 3, 4), 1))
        rows = [r for r in data["attention"] if r["kind"] == "unknown"]
        self.assertEqual((len(rows), rows[0]["count"]), (1, 1))
        self.assertEqual(rows[0]["action"]["res_model"], "gental.mcp.log")

    def test_p95(self):
        self.env["gental.mcp.log"].sudo().search([]).unlink()
        for ms in range(1, 101):
            self._log(duration_ms=ms)
        self.assertEqual(self._data()["calls"]["p95_ms"], 95)

    def test_attention_lists_denied(self):
        self.env["gental.mcp.log"].sudo().search([]).unlink()
        self._log(kind="denied", status="denied")
        self._log(kind="call", status="denied")
        rows = [r for r in self._data()["attention"] if r["kind"] == "denied"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["action"]["res_model"], "gental.mcp.log")
        self.env["gental.mcp.config"].get().reachability = "failed"
        self.assertIn("reachability", [r["kind"] for r in self._data()["attention"]])

    def test_clients_and_period_fallback(self):
        self.make_api_key(self.agent_user)
        data = self._data(5)  # not an allowed period: falls back to 7
        self.assertEqual(len(data["calls"]["per_day"]), 7)
        self.assertGreaterEqual(data["clients"]["by_kind"]["api_key"], 1)

    def test_non_manager_access_error(self):
        with self.assertRaises(AccessError):
            self.env["gental.mcp.dashboard"].with_user(self.agent_user).get_data(7)


    def test_assets_and_action_are_registered(self):
        """The tours need a browser the harness may lack; this checks the wiring they rely on."""
        paths = [a[0] for a in self.env["ir.asset"]._get_asset_paths("web.assets_backend", {})]
        for name in ("dashboard.js", "dashboard.xml", "dashboard.scss"):
            self.assertTrue(any(p.endswith("gental_mcp/static/src/dashboard/" + name) for p in paths), name)
        action = self.env.ref("gental_mcp.action_dashboard")
        self.assertEqual(action.tag, "gental_mcp_dashboard")
        menu = self.env.ref("gental_mcp.menu_dashboard")
        self.assertTrue(menu.active)
        self.assertEqual(menu.action, action)


@tagged("post_install", "-at_install", "gental_mcp")
class TestDashboardTour(HttpCase):
    def test_dashboard_tour_renders(self):
        self.start_tour("/odoo/action-gental_mcp.action_dashboard", "gental_mcp_dashboard_tour", login="admin")

    def test_dashboard_tour_rtl(self):
        self.env["res.lang"]._activate_lang("ar_001")
        self.env.ref("base.user_admin").lang = "ar_001"
        self.start_tour("/odoo/action-gental_mcp.action_dashboard", "gental_mcp_dashboard_rtl_tour", login="admin")
