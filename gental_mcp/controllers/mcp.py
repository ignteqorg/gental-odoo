"""The MCP endpoint: stateless Streamable HTTP, one JSON-RPC message per POST, JSON responses only."""
import json
import logging

from werkzeug.exceptions import RequestEntityTooLarge

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

from .. import compat
from ..core import jsonrpc, meta as gmeta
from ..core.context import CallContext
from ..core.protocol import SUPPORTED_VERSIONS
from ..core.tools import ToolDenied, ToolError
from ..services import auth, registry, walls

_logger = logging.getLogger(__name__)

MAX_BODY = 1024 * 1024
STATEMENT_TIMEOUT_MS = 20000
UNAUTHORIZED = -32001
RATE_LIMITED = -32002
FORBIDDEN = -32003


class McpController(http.Controller):
    @http.route(["/mcp", "/mcp/"], type="http", auth="public", methods=["GET", "POST", "DELETE"], csrf=False,
                save_session=False)
    def mcp(self, **_kw):
        if request.httprequest.method != "POST":
            return _respond(None, status=405, headers=[("Allow", "POST")])

        # (1) Body size.
        try:
            request.httprequest.max_content_length = MAX_BODY
            body = request.httprequest.get_data()
        except RequestEntityTooLarge:
            return _error(None, jsonrpc.INVALID_REQUEST, "Request body too large", status=413)
        if len(body) > MAX_BODY:
            return _error(None, jsonrpc.INVALID_REQUEST, "Request body too large", status=413)

        env = request.env
        config = env["gental.mcp.config"].get()

        # (2) Browser origin (DNS rebinding / cross-site abuse).
        origin = request.httprequest.headers.get("Origin")
        if origin and _norm_origin(origin) not in {_norm_origin(o) for o in config.allowed_origin_ids.mapped("origin")}:
            return _error(None, jsonrpc.INVALID_REQUEST, "Origin not allowed", status=403)

        version = request.httprequest.headers.get("MCP-Protocol-Version")
        if version and version not in SUPPORTED_VERSIONS:
            return _error(None, jsonrpc.INVALID_REQUEST, f"Unsupported MCP-Protocol-Version: {version}", status=400)

        # (3) Bearer.
        header = request.httprequest.headers.get("Authorization")
        who = auth.resolve_bearer(env, header)
        if who is None:
            known = auth.known_secret_owner(env, header)
            data = None
            if known is not None:
                data = {"_meta": {gmeta.AUTH: "invalid_token"}}
                client_id, user_id = known
                _log_throttled(env, f"invalid_token:{client_id}", kind="denied", status="denied",
                               client_id=client_id, user_id=user_id, ip=request.httprequest.remote_addr,
                               result_text="invalid_token: revoked, expired or for another server")
            challenge = "Bearer"
            if config.oauth_enabled:  # the metadata page only exists while OAuth is on
                base = config.base_url() or request.httprequest.host_url.rstrip("/")
                challenge = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
            return _error(_peek_id(body), UNAUTHORIZED, "Unauthorized", data=data, status=401,
                          headers=[("WWW-Authenticate", challenge)])
        client = who.client
        user = env["res.users"].browse(who.user_id)
        ip = request.httprequest.remote_addr

        # (4) Connect wall, on every request.
        try:
            walls.check_connect(env, user)
        except walls.McpForbidden as e:
            _log(env, kind="denied", status="denied", client_id=client.id, user_id=who.user_id, ip=ip,
                 result_text=str(e))
            return _error(_peek_id(body), FORBIDDEN, "Forbidden", status=403)

        # (5) Rate limit per client.
        if not env["gental.mcp.rate"].hit(f"client:{client.id}", config.rate_limit_per_min):
            _log(env, kind="rate_limited", client_id=client.id, user_id=who.user_id, ip=ip)
            return _error(_peek_id(body), RATE_LIMITED, "Rate limit exceeded", status=429,
                          headers=[("Retry-After", "60")])

        try:
            msg = jsonrpc.parse(body)
        except jsonrpc.JsonRpcError as e:
            return _error(e.id, e.code, e.message, status=400)

        # (6) Act as the resolved user, with a bounded statement time.
        request.update_env(user=who.user_id)
        env = request.env
        env.cr.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")

        # (7) Dispatch. Each tool call runs in a savepoint, so a failure leaves no partial writes.
        msg_id = msg.get("id")
        ctx = CallContext.build(env, who.user_id, client.id, None, msg_id, config.default_lang,
                                request.httprequest.headers.get("Accept-Language"))
        ctx.savepoint = env.cr.savepoint
        # Odoo serves every route through its ``retrying`` loop: a concurrency error that escapes
        # replays the whole request with a fresh snapshot. On the last attempt nothing escapes any more.
        attempt = getattr(request, "_gental_mcp_attempt", 0) + 1
        request._gental_mcp_attempt = attempt
        ctx.is_retryable = lambda e: compat.is_concurrency_error(e) and attempt < compat.concurrency_max_tries()
        server = registry.build_server(env)

        def on_call(name, args, result, exc, duration_ms):
            try:
                _log_call(env, ctx, client.id, who.user_id, ip, name, args, result, exc, duration_ms)
            except Exception:  # noqa: BLE001 - logging must never break a call
                _logger.exception("Could not write the Inbox entry for MCP tool %s", name)

        try:
            response = server.handle(msg, ctx, on_call=on_call)
        except Exception as e:  # noqa: BLE001 - never leak a traceback to the client
            if ctx.is_retryable(e):
                raise
            _logger.exception("MCP request %s failed", msg.get("method"))
            response = jsonrpc.error(msg_id, jsonrpc.INTERNAL_ERROR, "Internal error")

        # (8) Respond; notifications get no body, a request without an id is refused.
        if response is None:
            return _respond(None, status=202)
        return _respond(response, status=200 if "id" in msg else 400)


def _norm_origin(origin):
    return (origin or "").strip().rstrip("/").lower()


def _peek_id(body):
    """Best-effort JSON-RPC id of a message not parsed yet, so early errors can echo it."""
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    msg_id = msg.get("id") if isinstance(msg, dict) else None
    return msg_id if isinstance(msg_id, (int, str)) and not isinstance(msg_id, bool) else None


def _respond(payload, status=200, headers=None):
    headers = [("Cache-Control", "no-store"), *(headers or [])]
    if payload is None:
        return request.make_response("", headers=headers, status=status)
    return request.make_json_response(payload, headers=headers, status=status)


def _error(msg_id, code, message, data=None, status=200, headers=None):
    return _respond(jsonrpc.error(msg_id, code, message, data), status=status, headers=headers)


def _log(env, **vals):
    """Write an Inbox entry; failures are logged, never raised."""
    try:
        env["gental.mcp.log"].record_call(**vals)
    except Exception:  # noqa: BLE001
        _logger.exception("Could not write the Inbox entry %s", vals.get("kind"))


def _log_throttled(env, key, **vals):
    """Write an Inbox entry at most once a minute per ``key``; failures are logged, never raised."""
    try:
        env["gental.mcp.log"].record_throttled(key, **vals)
    except Exception:  # noqa: BLE001
        _logger.exception("Could not write the Inbox entry %s", vals.get("kind"))


def _tool_kind(spec):
    if spec is None:
        return False
    ann = spec.annotations
    return "destructive" if ann.destructive else "read" if ann.read_only else "write"


def _log_call(env, ctx, client_id, user_id, ip, name, args, result, exc, duration_ms):
    if isinstance(exc, ToolDenied):
        status, text = "denied", str(exc)
    elif isinstance(exc, AccessError):  # wall 3 (ACLs, record rules) escaping a provider
        status, text = "denied", "Access denied"
    elif exc is not None:
        status, text = "error", str(exc) if isinstance(exc, ToolError) else "Internal error"
    else:
        status, text = ("error" if result.is_error else "ok"), result.text
        if not result.is_error and result.meta.get(gmeta.OUTCOME) == "unknown":
            status = "unknown"
    record = result.record if result is not None and result.record else (False, False)
    env["gental.mcp.log"].record_call(
        kind="call",
        client_id=client_id,
        user_id=user_id,
        tool=name,
        tool_kind=_tool_kind(ctx.tool_spec),
        status=status,
        duration_ms=int(duration_ms),
        args=args,
        result_text=text,
        res_model=record[0],
        res_id=record[1],
        action_key=ctx.meta.get(gmeta.ACTION_KEY) or False,
        ip=ip,
        request_id=str(ctx.request_id) if ctx.request_id is not None else False,
    )
