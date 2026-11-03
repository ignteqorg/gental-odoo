import inspect

from odoo.tests import HttpCase
from odoo.tests.common import TransactionCase

try:
    from odoo.tests.common import freeze_time  # 18.0+: Odoo's wrapper around freezegun
except ImportError:  # 17.0
    from freezegun import freeze_time  # noqa: F401 - re-exported for the tests

from odoo.addons.gental_mcp.compat import set_param, user_groups_field
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.schema import schema_issues
from odoo.addons.gental_mcp.services import registry


def use_base_url(env, url):
    """Serve MCP at ``url`` through ``web.base.url``, with no Public URL override (an override must be
    https; test servers are plain http on 127.0.0.1)."""
    set_param(env["ir.config_parameter"].sudo(), "web.base.url", url)
    env["gental.mcp.config"].get().public_url = False


def tracking_text(messages):
    """What the tracking of ``messages`` shows: field names, labels and new values from ``tracking_value_ids``
    up to 19.0; from 20.0 tracking is rendered into the message body (``→ <new> (<field label>)``) only."""
    if "tracking_value_ids" not in messages._fields:  # 20.0+
        return str(messages.mapped("body"))
    values = messages.tracking_value_ids
    return str(values.mapped("field_id.name") + values.mapped("field_id.field_description")
               + values.mapped("new_value_char"))


def hide_records(env, model, domain, name="gmcp hide records"):
    """Hide the records of ``model`` outside ``domain`` (a domain string) from every user but the superuser.

    A global ``ir.rule`` up to 19.0; from 20.0 record rules and ACLs are one model, ``ir.access``, where a row
    with no group is a restriction that applies to everyone.
    """
    model_id = env["ir.model"]._get_id(model)
    if "ir.access" in env:  # 20.0+
        return env["ir.access"].create({"name": name, "model_id": model_id, "operation": "crud", "domain": domain})
    return env["ir.rule"].create({"name": name, "model_id": model_id, "domain_force": domain})


class PortableHttpCase(HttpCase):
    """``HttpCase`` whose ``url_open`` takes 19.0's keywords (``method``, ``json``, ``params``, ``cookies``) on
    17.0 and 18.0 too, built the way 19.0 does it. On 19.0 and later Odoo's own ``url_open`` is used."""

    if "method" not in inspect.signature(HttpCase.url_open).parameters:
        def url_open(self, url, data=None, files=None, timeout=12, headers=None, json=None, params=None,
                     allow_redirects=True, cookies=None, method=None, head=False):
            if head:
                method = "HEAD"
            if not method and (data or files or json):
                method = "POST"
            method = method or "GET"
            if url.startswith("/"):
                url = self.base_url() + url
            return self.opener.request(method, url, params=params, data=data, json=json, files=files,
                                       timeout=timeout, headers=headers, cookies=cookies,
                                       allow_redirects=allow_redirects and method != "HEAD")


class GentalMcpCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = cls._make_user(
            "gmcp_agent", "MCP Agent", ["base.group_user", "gental_mcp.group_mcp_connect"]
        )
        cls.manager_user = cls._make_user(
            "gmcp_manager", "MCP Manager", ["base.group_user", "gental_mcp.group_mcp_manager"]
        )

    @classmethod
    def _make_user(cls, login, name, group_xmlids):
        field = user_groups_field(cls.env)
        return cls.env["res.users"].with_context(no_reset_password=True).create({
            "login": login,
            "name": name,
            "email": f"{login}@example.com",  # 17.0/18.0 message_post refuses an author without one
            field: [(6, 0, [cls.env.ref(x).id for x in group_xmlids])],
        })

    def assert_tool_schemas_portable(self):
        """Every tool listed with everything switched on (all generic operations, a method, curated tools
        including customer ones) has an input schema strict LLM APIs accept: typed nodes, arrays with items."""
        self.env["gental.mcp.config"].get().set_identity_secret("s" * 40)
        Exposed = self.env["gental.mcp.exposed.model"].sudo().with_context(active_test=False)
        row = Exposed.search([("model", "=", "res.partner")], limit=1) or Exposed.create(
            {"model_id": self.env["ir.model"]._get_id("res.partner")}
        )
        row.write({"active": True, "can_read": True, "can_create": True, "can_write": True, "can_unlink": True})
        if "address_get" not in row.method_ids.mapped("name"):
            self.env["gental.mcp.exposed.method"].sudo().create(
                {"exposed_model_id": row.id, "name": "address_get", "kind": "read"}
            )
        admin = self.env.ref("base.user_admin")
        env = self.env(user=admin)
        ctx = CallContext.build(env, admin.id, 0, {}, 1, "en")
        resp = registry.build_server(env).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, ctx)
        tools = resp["result"]["tools"]
        self.assertIn("odoo_search", {t["name"] for t in tools})
        self.assertIn("odoo_call_method", {t["name"] for t in tools})
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(schema_issues(tool["inputSchema"]), [])
        return tools

    def make_api_key(self, user, name="test"):
        """Create an API key client for ``user``; returns the plaintext key."""
        _client, plaintext = self.env["gental.mcp.client"].create_api_key(name, user)
        return plaintext
