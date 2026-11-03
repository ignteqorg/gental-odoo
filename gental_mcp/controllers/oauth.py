"""OAuth endpoints of the MCP authorization server.

Dynamic Client Registration (RFC 7591), the authorization endpoint with Odoo login and a consent page,
the token endpoint (code exchange with PKCE, refresh rotation) and revocation (RFC 7009).
"""
import json
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from werkzeug.exceptions import RequestEntityTooLarge

from odoo import _, http
from odoo.exceptions import AccessDenied
from odoo.http import request

from ..models.mcp_client import CLIENT_NAME_MAX, check_redirect_uris
from ..core.urls import normalize_url
from ..services import walls
from ..services.cimd import resolve_client
from .wellknown import issuer, json_response

_logger = logging.getLogger(__name__)

IP_RATE_LIMIT = 20
MAX_REGISTRATION_BYTES = 65536
GRANT_TYPES = ["authorization_code", "refresh_token"]
RESPONSE_TYPES = ["code"]
AUTHORIZE_PARAMS = (
    "response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "resource", "state",
    "scope",
)
CODE_CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url SHA-256, unpadded (RFC 7636 §4.2)
PAGE_HEADERS = [
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("Referrer-Policy", "no-referrer"),
]


def oauth_error(error, description, status=400, headers=None):
    return json_response({"error": error, "error_description": description}, status=status, headers=headers)


def ip_allowed(scope):
    """Count one request from the caller's IP for ``scope``; False once over 20 a minute.

    The first refusal of a minute per IP and scope is written to the Inbox as ``rate_limited``.
    """
    ip = request.httprequest.remote_addr or "unknown"
    if request.env["gental.mcp.rate"].hit(f"ip:{scope}:{ip}", IP_RATE_LIMIT):
        return True
    log_auth_event(f"rate:{scope}:{ip}", kind="rate_limited", result_text=f"oauth {scope}")
    return False


def log_auth_event(throttle_key=None, **vals):
    """Write an Inbox entry for an OAuth event (``kind`` defaults to ``auth``); never raises.

    With ``throttle_key``, at most one entry a minute per key (events a caller can repeat at will).
    """
    vals = dict({"kind": "auth", "ip": request.httprequest.remote_addr}, **vals)
    Log = request.env["gental.mcp.log"]
    try:
        if throttle_key:
            Log.record_throttled(throttle_key, **vals)
        else:
            Log.record_call(**vals)
    except Exception:  # noqa: BLE001 - logging must never break the OAuth flow
        _logger.exception("Could not write the Inbox entry %s", vals.get("kind"))


def read_body(limit):
    """The request body, or None when it is larger than ``limit`` bytes.

    Content-Length is checked first. A chunked body is read through the input stream bounded by the
    route's ``max_content_length`` (``limit + 1``), so at most one byte past the limit is ever read.
    """
    length = request.httprequest.content_length
    if length is not None and length > limit:
        return None
    try:
        raw = request.httprequest.get_data(cache=False)
    except RequestEntityTooLarge:
        return None
    return None if len(raw) > limit else raw


class GentalMcpOAuth(http.Controller):
    @http.route(
        "/mcp/oauth/register", type="http", auth="public", methods=["POST", "OPTIONS"],
        csrf=False, save_session=False, cors="*", max_content_length=MAX_REGISTRATION_BYTES + 1,
    )
    def register(self, **_kw):
        cfg = request.env["gental.mcp.config"].get()
        if not (cfg.enabled and cfg.oauth_enabled and cfg.dcr_enabled):
            return oauth_error("access_denied", "Dynamic client registration is disabled.", status=403)
        if not ip_allowed("register"):
            return oauth_error(
                "slow_down", "Too many registrations; try again in a minute.", status=429,
                headers=[("Retry-After", "60")],
            )
        raw = read_body(MAX_REGISTRATION_BYTES)
        if raw is None:
            return oauth_error("invalid_client_metadata", "Registration request is too large.", status=413)
        try:
            meta = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            meta = None
        if not isinstance(meta, dict):
            return oauth_error("invalid_client_metadata", "Body must be a JSON object.")
        try:
            redirect_uris = check_redirect_uris(meta.get("redirect_uris"))
        except ValueError as exc:
            return oauth_error("invalid_redirect_uri", str(exc))
        name = meta.get("client_name", "")
        if not isinstance(name, str) or len(name) > CLIENT_NAME_MAX:
            return oauth_error(
                "invalid_client_metadata", f"client_name must be text of at most {CLIENT_NAME_MAX} characters."
            )
        # Public clients only. A requested token_endpoint_auth_method, grant_types or response_types is
        # replaced by what this server supports (RFC 7591 section 3.2.1); no client secret is issued.
        client = request.env["gental.mcp.client"].register_dynamic(
            name.strip() or _("MCP client"), redirect_uris
        )
        return json_response({
            "client_id": client.client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": client.name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": GRANT_TYPES,
            "response_types": RESPONSE_TYPES,
        }, status=201)


class OAuthError(Exception):
    """An RFC 6749 error answered as JSON by the token endpoint."""

    def __init__(self, error, description, client=None):
        super().__init__(description)
        self.error = error
        self.description = description
        self.client = client  # the known client, for the Inbox


class ErrorPage(Exception):
    """An authorization request that must not be redirected (RFC 6749 §4.1.2.1): show a page instead."""

    def __init__(self, reason, status=400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


class RedirectError(Exception):
    """An authorization request error sent back to the client's (already verified) redirect URI."""

    def __init__(self, error, description, params, iss):
        super().__init__(description)
        self.error = error
        self.description = description
        self.redirect_uri = params["redirect_uri"]
        self.state = params["state"]
        self.iss = iss


def single_params(multidict, names):
    """``{name: value}`` of ``names`` (missing ones as ""), and the names sent more than once."""
    values = {name: multidict.get(name) or "" for name in names}
    repeated = {name for name in names if len(multidict.getlist(name)) > 1}
    return values, repeated


def redirect_to(uri, params):
    """A redirect to ``uri`` with ``params`` added to its query (empty values left out)."""
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v]
    response = request.redirect(urlunsplit(parts._replace(query=urlencode(query))), code=302, local=False)
    response.headers["Cache-Control"] = "no-store"
    return response


def render_page(template, values, status=200):
    lang = request.env.lang or "en_US"
    direction = request.env["res.lang"].search([("code", "=", lang)], limit=1).direction or "ltr"
    values = dict(values, html_lang=lang.replace("_", "-"), direction=direction)
    return request.render(template, values, status=status, headers=PAGE_HEADERS)


def host_of(url):
    return urlsplit(url).netloc


class GentalMcpAuthorize(http.Controller):
    """The authorization endpoint: Odoo login (``auth="user"``, so Odoo 2FA applies), then consent."""

    @http.route("/mcp/oauth/authorize", type="http", auth="user", methods=["GET"], save_session=True)
    def authorize(self, **_kw):
        """Render the consent page. Request errors are redirected to the client only once it is trusted:
        a dynamically registered client nobody approved yet gets an error page instead."""
        if not ip_allowed("authorize"):
            return self._error_page(ErrorPage("rate_limited", 429))
        try:
            _cfg, client, params = self._check_request(request.httprequest.args, fetch=True)
        except ErrorPage as page:
            return self._error_page(page)
        except RedirectError as err:
            return self._redirect_error(err)
        return render_page("gental_mcp.oauth_consent", {
            "params": params,
            "client_name": client.name,
            "redirect_host": host_of(params["redirect_uri"]),
            "metadata_host": host_of(client.client_id) if client.registration == "cimd" else "",
            "user_name": request.env.user.name,
            "exposed": request.env["gental.mcp.exposed.model"].consent_summary(),
        })

    @http.route("/mcp/oauth/authorize", type="http", auth="user", methods=["POST"], save_session=True)
    def authorize_decision(self, **_kw):
        """The consent form (CSRF-checked by Odoo): approve issues a code, deny answers ``access_denied``."""
        if not ip_allowed("authorize_post"):
            return self._error_page(ErrorPage("rate_limited", 429))
        form = request.httprequest.form
        try:
            cfg, client, params = self._check_request(form, fetch=False)
        except ErrorPage as page:
            return self._error_page(page)
        except RedirectError as err:
            return self._redirect_error(err)
        user = request.env.user
        approved = form.get("decision") == "approve"
        # The client was stored by the GET that rendered this form, so its id is committed.
        log_auth_event(status="ok" if approved else "denied", client_id=client.id, user_id=user.id,
                       result_text="approved" if approved else "denied")
        base = {"state": params["state"], "iss": issuer(cfg)}
        if not approved:
            return redirect_to(params["redirect_uri"], {"error": "access_denied", **base})
        code = request.env["gental.mcp.auth.code"].issue(
            client, user, params["redirect_uri"], params["code_challenge"], params["resource"]
        )
        return redirect_to(params["redirect_uri"], {"code": code, **base})

    def _check_request(self, source, fetch):
        """Validate an authorization request; returns ``(config, client, params)``.

        Raises ``ErrorPage`` while the client or its redirect URI is not trusted yet, ``RedirectError`` after.
        ``fetch`` lets a metadata-document client be fetched (the GET); the decision POST only accepts a
        client that is already stored.
        """
        cfg = request.env["gental.mcp.config"].get()
        if not (cfg.enabled and cfg.oauth_enabled):
            raise ErrorPage("disabled", 404)
        params, repeated = single_params(source, AUTHORIZE_PARAMS)
        if "client_id" in repeated:
            raise ErrorPage("invalid_client")
        if fetch:
            try:
                client = resolve_client(request.env, params["client_id"])
            except ValueError:
                raise ErrorPage("invalid_client") from None
        else:
            client = request.env["gental.mcp.client"].find_oauth_client(params["client_id"])
            if not client:
                raise ErrorPage("invalid_client")
        if "redirect_uri" in repeated or params["redirect_uri"] not in client.redirect_uri_list():
            raise ErrorPage("invalid_redirect")
        try:
            walls.check_connect(request.env, request.env.user)
        except walls.McpForbidden:
            raise ErrorPage("not_allowed", 403) from None
        error = None
        if repeated:
            error = ("invalid_request", "Parameters must not be repeated.")
        elif params["response_type"] != "code":
            error = ("unsupported_response_type", "Only response_type=code is supported.")
        elif params["code_challenge_method"] != "S256" or not CODE_CHALLENGE_RE.fullmatch(params["code_challenge"]):
            error = ("invalid_request", "PKCE with code_challenge_method=S256 is required.")
        elif params["resource"] and normalize_url(params["resource"]) != cfg.mcp_url():
            error = ("invalid_target", "resource must be this server's MCP URL.")
        if error:
            if not client.may_receive_error_redirects():
                raise ErrorPage("invalid_request")
            raise RedirectError(*error, params, issuer(cfg))
        # RFC 8707: a missing resource means this server; the code is bound to the canonical URL.
        params["resource"] = cfg.mcp_url()
        return cfg, client, params

    def _redirect_error(self, err):
        return redirect_to(err.redirect_uri, {
            "error": err.error, "error_description": err.description, "state": err.state, "iss": err.iss,
        })

    def _error_page(self, page):
        return render_page("gental_mcp.oauth_error", {"reason": page.reason}, status=page.status)


class GentalMcpToken(http.Controller):
    @http.route(
        "/mcp/oauth/token", type="http", auth="public", methods=["POST", "OPTIONS"],
        csrf=False, save_session=False, cors="*",
    )
    def token(self, **_kw):
        """Token endpoint. Every failure is answered, never raised: a code burnt or a token family revoked
        while checking the request must be committed with the response."""
        cfg = request.env["gental.mcp.config"].get()
        if not (cfg.enabled and cfg.oauth_enabled):
            return oauth_error("invalid_request", "OAuth is switched off on this server.")
        if not ip_allowed("token"):
            return oauth_error(
                "slow_down", "Too many token requests; try again in a minute.", status=429,
                headers=[("Retry-After", "60")],
            )
        grant_type = None
        try:
            if request.httprequest.mimetype != "application/x-www-form-urlencoded":
                raise OAuthError("invalid_request", "Use application/x-www-form-urlencoded.")
            form = request.httprequest.form
            grant_type = form.get("grant_type")
            if grant_type == "authorization_code":
                body = self._grant_code(cfg, form)
            elif grant_type == "refresh_token":
                body = self._grant_refresh(cfg, form)
            else:
                raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token.")
        except OAuthError as err:
            if err.error in ("invalid_grant", "invalid_target") and err.client:
                log_auth_event(f"grant:{err.client.id}", status="denied", client_id=err.client.id,
                               result_text=f"{grant_type}: {err.error}")
            return oauth_error(err.error, err.description)
        user_id = body.pop("user_id")
        log_auth_event(status="ok", client_id=body.pop("client_id"), user_id=user_id,
                       result_text="token issued (code exchange)" if grant_type == "authorization_code"
                       else "token refreshed")
        return json_response(body)

    def _form(self, form, names):
        values, repeated = single_params(form, names)
        if repeated:
            raise OAuthError("invalid_request", "Parameters must not be repeated.")
        return values

    def _client(self, client_id):
        client = request.env["gental.mcp.client"].find_oauth_client(client_id) if client_id else None
        if not client:
            raise OAuthError("invalid_client", "Unknown client_id.")
        return client

    def _grant_code(self, cfg, form):
        p = self._form(form, ("code", "redirect_uri", "client_id", "code_verifier", "resource"))
        if not (p["code"] and p["redirect_uri"] and p["client_id"] and p["code_verifier"]):
            raise OAuthError("invalid_request", "code, redirect_uri, client_id and code_verifier are required.")
        client = self._client(p["client_id"])
        try:
            code = request.env["gental.mcp.auth.code"].consume(p["code"], client, p["redirect_uri"], p["code_verifier"])
        except AccessDenied:
            raise OAuthError("invalid_grant", "The code is invalid, expired or already used.", client) from None
        resource = cfg.mcp_url()
        if normalize_url(code.resource) != resource or (p["resource"] and normalize_url(p["resource"]) != resource):
            raise OAuthError("invalid_target", "resource must be this server's MCP URL.", client)
        try:
            walls.check_connect(request.env, code.user_id)
        except walls.McpForbidden:
            raise OAuthError("invalid_grant", "The user may no longer connect.", client) from None
        body = request.env["gental.mcp.token"].issue_pair(client, code.user_id, code.resource)
        return dict(body, client_id=client.id, user_id=code.user_id.id)

    def _grant_refresh(self, cfg, form):
        p = self._form(form, ("refresh_token", "client_id", "resource"))
        if not (p["refresh_token"] and p["client_id"]):
            raise OAuthError("invalid_request", "refresh_token and client_id are required.")
        client = self._client(p["client_id"])
        if p["resource"] and normalize_url(p["resource"]) != cfg.mcp_url():
            raise OAuthError("invalid_target", "resource must be this server's MCP URL.", client)
        try:
            body = request.env["gental.mcp.token"].rotate_refresh(p["refresh_token"], client, cfg.mcp_url())
        except AccessDenied:
            raise OAuthError("invalid_grant", "The refresh token is invalid, expired or revoked.", client) from None
        return dict(body, client_id=client.id)

    @http.route(
        "/mcp/oauth/revoke", type="http", auth="public", methods=["POST", "OPTIONS"],
        csrf=False, save_session=False, cors="*",
    )
    def revoke(self, **_kw):
        """RFC 7009: 200 whether or not the token was known (429 over the per-IP limit)."""
        cfg = request.env["gental.mcp.config"].get()
        if not ip_allowed("revoke"):
            return oauth_error(
                "slow_down", "Too many revocation requests; try again in a minute.", status=429,
                headers=[("Retry-After", "60")],
            )
        form = request.httprequest.form
        token = form.get("token")
        if token and cfg.enabled and cfg.oauth_enabled:
            revoked = request.env["gental.mcp.token"].revoke_token(token, form.get("client_id") or None)
            if revoked:
                log_auth_event(status="ok", client_id=revoked.client_id.id, user_id=revoked.user_id.id,
                               result_text="token revoked")
        return request.make_response("", headers=[("Cache-Control", "no-store")], status=200)
