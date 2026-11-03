"""MCP method dispatch over JSON-RPC. Pure Python; no Odoo imports."""
import logging
import time
from typing import Callable, Optional

from . import jsonrpc, meta, schema
from .context import CallContext, resolve_locale
from .tools import ToolError, ToolOutcomeUnknown, ToolResult

_logger = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_NAME = "gental-odoo"


class McpServer:
    def __init__(self, providers: list, server_version: str, instructions: Optional[str] = None):
        self.providers = providers
        self.server_version = server_version
        self.instructions = instructions

    # -- tool registry -------------------------------------------------
    def _specs(self, ctx: CallContext) -> list:
        """Return [(spec, provider)], raising on duplicate tool names."""
        seen: dict = {}
        out = []
        for provider in self.providers:
            for spec in provider.list_tools(ctx):
                if spec.name in seen:
                    raise ValueError(f"Duplicate tool name: {spec.name}")
                seen[spec.name] = provider
                out.append((spec, provider))
        return out

    def spec_for(self, name: str, ctx: CallContext):
        """``(spec, provider)`` of the tool called ``name``, or None.

        A provider may also know tools it hides because they are switched off (optional
        ``hidden_spec(name, ctx)``): those are found too, so calling one is denied, not unknown.
        """
        found = next(((s, p) for s, p in self._specs(ctx) if s.name == name), None)
        if found is not None:
            return found
        for provider in self.providers:
            hidden_spec = getattr(provider, "hidden_spec", None)
            spec = hidden_spec(name, ctx) if hidden_spec is not None else None
            if spec is not None:
                return spec, provider
        return None

    # -- dispatch ------------------------------------------------------
    def handle(self, msg: dict, ctx: CallContext, on_call: Optional[Callable] = None) -> Optional[dict]:
        method = msg.get("method", "")
        if method.startswith("notifications/"):
            return None
        if "id" not in msg:
            # Only notifications may omit the id; a request without one is never dispatched.
            return jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "id is required (only notifications/* omit it)")
        msg_id = msg.get("id")
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        req_meta = params.get("_meta")
        if isinstance(req_meta, dict):
            ctx.meta.update(req_meta)
            ctx.locale = resolve_locale(ctx.meta, ctx.locale)

        if method == "initialize":
            return jsonrpc.result(msg_id, self._initialize(params))
        if method == "ping":
            return jsonrpc.result(msg_id, {})
        if method == "tools/list":
            return jsonrpc.result(msg_id, {"tools": [s.to_mcp() for s, _ in self._specs(ctx)]})
        if method == "tools/call":
            return self._call(msg_id, params, ctx, on_call)
        return jsonrpc.error(msg_id, jsonrpc.METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        out = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self.server_version},
        }
        if self.instructions:
            out["instructions"] = self.instructions
        return out

    def _call(self, msg_id, params: dict, ctx: CallContext, on_call) -> dict:
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return jsonrpc.error(msg_id, jsonrpc.INVALID_PARAMS, "Invalid params")
        found = self.spec_for(name, ctx)
        if found is None:
            return jsonrpc.error(msg_id, jsonrpc.INVALID_PARAMS, f"Unknown tool: {name}")
        spec, provider = found
        ctx.tool_spec = spec

        started = time.monotonic()
        result: Optional[ToolResult] = None
        exc: Optional[Exception] = None
        errors = schema.validate(args, spec.input_schema)
        if errors:
            result = ToolResult("Invalid arguments: " + "; ".join(errors), is_error=True)
        else:
            try:
                if ctx.savepoint is not None:
                    # Any exception rolls the tool's writes back before it is reported.
                    with ctx.savepoint():
                        result = provider.call(name, args, ctx)
                else:
                    result = provider.call(name, args, ctx)
            except ToolError as e:
                exc = e
                result = ToolResult(str(e), is_error=True)
            except ToolOutcomeUnknown as e:
                result = ToolResult(str(e), meta={meta.OUTCOME: "unknown"})
            except Exception as e:  # noqa: BLE001 - never leak a traceback to the client
                if ctx.is_retryable is not None and ctx.is_retryable(e):
                    raise  # the host replays the whole request; this attempt is not logged
                _logger.exception("Tool %s failed", name)
                exc = e
                result = ToolResult("Internal error", is_error=True)
        if on_call is not None:
            on_call(name, args, result if exc is None else None, exc, (time.monotonic() - started) * 1000.0)
        return jsonrpc.result(msg_id, result.to_mcp())
