"""Gental's signed customer identity (spec §7.3): verify it, then find the customer by phone.

Identity tools ("my orders", "my invoices") never take a phone or customer argument: the customer is
whoever Gental verified on the channel. Anything short of one certain match gives no data.
"""
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

from odoo.exceptions import AccessError
from odoo.tools.translate import _lt

from .. import compat
from ..core import crypto
from ..core.context import ODOO_LANGS
from ..core import meta as gmeta
from ..core.tools import ToolDenied

CHANNELS = frozenset({"whatsapp", "whatsapp_linked", "widget", "api"})
MAX_AGE_SECONDS = 300
CACHE_KEY = "_identity"
MATCH_DIGITS = 9
MIN_DIGITS, MAX_DIGITS = 8, 15
MAX_CANDIDATES = 50
# Countries whose numbers keep the national 0 after the country code (Italy).
TRUNK_ZERO_KEPT = frozenset({"39"})
DENIED = _lt("This needs a verified customer.")


@dataclass(frozen=True)
class VerifiedIdentity:
    channel: str
    phone: str
    verified_at: str
    ts: int


def _text(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def verify(meta: dict, secret: Optional[str], now: int) -> Optional[VerifiedIdentity]:
    """The identity in ``meta["ai.gental/identity"]`` if its signature is valid and fresh, else ``None``.

    ``sig = hmac_b64url(secret, "{channel}|{phone}|{verifiedAt}|{ts}")`` over the values exactly as
    sent; ``ts`` (unix seconds, number or string) must be within 300 s of ``now``.
    """
    if not secret or not isinstance(meta, dict):
        return None
    data = meta.get(gmeta.IDENTITY)
    if not isinstance(data, dict):
        return None
    channel, phone, verified_at, sig = (_text(data.get(k)) for k in ("channel", "phone", "verifiedAt", "sig"))
    raw_ts = data.get("ts")
    if isinstance(raw_ts, int) and not isinstance(raw_ts, bool):
        ts_text = str(raw_ts)
    elif isinstance(raw_ts, str) and raw_ts.isascii() and raw_ts.isdigit():
        ts_text = raw_ts
    else:
        return None
    if not (channel and phone and verified_at and sig):
        return None
    expected = crypto.hmac_b64url(secret, f"{channel}|{phone}|{verified_at}|{ts_text}")
    if not crypto.consteq(sig, expected):
        return None
    ts = int(ts_text)
    if abs(now - ts) > MAX_AGE_SECONDS or channel not in CHANNELS:
        return None
    return VerifiedIdentity(channel, phone, verified_at, ts)


def _denied_text(ctx) -> str:
    return compat.translate(ODOO_LANGS.get(ctx.locale, "en_US"), DENIED)


def require(ctx) -> VerifiedIdentity:
    """The verified customer of this call, or ``ToolDenied`` ("This needs a verified customer.")."""
    cached = ctx.meta.get(CACHE_KEY)
    if isinstance(cached, VerifiedIdentity):
        return cached
    secret = ctx.env["gental.mcp.config"].get().identity_secret
    found = verify(ctx.meta, secret, int(time.time()))
    if found is None:
        raise ToolDenied(_denied_text(ctx))
    ctx.meta[CACHE_KEY] = found
    return found


def normalise_phone(raw, default_country_code) -> Optional[str]:
    """``raw`` as ``+<digits>`` (E.164), or ``None`` when it has fewer than 8 or more than 15 digits.

    ``00`` becomes ``+``; a national number (leading ``0``) gets ``default_country_code`` (the
    calling code, e.g. ``966``), and a ``0`` written right after that code (``+966 050…``) is
    dropped. Arabic-Indic digits count as digits.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    digits = "".join(str(unicodedata.decimal(ch)) for ch in text if ch.isdecimal())
    if not text.startswith("+"):
        if digits.startswith("00"):
            digits = digits[2:]
        elif digits.startswith("0") and default_country_code:
            digits = str(default_country_code) + digits[1:]
    code = str(default_country_code or "")
    if code and code not in TRUNK_ZERO_KEPT and digits.startswith(code + "0"):
        digits = code + digits[len(code) + 1:]
    if not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        return None
    return "+" + digits


def _loose_pattern(digits: str) -> str:
    """A ``=like`` pattern matching ``digits`` with any separators between them."""
    return "%" + "%".join(digits) + "%"


def current(ctx) -> Optional[VerifiedIdentity]:
    """The verified customer of this call, or ``None`` (no secret configured, no identity or a bad one).
    For tools that work without an identity but trust it when present."""
    try:
        return require(ctx)
    except ToolDenied:
        return None


def phone_candidates(env, phone_e164: str):
    """Partners whose phone fields contain the last digits of ``phone_e164`` (any separators), searched as
    ``env``'s user; ``None`` when there are more than ``MAX_CANDIDATES`` (too common to tell)."""
    phone_fields = compat.partner_phone_fields(env)
    if not phone_fields:
        return env["res.partner"]
    pattern = _loose_pattern(phone_e164[1:][-MATCH_DIGITS:])
    domain = ["|"] * (len(phone_fields) - 1) + [(f, "=like", pattern) for f in phone_fields]
    candidates = env["res.partner"].search(domain, limit=MAX_CANDIDATES + 1)
    return None if len(candidates) > MAX_CANDIDATES else candidates


def same_phone(partners, phone_e164: str):
    """The ``partners`` with a number that is exactly ``phone_e164`` once normalised.

    A partner's numbers are read in its country. A partner without a country uses the company's country only
    when the setting ``identity_assume_company_country`` is on; otherwise its national-format numbers never match.
    """
    env = partners.env
    phone_fields = compat.partner_phone_fields(env)
    assume_company = env["gental.mcp.config"].get().identity_assume_company_country
    company_code = env.company.country_id.phone_code if assume_company else None

    def default_code(partner):
        return partner.country_id.phone_code if partner.country_id else company_code

    return partners.filtered(
        lambda p: any(normalise_phone(p[f], default_code(p)) == phone_e164 for f in phone_fields)
    )


def match_partner(env, phone_e164: str):
    """The one commercial partner whose phone is ``phone_e164``, searched as ``env``'s user.

    Numbers are compared as ``same_phone`` does. Contacts resolve to their company. No match, several
    commercial partners, too many candidates or an unreadable result give ``None``.
    """
    target = normalise_phone(phone_e164, None)
    if not target or not compat.partner_phone_fields(env):
        return None
    candidates = phone_candidates(env, target)
    if candidates is None:
        return None
    commercial = same_phone(candidates, target).commercial_partner_id
    if len(commercial) != 1:
        return None
    try:
        compat.check_read_access(commercial)
    except AccessError:
        return None
    return commercial
