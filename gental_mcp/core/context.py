"""Per-call context handed to tool providers. Core never imports odoo."""
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional, Union

from . import meta as gmeta

LOCALES = ("ar", "en")
# Request locale -> Odoo language code of this module's translations.
ODOO_LANGS = {"ar": "ar_001", "en": "en_US"}


def resolve_locale(meta: dict, default: str, accept_language: Optional[str] = None) -> str:
    """The request's locale: ``_meta["ai.gental/locale"]`` when it is ``ar`` or ``en``, else the first
    ``ar``/``en`` primary tag of the HTTP ``Accept-Language`` header (by preference), else ``default``."""
    value = (meta or {}).get(gmeta.LOCALE)
    if value in LOCALES:
        return value
    return _accept_language_locale(accept_language) or default


def _accept_language_locale(header: Optional[str]) -> Optional[str]:
    """``ar`` or ``en`` from an ``Accept-Language`` header (``ar-SA,en;q=0.8`` -> ``ar``), or None."""
    ranges = []
    for position, part in enumerate((header or "").split(",")[:20]):
        tag, _, params = part.strip().partition(";")
        weight = 1.0
        if params.strip().startswith("q="):
            try:
                weight = float(params.strip()[2:])
            except ValueError:
                continue
        primary = tag.strip().split("-")[0].lower()
        if primary in LOCALES and weight > 0:
            ranges.append((-weight, position, primary))
    return min(ranges)[2] if ranges else None


@dataclass
class CallContext:
    env: Any  # Odoo environment, injected by the controller layer
    user_id: int
    client_id: int
    locale: str
    meta: dict = field(default_factory=dict)
    request_id: Optional[Union[str, int]] = None
    # Wraps each tool call (e.g. ``cr.savepoint``) so a failing call leaves no partial writes.
    savepoint: Optional[Callable[[], ContextManager]] = None
    # The spec of the tool being called, set before dispatch so ``on_call`` can log it after any error.
    tool_spec: Any = None
    # True for errors the host should replay the whole request for (e.g. a Postgres serialization failure):
    # ``McpServer`` re-raises them, after the call's savepoint rolled back, instead of reporting an error.
    is_retryable: Optional[Callable[[BaseException], bool]] = None

    @classmethod
    def build(cls, env, user_id, client_id, meta, request_id, default_locale, accept_language=None):
        meta = dict(meta or {})
        locale = resolve_locale(meta, default_locale, accept_language)
        return cls(env, user_id, client_id, locale, meta, request_id)
