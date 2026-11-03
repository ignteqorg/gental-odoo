"""At most one record per Gental action key (spec §7.1): a confirmed action retried after a lost response
returns the record it already created instead of creating a duplicate."""
import re
from typing import Callable, Optional, Tuple

from odoo import _, models
from odoo.exceptions import AccessError

from .. import compat
from ..core import meta
from ..core.context import ODOO_LANGS
from ..core.tools import ToolError, ToolOutcomeUnknown
from ..models.action_key import ActionKeyBusy

KEY_MAX = 200
KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,%d}" % KEY_MAX)


class _KeyTaken(Exception):
    """Unwinds the creating savepoint when the key was claimed first in this transaction's view."""

    def __init__(self, found, res_id):
        super().__init__(found, res_id)
        self.found, self.res_id = found, res_id


def once_per_key(ctx, model: str, create_fn: Callable[[], models.Model],
                 key_model: Optional[str] = None) -> Tuple[models.Model, bool]:
    """Create through ``create_fn`` unless the call's action key already produced a ``model`` record.

    ``key_model`` is the namespace the key is stored under (default ``model``), e.g.
    ``"account.move.send:<invoice id>"`` for an email sent once per key and invoice; the stored id is
    still a ``model`` record. Returns ``(record, created)``. Without a key, ``create_fn()`` runs as usual. The existing record is
    returned as the acting user; ``ToolError`` when it is gone, unreadable or was created by another client.

    A key committed by a concurrent call after this transaction began raises a serialization failure: it is
    re-raised when ``ctx.is_retryable`` accepts it (Odoo replays the request, which then finds the record);
    otherwise the call ends with ``ToolOutcomeUnknown``, never with a failure that invites a new attempt.
    """
    key = (ctx.meta or {}).get(meta.ACTION_KEY)
    if key is None or key == "":
        return _single(create_fn()), True
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ToolError(_message(ctx, "invalid"))

    Keys = ctx.env["gental.mcp.action.key"]
    namespace = key_model or model
    found, res_id = Keys._find_res_id(key, namespace, ctx.client_id)
    if found:
        return _existing(ctx, model, res_id), False
    try:
        with ctx.env.cr.savepoint():
            record = _single(create_fn())
            if not Keys._claim(key, namespace, record.id, ctx.client_id):
                # Discard this record and keep the first one (read before the savepoint unwinds).
                raise _KeyTaken(*Keys._find_res_id(key, namespace, ctx.client_id))
    except _KeyTaken as taken:
        if not taken.found:
            raise ToolOutcomeUnknown(_message(ctx, "unknown")) from None
        return _existing(ctx, model, taken.res_id), False
    except ActionKeyBusy as busy:
        cause = busy.__cause__
        if ctx.is_retryable is not None and ctx.is_retryable(cause):
            raise cause from None
        raise ToolOutcomeUnknown(_message(ctx, "unknown")) from None
    return record, True


def _single(record):
    record.ensure_one()
    return record


def _existing(ctx, model, res_id):
    record = ctx.env[model].browse(res_id).exists() if res_id else None
    if not record:
        raise ToolError(_message(ctx, "gone"))
    try:
        compat.check_read_access(record)
    except AccessError:
        raise ToolError(_message(ctx, "gone")) from None
    return record


def _message(ctx, which):
    # ``_`` reads the language from a local ``context`` (Odoo 17+), so result text follows ``ctx.locale``.
    context = {"lang": ODOO_LANGS.get(ctx.locale, "en_US")}  # noqa: F841 - read by ``_``
    if which == "invalid":
        return _("The action key must be 1 to %s characters: letters, digits, “.”, “_”, “:” or “-”.", KEY_MAX)
    if which == "unknown":
        return _("This action may already be done. Check before doing it again.")
    return _("The record this action created is no longer available.")
