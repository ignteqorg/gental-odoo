"""Per-call context handed to tool providers. Core never imports odoo."""
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional, Union

from . import meta as gmeta

LOCALES = ("ar", "en")
# Request locale -> Odoo language code of this module's translations.
ODOO_LANGS = {"ar": "ar_001", "en": "en_US"}


def resolve_locale(meta: dict, default: str) -> str:
    value = (meta or {}).get(gmeta.LOCALE)
    return value if value in LOCALES else default


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
    def build(cls, env, user_id, client_id, meta, request_id, default_locale):
        meta = dict(meta or {})
        return cls(env, user_id, client_id, resolve_locale(meta, default_locale), meta, request_id)
