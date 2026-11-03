from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.modules import module as odoo_module

PERIODS = (7, 30, 90)
TOP_TOOLS = 5


class GentalMcpDashboard(models.AbstractModel):
    """Numbers for the Gental MCP Dashboard screen. Managers only."""

    _name = "gental.mcp.dashboard"
    _description = "Gental MCP Dashboard"

    @api.model
    def get_data(self, period_days=7):
        """Everything the Dashboard shows for the last ``period_days`` days (7, 30 or 90)."""
        if not self.env.user.has_group("gental_mcp.group_mcp_manager"):
            raise AccessError(_("Only Gental MCP managers can see the Dashboard."))
        period_days = period_days if period_days in PERIODS else 7
        since = fields.Datetime.now() - timedelta(days=period_days)
        # Bookkeeping reads (Inbox rows of every user) run as superuser, after the manager check above.
        env = self.env(su=True)
        dash = env[self._name]
        config = env["gental.mcp.config"].get()
        return {
            "status": {
                "enabled": bool(config.enabled),
                "mcp_url": config.mcp_url(),
                "version": (odoo_module.get_manifest("gental_mcp") or {}).get("version", ""),
            },
            "clients": dash._dashboard_clients(),
            "calls": dash._dashboard_calls(since, period_days),
            "top_tools": dash._dashboard_top_tools(since),
            "attention": dash._dashboard_attention(since),
            "documents": dash._dashboard_documents(since, 5),
            "webhooks": dash._dashboard_webhooks(since),
        }

    def _dashboard_clients(self):
        self.env.cr.execute(
            "SELECT kind, count(*) FROM gental_mcp_client WHERE state = 'active' GROUP BY kind"
        )
        by_kind = {"oauth": 0, "api_key": 0}
        by_kind.update(dict(self.env.cr.fetchall()))
        return {"total": sum(by_kind.values()), "by_kind": by_kind}

    def _dashboard_calls(self, since, period_days):
        cr = self.env.cr
        cr.execute(
            """SELECT count(*),
                      count(*) FILTER (WHERE status = 'error'),
                      count(*) FILTER (WHERE status = 'unknown'),
                      percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                 FROM gental_mcp_log WHERE kind = 'call' AND create_date >= %s""",
            (since,),
        )
        # ``unknown`` outcomes (a write that may have happened) are counted on their own, not as errors.
        total, errors, unknown, p95 = cr.fetchone()
        cr.execute(
            """SELECT create_date::date, count(*) FROM gental_mcp_log
                WHERE kind = 'call' AND create_date >= %s GROUP BY 1""",
            (since,),
        )
        counts = dict(cr.fetchall())
        today = fields.Datetime.now().date()
        per_day = []
        for offset in range(period_days - 1, -1, -1):
            day = today - timedelta(days=offset)
            per_day.append({"date": fields.Date.to_string(day), "count": counts.get(day, 0)})
        return {
            "total": total,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "unknown": unknown,
            "p95_ms": int(round(p95)) if p95 is not None else 0,
            "per_day": per_day,
        }

    def _dashboard_top_tools(self, since):
        self.env.cr.execute(
            """SELECT tool, count(*) AS n, mode() WITHIN GROUP (ORDER BY tool_kind)
                 FROM gental_mcp_log
                WHERE kind = 'call' AND tool IS NOT NULL AND create_date >= %s
                GROUP BY tool ORDER BY n DESC, tool LIMIT %s""",
            (since, TOP_TOOLS),
        )
        return [{"tool": t, "count": n, "kind": k or ""} for t, n, k in self.env.cr.fetchall()]

    def _dashboard_attention(self, since):
        """Things a manager should look at; extend with ``super()`` and append rows.

        Each row is ``{"kind", "label", "count", "action"}``; ``action`` is either
        ``{"res_model", "domain"}`` (open a filtered list) or ``{"xml_id"}`` (open an action).
        """
        rows = []
        denied_domain = ["|", ("kind", "=", "denied"), ("status", "=", "denied"),
                         ("create_date", ">=", fields.Datetime.to_string(since))]
        denied = self.env["gental.mcp.log"].search_count(denied_domain)
        if denied:
            rows.append({
                "kind": "denied",
                "label": _("Calls that were denied"),
                "count": denied,
                "action": {"res_model": "gental.mcp.log", "domain": denied_domain, "name": _("Inbox")},
            })
        unknown_domain = [("kind", "=", "call"), ("status", "=", "unknown"),
                          ("create_date", ">=", fields.Datetime.to_string(since))]
        unknown = self.env["gental.mcp.log"].search_count(unknown_domain)
        if unknown:
            rows.append({
                "kind": "unknown",
                "label": _("Actions whose outcome is unknown (check them in Odoo)"),
                "count": unknown,
                "action": {"res_model": "gental.mcp.log", "domain": unknown_domain, "name": _("Inbox")},
            })
        if self.env["gental.mcp.config"].get().reachability == "failed":
            rows.append({
                "kind": "reachability",
                "label": _("The public URL was not reachable at the last check"),
                "count": 1,
                "action": {"xml_id": "gental_mcp.server_action_config"},
            })
        rows += self.env["gental.mcp.webhook.delivery"].attention_items()
        return rows

    def _dashboard_webhooks(self, since):
        """Webhook delivery counts since ``since``: ``{"delivered", "failing", "queued"}``."""
        return self.env["gental.mcp.webhook.delivery"].stats(since)

    def _dashboard_documents(self, since, limit):
        """Recent documents served to assistants as ``{"name", "client", "created"}``, newest first."""
        docs = self.env["gental.mcp.document"].search([("create_date", ">=", since)], order="id desc", limit=limit)
        return [
            {"name": doc.name, "client": doc.client_id.name or "",
             "created": fields.Datetime.to_string(doc.create_date)}
            for doc in docs
        ]
