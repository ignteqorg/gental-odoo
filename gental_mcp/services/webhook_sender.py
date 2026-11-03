"""Signing and sending one webhook delivery (spec §10).

The body is sent as the exact bytes that were signed: the stored JSON text, UTF-8. The URL goes through
``net_guard.safe_post`` on every send, so a host that now resolves to a private address is refused even if
it passed when the subscription was saved. Redirects are never followed: a 3xx is a failure.

The receiver's answer is untrusted: only a short, single-line excerpt is kept, as plain text. Neither the
secret nor the body is ever logged.
"""
import time

from ..core.crypto import hmac_hex
from . import net_guard

# Delays after attempts 1 to 7; the 8th failure is final (about 22 h 21 min in all).
BACKOFF_SECONDS = (60, 300, 900, 3600, 10800, 21600, 43200)
MAX_ATTEMPTS = 8
TIMEOUT_SECONDS = 10
# ``last_error`` is a 500-character field: the status line plus an excerpt of the answer fit in it.
ERROR_MAX_CHARS = 500


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Lowercase hex HMAC-SHA256 of ``"{timestamp}." + body`` keyed with ``secret``."""
    return hmac_hex(secret, f"{timestamp}.".encode() + body)


def headers_for(delivery, timestamp: int, body: bytes) -> dict:
    """The request headers, exactly as §10 lists them."""
    return {
        "Content-Type": "application/json",
        "X-Gental-Event": delivery.event_code,
        "X-Gental-Delivery": delivery.delivery_uuid,
        "X-Gental-Timestamp": str(timestamp),
        "X-Gental-Signature": sign(delivery.subscription_id.secret, timestamp, body),
    }


def _excerpt(content: bytes) -> str:
    """The start of an answer as one line of printable text (it is shown in the Outbox, never trusted)."""
    text = content[:ERROR_MAX_CHARS * 4].decode("utf-8", errors="replace")
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def _http_error(status: int, content: bytes) -> str:
    excerpt = _excerpt(content or b"")
    message = f"HTTP {status}: {excerpt}" if excerpt else f"HTTP {status}"
    return message[:ERROR_MAX_CHARS]


def send(delivery) -> tuple[bool, int | None, str | None]:
    """POST one delivery; return ``(ok, status, error)``. Any 2xx is a success.

    ``delivery`` must be readable with the subscription's secret (the dispatcher passes it as sudo).
    """
    if not delivery.subscription_id.secret:
        return False, None, "no signing secret"
    body = (delivery.body or "").encode("utf-8")
    timestamp = int(time.time())
    try:
        resp = net_guard.safe_post(
            delivery.subscription_id.url, body, headers_for(delivery, timestamp, body), timeout=TIMEOUT_SECONDS,
        )
    except ValueError as exc:  # refused URL, timeout or no answer: net_guard's message is generic
        return False, None, str(exc)
    status = resp.status_code
    if 200 <= status < 300:
        return True, status, None
    if 300 <= status < 400:
        return False, status, f"redirect not followed (HTTP {status})"
    return False, status, _http_error(status, resp.content)


def next_delay(attempts: int) -> int:
    """Seconds to wait after the ``attempts``-th failure (1-based)."""
    return BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]
