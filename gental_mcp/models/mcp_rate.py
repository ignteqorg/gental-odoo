import time

from odoo import api, fields, models

from ..compat import create_unique_index, private
from .mcp_log import _bookkeeping_env


class GentalMcpRate(models.Model):
    """Fixed one-minute window counters for rate limiting (per client, per IP)."""

    _name = "gental.mcp.rate"
    _description = "Gental MCP Rate Counter"

    key = fields.Char(required=True)
    window = fields.Integer(required=True, help="Epoch minute")
    count = fields.Integer(default=0)

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_rate_key_window_uniq", self._table, ["key", '"window"'])

    @private
    @api.model
    def hit(self, key, limit):
        """Count one request for ``key`` in the current minute; True while the count is within ``limit``."""
        window = int(time.time() // 60)
        with _bookkeeping_env(self.env) as env:
            env.cr.execute(
                f"""INSERT INTO {self._table} (key, "window", count) VALUES (%s, %s, 1)
                    ON CONFLICT (key, "window") DO UPDATE SET count = {self._table}.count + 1
                    RETURNING count""",
                (key, window),
            )
            count = env.cr.fetchone()[0]
        return count <= limit
