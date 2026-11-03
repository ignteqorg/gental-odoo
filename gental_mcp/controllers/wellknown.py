"""OAuth discovery documents: protected resource metadata (RFC 9728) and authorization server metadata (RFC 8414)."""
from odoo import http
from odoo.http import request

RESOURCE_NAME = "Gental MCP (Odoo)"


def json_response(data, status=200, headers=None):
    return request.make_json_response(
        data, status=status, headers=[("Cache-Control", "no-store"), *(headers or [])]
    )


def issuer(cfg):
    return cfg.base_url()


class GentalMcpWellKnown(http.Controller):
    @http.route(
        ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"],
        type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False, cors="*",
    )
    def protected_resource(self, **_kw):
        cfg = request.env["gental.mcp.config"].get()
        if not (cfg.enabled and cfg.oauth_enabled):  # clients then fall back to an API key
            return json_response({"error": "not_found"}, status=404)
        return json_response({
            "resource": cfg.mcp_url(),
            "authorization_servers": [issuer(cfg)],
            "bearer_methods_supported": ["header"],
            "resource_name": RESOURCE_NAME,
        })

    @http.route(
        "/.well-known/oauth-authorization-server",
        type="http", auth="public", methods=["GET", "OPTIONS"], csrf=False, save_session=False, cors="*",
    )
    def authorization_server(self, **_kw):
        cfg = request.env["gental.mcp.config"].get()
        if not (cfg.enabled and cfg.oauth_enabled):
            return json_response({"error": "not_found"}, status=404)
        base = issuer(cfg)
        doc = {
            "issuer": base,
            "authorization_endpoint": f"{base}/mcp/oauth/authorize",
            "token_endpoint": f"{base}/mcp/oauth/token",
            "registration_endpoint": f"{base}/mcp/oauth/register",
            "revocation_endpoint": f"{base}/mcp/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
        }
        if not cfg.dcr_enabled:
            del doc["registration_endpoint"]
        return json_response(doc)
