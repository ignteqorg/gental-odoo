from odoo import api, fields, models

from ..compat import create_unique_index, private

SERIALIZATION_FAILURE = "40001"


class ActionKeyBusy(Exception):
    """A concurrent transaction committed the same key after this transaction's snapshot was taken.

    Raised from the Postgres serialization failure, which stays the ``__cause__`` so the caller can let it
    escape and have Odoo replay the request with a fresh snapshot.
    """


class GentalMcpActionKey(models.Model):
    """At most one record per Gental action key and model (spec §7.1): the key a confirmed action carries."""

    _name = "gental.mcp.action.key"
    _description = "Gental MCP Action Key"
    _order = "id desc"

    key = fields.Char(required=True, readonly=True)
    model = fields.Char(required=True, readonly=True)
    res_id = fields.Integer(string="Record ID", required=True, readonly=True)
    client_id = fields.Many2one("gental.mcp.client", string="Client", ondelete="set null", readonly=True)

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_action_key_key_model_uniq", self._table, ["key", "model"])

    @private
    @api.model
    def _find_res_id(self, key, model, client_id):
        """``(found, res_id)`` for ``key`` on ``model`` (bookkeeping lookup only).

        ``res_id`` is None when the key belongs to another client, so a key never reveals another client's record.
        """
        row = self.sudo().search([("key", "=", key), ("model", "=", model)], limit=1)
        if not row:
            return False, None
        return True, (row.res_id if row.client_id.id == (client_id or False) else None)

    @private
    @api.model
    def _claim(self, key, model, res_id, client_id):
        """Bind ``key`` on ``model`` to ``res_id`` in the current transaction; False if the key is taken.

        Raises ``ActionKeyBusy`` when a concurrent transaction committed the key after this transaction's
        snapshot (Odoo runs REPEATABLE READ, so Postgres reports a serialization failure).
        """
        try:
            self.env.cr.execute(
                f"""INSERT INTO {self._table} (key, model, res_id, client_id, create_uid, write_uid,
                                               create_date, write_date)
                    VALUES (%s, %s, %s, %s, %s, %s, now() at time zone 'UTC', now() at time zone 'UTC')
                    ON CONFLICT (key, model) DO NOTHING
                    RETURNING id""",
                (key, model, res_id, client_id or None, self.env.uid, self.env.uid),
                log_exceptions=False,
            )
        except Exception as e:  # psycopg2 errors carry ``pgcode``
            if getattr(e, "pgcode", None) == SERIALIZATION_FAILURE:
                raise ActionKeyBusy(key) from e
            raise
        return bool(self.env.cr.fetchone())
