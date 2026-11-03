"""Client ID Metadata Documents: an OAuth ``client_id`` that is an https URL describing the client."""
import json
from datetime import timedelta
from urllib.parse import urlsplit

from odoo import fields

from ..models.mcp_client import CLIENT_NAME_MAX, check_redirect_uris, is_metadata_url
from . import net_guard

CACHE_TTL = timedelta(hours=1)
FETCH_TIMEOUT = 5
MAX_DOCUMENT_BYTES = 65536


def resolve_client(env, client_id: str):
    """The active ``gental.mcp.client`` for ``client_id``; raise ``ValueError(reason)`` when there is none.

    A metadata-document client id is fetched (guarded by ``net_guard``) and upserted, at most once an hour.
    Any other client id must already be registered.
    """
    Client = env["gental.mcp.client"]
    if not is_metadata_url(client_id):
        client = Client.find_oauth_client(client_id or "")
        if not client:
            raise ValueError("unknown client_id")
        return client
    client = Client.find_oauth_client(client_id, include_revoked=True)
    if client and client.state != "active":
        raise ValueError("client is revoked")
    if client and client.registration != "cimd":  # registered by hand under a URL id: nothing to fetch
        return client
    if client and client.cimd_fetched_at and client.cimd_fetched_at > fields.Datetime.now() - CACHE_TTL:
        return client
    body = net_guard.safe_get(client_id, timeout=FETCH_TIMEOUT, max_bytes=MAX_DOCUMENT_BYTES)
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("metadata document is not JSON") from None
    if not isinstance(doc, dict):
        raise ValueError("metadata document is not a JSON object")
    if doc.get("client_id") != client_id:
        raise ValueError("metadata document client_id does not match its URL")
    # Like DCR, a declared token_endpoint_auth_method is ignored: the client is registered as public
    # (no secret, PKCE required), whatever authentication it would have offered.
    redirect_uris = check_redirect_uris(doc.get("redirect_uris"))
    name = doc.get("client_name")
    if not isinstance(name, str) or not name.strip():
        name = urlsplit(client_id).hostname
    return Client.upsert_metadata_client(client_id, name.strip()[:CLIENT_NAME_MAX], redirect_uris)
