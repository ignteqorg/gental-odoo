"""Bearer resolution for /mcp: OAuth access tokens (``gmat_``) and API keys (``gmcp_``)."""
from dataclasses import dataclass
from typing import Any, Optional

from ..core.urls import normalize_url


@dataclass
class AuthResult:
    user_id: int
    client: Any  # gental.mcp.client record
    via: str  # "api_key" | "oauth"


def bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    """The token of an ``Authorization: Bearer <token>`` header, or None."""
    scheme, _, token = (authorization_header or "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def resolve_bearer(env, authorization_header: Optional[str]) -> Optional[AuthResult]:
    """The user and client behind the bearer, or None when it is missing, unknown or not accepted."""
    token = bearer_token(authorization_header)
    if not token:
        return None
    config = env["gental.mcp.config"].get()
    result = None
    if token.startswith("gmat_") and config.oauth_enabled:
        access = env["gental.mcp.token"].find_access(token)
        # Tokens are bound to this server's MCP URL (RFC 8707); one minted for another resource is refused.
        if access and normalize_url(access.resource) == config.mcp_url():
            result = AuthResult(access.user_id.id, access.client_id, "oauth")
    elif token.startswith("gmcp_") and config.api_keys_enabled:
        client = env["gental.mcp.client"].find_by_api_key(token)
        if client:
            result = AuthResult(client.user_id.id, client, "api_key")
    if result:
        result.client.touch_last_used()
    return result


def known_secret_owner(env, authorization_header: Optional[str]):
    """``(client_id, user_id)`` of a bearer this database issued that is no longer accepted (revoked,
    expired, bound elsewhere), or None for anything else."""
    token = bearer_token(authorization_header)
    if not token:
        return None
    if token.startswith("gmat_"):
        access = env["gental.mcp.token"].is_known_access(token)
        return (access.client_id.id, access.user_id.id) if access else None
    if token.startswith("gmcp_"):
        client = env["gental.mcp.client"].is_known_api_key(token)
        return (client.id, client.user_id.id) if client else None
    return None


def is_known_secret(env, authorization_header: Optional[str]) -> bool:
    """True when the bearer is a token or key this database issued (e.g. since revoked or expired)."""
    return known_secret_owner(env, authorization_header) is not None

