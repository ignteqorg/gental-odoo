import json
from unittest.mock import patch

from odoo.modules.module import get_manifest
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.gental_mcp.core.tools import ToolAnnotations, ToolDenied, ToolError, ToolResult, ToolSpec
from odoo.addons.gental_mcp.services import registry

from .common import GentalMcpCase, PortableHttpCase, use_base_url

OBJECT = {"type": "object", "properties": {"password": {"type": "string"}, "q": {"type": "string"}}}


class _TestProvider:
    def __init__(self, env):
        self.env = env

    def list_tools(self, ctx):
        return [
            ToolSpec("test_whoami", "Who am I", "Returns the acting uid.", OBJECT, ToolAnnotations(read_only=True)),
            ToolSpec("test_echo", "Echo", "Echoes.", OBJECT, ToolAnnotations(read_only=False)),
            ToolSpec("test_deny", "Deny", "Always denied.", OBJECT, ToolAnnotations(read_only=True)),
            ToolSpec("test_write_fail", "Write then fail", "Creates a contact, then fails.", OBJECT,
                     ToolAnnotations(read_only=False)),
            ToolSpec("test_db_error", "DB error", "Runs a query that times out.", OBJECT,
                     ToolAnnotations(read_only=True)),
        ]

    def call(self, name, args, ctx):
        if name == "test_whoami":
            return ToolResult(str(ctx.env.uid), structured={"uid": ctx.env.uid, "self_uid": self.env.uid})
        if name == "test_deny":
            raise ToolDenied("not for you")
        if name == "test_write_fail":
            ctx.env["res.partner"].create({"name": "gmcp rollback probe"})
            ctx.env.flush_all()
            raise ToolError("failed after writing")
        if name == "test_db_error":
            ctx.env.cr.execute("SET LOCAL statement_timeout = 1")
            ctx.env.cr.execute("SELECT pg_sleep(1)")
        return ToolResult("ok")


@tagged("post_install", "-at_install", "gental_mcp")
class TestMcpEndpoint(GentalMcpCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        registry.register_provider(_TestProvider)
        cls.config = cls.env["gental.mcp.config"].get()
        use_base_url(cls.env, cls.base_url())

    @classmethod
    def tearDownClass(cls):
        registry.unregister_provider(_TestProvider)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("test", self.agent_user)

    # -- helpers ------------------------------------------------------------------

    def post(self, payload, token=None, headers=None, raw=None):
        hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token is not False:
            hdrs["Authorization"] = f"Bearer {token or self.key}"
        hdrs.update(headers or {})
        data = raw if raw is not None else json.dumps(payload)
        return self.url_open("/mcp", data=data, headers=hdrs, method="POST")

    def rpc(self, method, params=None, msg_id=1, **kw):
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        return self.post(msg, **kw)

    def call_tool(self, name, arguments=None, **kw):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, **kw)

    def logs(self, **domain):
        return self.env["gental.mcp.log"].sudo().search(
            [("client_id", "=", self.client.id)] + [(k, "=", v) for k, v in domain.items()]
        )

    # -- auth -------------------------------------------------------------------

    def test_no_token_401_with_resource_metadata(self):
        resp = self.rpc("initialize", token=False)
        self.assertEqual(resp.status_code, 401)
        self.assertIn("/.well-known/oauth-protected-resource", resp.headers["WWW-Authenticate"])
        self.assertTrue(resp.headers["WWW-Authenticate"].startswith('Bearer resource_metadata="'))
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertNotIn("data", resp.json()["error"])

    def test_api_key_initialize_ok(self):
        resp = self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["Content-Type"].startswith("application/json"))
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        result = resp.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "gental-odoo")
        self.assertEqual(result["serverInfo"]["version"], get_manifest("gental_mcp")["version"])
        self.assertTrue(self.client.last_used_at)

    def test_oauth_disabled_401_without_resource_metadata(self):
        self.config.oauth_enabled = False
        resp = self.rpc("initialize", token=False)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.rpc("ping").status_code, 200)  # API keys still work

    def test_api_keys_disabled_rejects_key(self):
        self.config.api_keys_enabled = False
        self.assertEqual(self.rpc("ping").status_code, 401)

    def test_oauth_token_ok(self):
        oauth = self.env["gental.mcp.client"].sudo().create({"name": "OAuth", "kind": "oauth", "client_id": "c1"})
        pair = self.env["gental.mcp.token"].issue_pair(oauth, self.agent_user, self.config.mcp_url())
        resp = self.call_tool("test_whoami", token=pair["access_token"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["result"]["structuredContent"]["uid"], self.agent_user.id)

    def test_token_for_other_resource_401(self):
        oauth = self.env["gental.mcp.client"].sudo().create({"name": "OAuth", "kind": "oauth", "client_id": "c2"})
        pair = self.env["gental.mcp.token"].issue_pair(oauth, self.agent_user, "https://other.example/mcp")
        self.assertEqual(self.rpc("ping", token=pair["access_token"]).status_code, 401)

    def test_revoked_token_meta_invalid_token(self):
        oauth = self.env["gental.mcp.client"].sudo().create({"name": "OAuth", "kind": "oauth", "client_id": "c3"})
        pair = self.env["gental.mcp.token"].issue_pair(oauth, self.agent_user, self.config.mcp_url())
        self.assertEqual(self.rpc("ping", token=pair["access_token"]).status_code, 200)
        oauth.action_revoke()
        resp = self.rpc("ping", msg_id=7, token=pair["access_token"])
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual(body["id"], 7)
        self.assertEqual(body["error"]["data"], {"_meta": {"ai.gental/auth": "invalid_token"}})
        # A revoked API key carries the same signal; an unknown one does not.
        self.client.action_revoke()
        self.assertEqual(self.rpc("ping").json()["error"]["data"]["_meta"]["ai.gental/auth"], "invalid_token")
        self.assertNotIn("data", self.rpc("ping", token="gmcp_unknown").json()["error"])

    def test_known_but_dead_secret_logged_denied(self):
        """Spec §5.2: a revoked key or token is a denial in the Inbox (once a minute per client); unknown
        secrets are not logged."""
        oauth = self.env["gental.mcp.client"].sudo().create({"name": "OAuth", "kind": "oauth", "client_id": "c4"})
        pair = self.env["gental.mcp.token"].issue_pair(oauth, self.agent_user, self.config.mcp_url())
        oauth.action_revoke()
        for _i in range(2):
            self.assertEqual(self.rpc("ping", token=pair["access_token"]).status_code, 401)
        rows = self.env["gental.mcp.log"].sudo().search([("client_id", "=", oauth.id)])
        self.assertEqual((len(rows), rows.kind, rows.status, rows.user_id), (1, "denied", "denied", self.agent_user))
        self.client.action_revoke()
        self.rpc("ping")
        self.assertEqual(self.logs(kind="denied").user_id, self.agent_user)
        before = self.env["gental.mcp.log"].sudo().search_count([])
        self.rpc("ping", token="gmcp_unknown")
        self.rpc("ping", token=False)
        self.assertEqual(self.env["gental.mcp.log"].sudo().search_count([]), before)

    # -- walls ------------------------------------------------------------------

    def test_server_disabled_403(self):
        self.config.enabled = False
        self.assertEqual(self.rpc("ping").status_code, 403)

    def test_user_not_in_connect_group_403(self):
        user = self._make_user("gmcp_plain", "Plain", ["base.group_user"])
        _client, key = self.env["gental.mcp.client"].create_api_key("plain", user)
        resp = self.rpc("ping", token=key)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(self.env["gental.mcp.log"].sudo().search([("user_id", "=", user.id), ("kind", "=", "denied")]))

    def test_group_change_checked_every_request(self):
        """The wall itself refuses a user who left the connect group, even when no hook revoked the key."""
        self.assertEqual(self.rpc("ping").status_code, 200)
        self.env.cr.execute(
            "DELETE FROM res_groups_users_rel WHERE uid = %s AND gid = %s",
            (self.agent_user.id, self.env.ref("gental_mcp.group_mcp_connect").id),
        )
        self.env.invalidate_all()
        self.assertEqual(self.client.state, "active")
        self.assertEqual(self.rpc("ping").status_code, 403)

    def test_bad_origin_403(self):
        self.assertEqual(self.rpc("ping", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.env["gental.mcp.origin"].sudo().create({"config_id": self.config.id, "origin": "https://claude.ai/"})
        self.assertEqual(self.rpc("ping", headers={"Origin": "https://claude.ai"}).status_code, 200)

    def test_body_too_large_413(self):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (1024 * 1024)}})
        self.assertEqual(self.post(None, raw=raw).status_code, 413)

    def test_rate_limited_429_and_logged(self):
        self.config.rate_limit_per_min = 2
        # Freeze the counter's clock so the three calls share one minute window.
        with patch("odoo.addons.gental_mcp.models.mcp_rate.time") as clock:
            clock.time.return_value = 1_800_000_000.0
            self.assertEqual(self.rpc("ping").status_code, 200)
            self.assertEqual(self.rpc("ping").status_code, 200)
            resp = self.rpc("ping")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers["Retry-After"], "60")
        self.assertEqual(len(self.logs(kind="rate_limited")), 1)

    # -- dispatch and logging ---------------------------------------------------

    def test_tool_call_logged_with_masked_args(self):
        resp = self.call_tool("test_echo", {"password": "x", "q": "hello"}, msg_id="r1")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["result"]["isError"])
        log = self.logs(kind="call", tool="test_echo")
        self.assertEqual(len(log), 1)
        self.assertEqual(json.loads(log.args_json), {"password": "•••", "q": "hello"})
        self.assertEqual((log.status, log.tool_kind, log.user_id, log.request_id), ("ok", "write", self.agent_user, "r1"))

    def test_denied_tool_logged_denied(self):
        resp = self.call_tool("test_deny")
        self.assertTrue(resp.json()["result"]["isError"])
        self.assertEqual(self.logs(kind="call", tool="test_deny").status, "denied")

    def test_failed_tool_rolls_back_its_writes(self):
        user = self._make_user("gmcp_writer", "Writer", [
            "base.group_user", "base.group_partner_manager", "gental_mcp.group_mcp_connect",
        ])
        client, key = self.env["gental.mcp.client"].create_api_key("writer", user)
        resp = self.call_tool("test_write_fail", token=key)
        self.assertTrue(resp.json()["result"]["isError"])
        self.env.invalidate_all()
        self.assertFalse(self.env["res.partner"].search([("name", "=", "gmcp rollback probe")]))
        log = self.env["gental.mcp.log"].search([("client_id", "=", client.id), ("tool", "=", "test_write_fail")])
        self.assertEqual((log.status, log.tool_kind), ("error", "write"))

    @mute_logger("odoo.sql_db", "odoo.addons.gental_mcp.core.protocol")
    def test_db_error_still_logged(self):
        resp = self.call_tool("test_db_error")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["result"]["content"][0]["text"], "Internal error")
        log = self.logs(kind="call", tool="test_db_error")
        self.assertEqual((log.status, log.tool_kind, log.result_text), ("error", "read", "Internal error"))
        self.assertEqual(self.rpc("ping").status_code, 200)

    def test_tool_runs_as_acting_user(self):
        resp = self.call_tool("test_whoami")
        structured = resp.json()["result"]["structuredContent"]
        self.assertEqual(structured["uid"], self.agent_user.id)
        self.assertEqual(structured["self_uid"], self.agent_user.id)

    def test_get_405(self):
        for method in ("GET", "DELETE"):
            resp = self.url_open("/mcp", method=method, headers={"Authorization": f"Bearer {self.key}"})
            self.assertEqual(resp.status_code, 405, method)
            self.assertEqual(resp.headers["Allow"], "POST")

    def test_notification_202(self):
        resp = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.content, b"")

    def test_request_without_id_refused_not_dispatched(self):
        resp = self.post({"jsonrpc": "2.0", "method": "tools/call",
                          "params": {"name": "test_write_fail", "arguments": {}}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], -32600)
        self.assertFalse(self.logs(tool="test_write_fail"))

    def test_trailing_slash_same_endpoint(self):
        resp = self.url_open("/mcp/", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }, allow_redirects=False)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["result"], {})

    def test_unsupported_protocol_header_400(self):
        self.assertEqual(self.rpc("ping", headers={"MCP-Protocol-Version": "2024-01-01"}).status_code, 400)
        self.assertEqual(self.rpc("ping", headers={"MCP-Protocol-Version": "2025-06-18"}).status_code, 200)

    def test_malformed_messages(self):
        self.assertEqual(self.post(None, raw="{nope").json()["error"]["code"], -32700)
        resp = self.post({"jsonrpc": "2.0", "id": 3, "method": 5})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], -32600)
        self.assertEqual(self.post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).json()["error"]["code"], -32600)
