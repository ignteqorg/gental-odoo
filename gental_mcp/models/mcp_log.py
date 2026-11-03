import contextlib
import json
import logging
import re

from odoo import api, fields, models
from odoo.modules import module as odoo_module

from ..compat import private

_logger = logging.getLogger(__name__)

SECRET_KEY_RE = re.compile(r"(pass|secret|token|api_?key|authorization|totp|otp)", re.IGNORECASE)
MASK = "•••"
RESULT_MAX = 4000


def _bookkeeping_env(env):
    """A context manager yielding a superuser env on its own cursor, committed on exit.

    Bookkeeping rows (Inbox logs, rate counters, last use) must survive a rollback of the request.
    Under tests the current cursor is used instead, so the test transaction sees the rows.
    Private to ``models/``: elsewhere, call a ``@private`` model method instead.
    """
    if under_test():
        return contextlib.nullcontext(env(su=True))
    return _own_cursor_env(env)


def under_test():
    """True while a test runs (Odoo sets ``current_test``; absent on some versions)."""
    return bool(getattr(odoo_module, "current_test", False))


@contextlib.contextmanager
def _own_cursor_env(env):
    with env.registry.cursor() as cr:
        yield env(cr=cr, su=True)


class GentalMcpLog(models.Model):
    """The Inbox: one row per MCP call, auth event, denial or rate-limit hit."""

    _name = "gental.mcp.log"
    _description = "Gental MCP Inbox Entry"
    _order = "id desc"

    kind = fields.Selection(
        [("call", "Call"), ("auth", "Sign-in"), ("denied", "Denied"), ("rate_limited", "Rate Limited")],
        required=True, default="call", index=True,
    )
    client_id = fields.Many2one("gental.mcp.client", ondelete="set null", index=True)
    user_id = fields.Many2one("res.users", string="User", ondelete="set null", index=True)
    tool = fields.Char()
    tool_kind = fields.Selection([("read", "Read"), ("write", "Write"), ("destructive", "Destructive")])
    # ``unknown``: a write may have happened but the module can't prove it (spec §7.4); not an error.
    status = fields.Selection(
        [("ok", "OK"), ("error", "Error"), ("denied", "Denied"), ("unknown", "Outcome Unknown")], index=True,
    )
    duration_ms = fields.Integer(string="Duration (ms)")
    args_json = fields.Text(string="Arguments")
    result_text = fields.Text(string="Result")
    res_model = fields.Char(string="Record Model")
    res_id = fields.Integer(string="Record ID")
    action_key = fields.Char()
    identity_phone = fields.Char(string="Requester Phone")
    ip = fields.Char(string="IP Address")
    request_id = fields.Char(string="Request ID", index=True)

    @staticmethod
    @private
    def mask_args(args):
        """Copy of ``args`` with every value under a secret-looking key replaced by ``•••`` (recursive)."""
        if isinstance(args, dict):
            return {
                k: MASK if isinstance(k, str) and SECRET_KEY_RE.search(k) else GentalMcpLog.mask_args(v)
                for k, v in args.items()
            }
        if isinstance(args, list):
            return [GentalMcpLog.mask_args(v) for v in args]
        return args

    @private
    @api.model
    def record_throttled(self, key, **vals):
        """``record_call`` at most once a minute per ``key``, for events a caller can repeat at will
        (rate-limit hits, a revoked token replayed in a loop), so they can't flood the Inbox."""
        if self.env["gental.mcp.rate"].hit(f"log:{key}", 1):
            return self.record_call(**vals)
        return None

    @private
    @api.model
    def record_call(self, **vals):
        """Write an Inbox row that survives a rollback of the call; returns its id (int), or None.

        ``args`` (a dict) is masked and stored as ``args_json``; ``result_text`` is cut to 4000 chars.
        In production the row is written on its own cursor, best effort: a failure is logged and never
        breaks the request. That cursor cannot see rows the request created but has not committed, so
        callers must not pass ids (``client_id``, ``user_id``, ...) of such rows (pass None instead),
        and the new row is not visible in the request's own snapshot.
        """
        args = vals.pop("args", None)
        if args is not None:
            vals["args_json"] = json.dumps(self.mask_args(args), ensure_ascii=False, default=str)
        if vals.get("result_text"):
            vals["result_text"] = str(vals["result_text"])[:RESULT_MAX]
        if under_test():
            return self.sudo().create(vals).id
        try:
            with _bookkeeping_env(self.env) as env:
                return env[self._name].create(vals).id
        except Exception:
            _logger.exception("Gental MCP: could not write an Inbox entry (%s)", vals.get("kind"))
            return None
