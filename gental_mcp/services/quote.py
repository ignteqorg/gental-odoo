"""Signed preview quotes (spec §7.2): what ``preview_quotation`` priced, signed so ``create_quotation`` can
prove the customer saw these exact lines and this total before anything is written.

``token = base64url(json(payload + {"exp"})) + "." + hmac_b64url(secret, "quote|" + body)``. The secret is
the module's per-database signing secret (``gental.mcp.config.quote_secret()``), shared with document links;
the ``quote|`` prefix keeps a document signature from ever passing as a quote.
"""
import base64
import binascii
import json
import time

from odoo import Command
from odoo.tools.translate import _lt

from .. import compat
from ..core import crypto
from ..core.context import ODOO_LANGS
from ..core.tools import ToolError

TTL_SECONDS = 15 * 60
PREFIX = "quote|"
EXPIRED = _lt("This quote expired. Preview the quotation again and show the customer the new total.")
INVALID = _lt("This quote isn't valid. Preview the quotation again.")


def now() -> int:
    """Current unix time (a function so tests can move the clock)."""
    return int(time.time())


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _refuse(term, locale):
    return ToolError(compat.translate(ODOO_LANGS.get(locale, "en_US"), term))


def sign(secret: str, payload: dict, now: int, ttl: int = TTL_SECONDS) -> str:
    """The signed quote for ``payload``, valid ``ttl`` seconds from ``now``."""
    data = dict(payload, exp=int(now) + int(ttl))
    body = _b64url(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return body + "." + crypto.hmac_b64url(secret, PREFIX + body)


def verify(secret: str, token, now: int, locale: str = "en") -> dict:
    """The payload of ``token`` (``exp`` included); ``ToolError`` when it is malformed, badly signed or expired."""
    if not secret or not isinstance(token, str) or token.count(".") != 1:
        raise _refuse(INVALID, locale)
    body, sig = token.split(".")
    if not crypto.consteq(sig, crypto.hmac_b64url(secret, PREFIX + body)):
        raise _refuse(INVALID, locale)
    try:
        payload = json.loads(_unb64url(body).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise _refuse(INVALID, locale) from None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise _refuse(INVALID, locale)
    if now > exp:
        raise _refuse(EXPIRED, locale)
    return payload


def price_lines(env, partner, lines: list, pricelist=None) -> dict:
    """Odoo's prices for ``lines`` (``[{"product_id", "qty"}]``) sold to ``partner``, computed on a new,
    unsaved ``sale.order`` in ``env`` (the acting user's): computes run, nothing is written.

    Returns ``{"lines": [{"product_id", "name", "qty", "price_unit", "discount", "taxes", "subtotal"}],
    "amount_untaxed", "amount_tax", "amount_total", "currency"}`` (``taxes`` are tax ids).
    """
    values = {
        "partner_id": partner.id,
        "order_line": [
            Command.create({"product_id": int(line["product_id"]), "product_uom_qty": line["qty"]}) for line in lines
        ],
    }
    if pricelist:
        values["pricelist_id"] = pricelist.id
    order = env["sale.order"].new(values)
    return summarise(order)


def summarise(order) -> dict:
    """The ``price_lines`` shape for an order (new or saved)."""
    taxes_field = compat.sale_line_taxes_field(order.env)
    currency = order.currency_id
    return {
        "lines": [
            {
                "product_id": line.product_id.id,
                "name": line.product_id.with_context(display_default_code=False).display_name,
                "qty": line.product_uom_qty,
                "price_unit": line.price_unit,
                "discount": line.discount,
                "taxes": sorted(line[taxes_field]._origin.ids),
                "subtotal": currency.round(line.price_subtotal),
            }
            for line in order.order_line
            if not line.display_type
        ],
        "amount_untaxed": currency.round(order.amount_untaxed),
        "amount_tax": currency.round(order.amount_tax),
        "amount_total": currency.round(order.amount_total),
        "currency": currency.name,
    }
