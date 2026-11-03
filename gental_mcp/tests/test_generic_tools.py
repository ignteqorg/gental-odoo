import base64
import json

from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.gental_mcp.controllers.mcp import _log_call
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.meta import ACTION_KEY
from odoo.addons.gental_mcp.services import domain_guard, registry
from odoo.addons.gental_mcp.services.generic_tools import (
    DEFAULT_FIELDS_MAX, KEY_FIELDS, MAX_RECORDS_TEXT_BYTES, GenericToolProvider,
)
from odoo.addons.gental_mcp.core.tools import ToolError

from .common import GentalMcpCase

GENERIC = {"odoo_describe_model", "odoo_search", "odoo_read", "odoo_create", "odoo_update", "odoo_delete",
           "odoo_call_method"}


@tagged("post_install", "-at_install", "gental_mcp")
class TestGenericTools(GentalMcpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.writer_user = cls._make_user(
            "gmcp_writer", "MCP Writer",
            ["base.group_user", "base.group_partner_manager", "gental_mcp.group_mcp_connect"],
        )
        cls.client, _key = cls.env["gental.mcp.client"].create_api_key("generic", cls.writer_user)
        cls.Exposed = cls.env["gental.mcp.exposed.model"].sudo()
        cls.Exposed.search([]).unlink()

    # -- helpers ------------------------------------------------------------------

    def expose(self, model="res.partner", methods=(), hidden=(), **vals):
        row = self.Exposed.create({"model_id": self.env["ir.model"]._get_id(model), **vals})
        for name, kind in methods:
            self.env["gental.mcp.exposed.method"].sudo().create(
                {"exposed_model_id": row.id, "name": name, "kind": kind}
            )
        if hidden:
            row.hidden_field_ids = self.env["ir.model.fields"].search(
                [("model", "=", model), ("name", "in", list(hidden))]
            )
        return row

    def server(self, user):
        return registry.build_server(self.env(user=user))

    def tools(self, user=None):
        user = user or self.writer_user
        server = self.server(user)
        ctx = CallContext.build(server_env(server), user.id, self.client.id, {}, 1, "en")
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, ctx)
        return {t["name"]: t for t in resp["result"]["tools"] if t["name"] in GENERIC}

    def call(self, name, arguments, user=None, server=None, between=None, meta=None, client=None):
        """Call a tool through the MCP server like the endpoint: schema validation, savepoint, Inbox log."""
        user = user or self.writer_user
        server = server or self.server(user)
        env = server_env(server)
        ctx = CallContext.build(env, user.id, (client or self.client).id, meta or {}, 7, "en")
        ctx.savepoint = env.cr.savepoint  # as the endpoint does
        if between:
            server._specs(ctx)  # the listing is taken before the flag changes
            between()

        def on_call(tool, args, result, exc, duration_ms):
            _log_call(env, ctx, self.client.id, user.id, "127.0.0.1", tool, args, result, exc, duration_ms)

        msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        if meta:
            msg["params"]["_meta"] = meta
        return server.handle(msg, ctx, on_call=on_call)["result"]

    def ok(self, name, arguments, **kw):
        result = self.call(name, arguments, **kw)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return result["structuredContent"], result["content"][0]["text"]

    def err(self, name, arguments, **kw):
        result = self.call(name, arguments, **kw)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    def partner(self, name, **vals):
        return self.env["res.partner"].create({"name": name, **vals})

    # -- listing ------------------------------------------------------------------

    def test_registered(self):
        self.assertIn(GenericToolProvider, registry.PROVIDERS)

    def test_no_exposed_models_no_tools(self):
        self.assertEqual(self.tools(), {})

    def test_model_enum_lists_only_exposed(self):
        self.expose("res.partner", can_read=True)
        self.expose("res.country", can_read=True, can_create=True)
        tools = self.tools()
        self.assertEqual(set(tools), {"odoo_describe_model", "odoo_search", "odoo_read", "odoo_create"})
        enum = tools["odoo_search"]["inputSchema"]["properties"]["model"]["enum"]
        self.assertEqual(enum, ["res.country", "res.partner"])
        self.assertEqual(tools["odoo_create"]["inputSchema"]["properties"]["model"]["enum"], ["res.country"])
        self.assertTrue(tools["odoo_search"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["odoo_create"]["annotations"]["readOnlyHint"])
        # A model no longer exposed (or archived) disappears from the listing.
        self.Exposed.search([("model", "=", "res.country")]).active = False
        self.assertNotIn("odoo_create", self.tools())

    def test_every_listed_tool_schema_is_portable(self):
        tools = {t["name"]: t for t in self.assert_tool_schemas_portable()}
        items = tools["odoo_search"]["inputSchema"]["properties"]["domain"]["items"]
        self.assertEqual([b["type"] for b in items["anyOf"]], ["string", "array"])

    # -- search / read --------------------------------------------------------------

    def test_default_fields_and_limit_are_small(self):
        self.expose("res.partner", can_read=True)
        self.env["res.partner"].create([
            {"name": f"GMXDEF {i:02d}", "email": f"d{i}@example.com", "comment": "long note " * 50}
            for i in range(25)
        ])
        data, _text = self.ok("odoo_search", {"model": "res.partner", "domain": [["name", "like", "GMXDEF"]],
                                              "order": "name asc"})
        self.assertEqual(len(data["records"]), 20)
        self.assertEqual(base64.urlsafe_b64decode(data["next_cursor"]).decode(), "20")
        record = data["records"][0]
        self.assertLessEqual(set(record), {"id", "display_name", *KEY_FIELDS})
        self.assertLessEqual(len(record), 2 + DEFAULT_FIELDS_MAX)
        self.assertEqual((record["name"], record["email"]), ("GMXDEF 00", "d0@example.com"))
        self.assertNotIn("comment", record)
        data, _text = self.ok("odoo_read", {"model": "res.partner", "ids": [record["id"]]})
        self.assertEqual(set(data["records"][0]), set(record))

    def test_large_results_cut_with_note(self):
        self.expose("res.partner", can_read=True)
        partners = self.env["res.partner"].create([
            {"name": f"GMXBIG {i:02d}", "comment": "x" * 2000} for i in range(30)
        ])
        args = {"model": "res.partner", "domain": [["name", "like", "GMXBIG"]], "fields": ["name", "comment"],
                "order": "name asc", "limit": 30}
        data, text = self.ok("odoo_search", args)
        kept = len(data["records"])
        self.assertTrue(0 < kept < 30)
        self.assertLess(len(text.encode()), MAX_RECORDS_TEXT_BYTES + 1024)
        self.assertIn("fewer fields", data["note"])
        self.assertEqual(base64.urlsafe_b64decode(data["next_cursor"]).decode(), str(kept))
        data2, _text = self.ok("odoo_search", dict(args, cursor=data["next_cursor"]))
        self.assertEqual(data2["records"][0]["name"], f"GMXBIG {kept:02d}")  # continues where it was cut
        data, _text = self.ok("odoo_read", {"model": "res.partner", "ids": partners.ids, "fields": ["comment"]})
        self.assertEqual(len(data["records"]) + len(data["omitted_ids"]), 30)
        self.assertEqual(data["omitted_ids"], partners.ids[len(data["records"]):])
        self.assertIn("note", data)
        # Small results carry no note.
        data, _text = self.ok("odoo_search", dict(args, fields=["name"]))
        self.assertNotIn("note", data)

    def test_search_respects_extra_domain(self):
        # ``employee``: a plain flag on every version (20.0 computes ``is_company`` from the VAT).
        self.expose("res.partner", can_read=True, extra_domain="[('employee', '=', True)]")
        company = self.partner("GMX Company", employee=True)
        self.partner("GMX Person", employee=False)
        data, _text = self.ok("odoo_search", {"model": "res.partner", "domain": [["name", "like", "GMX"]],
                                              "fields": ["name"]})
        self.assertEqual([r["id"] for r in data["records"]], [company.id])
        self.assertIsNone(data["next_cursor"])
        # Ids outside the extra domain are silently dropped by odoo_read.
        person = self.env["res.partner"].search([("name", "=", "GMX Person")])
        data, _text = self.ok("odoo_read", {"model": "res.partner", "ids": [person.id, company.id], "fields": ["name"]})
        self.assertEqual(data["records"], [{"id": company.id, "name": "GMX Company"}])

    def test_search_multi_company_record_rules(self):
        """Review Focus 1: record rules apply, nothing runs as sudo."""
        Company = self.env["res.company"].sudo()
        company_a = Company.create({"name": "GMX Co A"})
        company_b = Company.create({"name": "GMX Co B"})
        user_b = self._make_user("gmcp_b", "MCP B", ["base.group_user", "gental_mcp.group_mcp_connect"])
        user_b.write({"company_ids": [(6, 0, [company_b.id])], "company_id": company_b.id})
        p_a = self.partner("GMXMC partner A", company_id=company_a.id)
        p_b = self.partner("GMXMC partner B", company_id=company_b.id)
        self.expose("res.partner", can_read=True)
        data, _text = self.ok("odoo_search", {"model": "res.partner", "domain": [["name", "like", "GMXMC"]],
                                              "fields": ["name", "company_id"]}, user=user_b)
        self.assertEqual([r["id"] for r in data["records"]], [p_b.id])
        data, _text = self.ok("odoo_read", {"model": "res.partner", "ids": [p_a.id, p_b.id]}, user=user_b)
        self.assertEqual([r["id"] for r in data["records"]], [p_b.id])

    def test_hidden_field_not_returned_and_not_filterable(self):
        """Review Focus 2: a hidden field cannot be read, filtered, ordered or written."""
        self.expose("res.partner", can_read=True, can_write=True, hidden=["vat"])
        p = self.partner("GMX Hidden", vat="SECRETVAT")
        data, _text = self.ok("odoo_describe_model", {"model": "res.partner"})
        self.assertIn("name", data["fields"])
        self.assertNotIn("vat", data["fields"])
        data, text = self.ok("odoo_read", {"model": "res.partner", "ids": [p.id]})
        self.assertNotIn("vat", data["records"][0])
        self.assertNotIn("SECRETVAT", text)
        data, text = self.ok("odoo_search", {"model": "res.partner", "domain": [["id", "=", p.id]]})
        self.assertNotIn("vat", data["records"][0])
        self.assertNotIn("SECRETVAT", text)
        self.assertIn("vat", self.err("odoo_search", {"model": "res.partner", "domain": [["vat", "=", "SECRETVAT"]]}))
        self.err("odoo_search", {"model": "res.partner", "domain": ["!", ["vat", "=", "x"]]})
        self.err("odoo_search", {"model": "res.partner", "fields": ["vat"]})
        self.err("odoo_search", {"model": "res.partner", "order": "vat asc"})
        self.err("odoo_read", {"model": "res.partner", "ids": [p.id], "fields": ["vat"]})
        self.err("odoo_update", {"model": "res.partner", "ids": [p.id], "values": {"vat": "X"}})
        self.assertEqual(p.vat, "SECRETVAT")

    def test_hidden_field_oracles_rejected(self):
        """Fix round 1: name searches (display_name, relational strings, child_of) can match hidden vat."""
        self.expose("res.partner", can_read=True, hidden=["vat"])
        parent = self.partner("GMX Oracle Parent", is_company=True, vat="SECRETVAT")
        child = self.partner("GMX Oracle Child", parent_id=parent.id)
        for domain in ([["display_name", "=like", "SECRET%"]], [["parent_id", "ilike", "SECRETVAT"]],
                       [["commercial_partner_id", "=", "SECRETVAT"]], [["parent_id", "ilike", 123]],
                       [["id", "child_of", "SECRETVAT"]], [["id", "parent_of", ["SECRETVAT"]]],
                       [["parent_id", "in", ["SECRETVAT"]]]):
            with self.subTest(domain=domain):
                self.err("odoo_search", {"model": "res.partner", "domain": domain})
        for order in ("display_name", "parent_id desc"):
            with self.subTest(order=order):
                self.err("odoo_search", {"model": "res.partner", "order": order})
        data, _text = self.ok("odoo_search", {"model": "res.partner", "fields": ["name"],
                                              "domain": [["parent_id", "=", parent.id]]})
        self.assertEqual([r["id"] for r in data["records"]], [child.id])
        data, _text = self.ok("odoo_search", {"model": "res.partner", "fields": ["name"],
                                              "domain": [["id", "child_of", parent.id], ["parent_id", "!=", False]]})
        self.assertEqual([r["id"] for r in data["records"]], [child.id])

    def test_link_to_invisible_record_rejected(self):
        """Fix round 1: linked ids must be readable by the acting user (no cross-company links)."""
        Company = self.env["res.company"].sudo()
        company_a = Company.create({"name": "GMX Link A"})
        company_b = Company.create({"name": "GMX Link B"})
        user_b = self._make_user("gmcp_link_b", "MCP Link B", ["base.group_user", "base.group_partner_manager",
                                                                "gental_mcp.group_mcp_connect"])
        user_b.write({"company_ids": [(6, 0, [company_b.id])], "company_id": company_b.id})
        p_a = self.partner("GMX Link Partner A", company_id=company_a.id)
        p_b = self.partner("GMX Link Partner B", company_id=company_b.id)
        p_b2 = self.partner("GMX Link Partner B2", company_id=company_b.id, is_company=True)
        self.expose("res.partner", can_read=True, can_write=True, can_create=True)
        text = self.err("odoo_update", {"model": "res.partner", "ids": [p_b.id], "values": {"parent_id": p_a.id}},
                        user=user_b)
        self.assertIn(str(p_a.id), text)
        self.assertFalse(p_b.parent_id)
        self.err("odoo_create", {"model": "res.partner", "values": {"name": "GMX Link New", "parent_id": p_a.id}},
                 user=user_b)
        self.err("odoo_update", {"model": "res.partner", "ids": [p_b.id], "values": {"category_id": [999999999]}},
                 user=user_b)
        self.err("odoo_update", {"model": "res.partner", "ids": [p_b.id], "values": {"parent_id": "GMX"}},
                 user=user_b)
        self.ok("odoo_update", {"model": "res.partner", "ids": [p_b.id], "values": {"parent_id": p_b2.id}},
                user=user_b)
        self.assertEqual(p_b.parent_id, p_b2)
        self.ok("odoo_update", {"model": "res.partner", "ids": [p_b.id], "values": {"parent_id": False}},
                user=user_b)
        self.assertFalse(p_b.parent_id)

    def test_dotted_path_rejected(self):
        self.expose("res.partner", can_read=True)
        text = self.err("odoo_search", {"model": "res.partner", "domain": [["parent_id.vat", "=", "x"]]})
        self.assertIn("parent_id.vat", text)
        self.err("odoo_search", {"model": "res.partner", "fields": ["parent_id.name"]})
        self.err("odoo_search", {"model": "res.partner", "order": "parent_id.name"})
        with self.assertRaises(ToolError):
            domain_guard.validate([("parent_id.vat", "=", "x")], {"parent_id", "parent_id.vat"})

    def test_unknown_operator_rejected(self):
        self.expose("res.partner", can_read=True)
        for domain in ([["name", "any", [["vat", "=", "x"]]]], [["name", "=?", "x"]], [["name", "=", {"a": 1}]],
                       [["name", "in", [["nested"]]]], ["&", ["name", "=", "x"]], ["^", ["name", "=", "x"]],
                       [["name", "="]], [[1, "=", 1]]):
            with self.subTest(domain=domain):
                self.err("odoo_search", {"model": "res.partner", "domain": domain})
        domain_guard.validate(["|", ["name", "ilike", "a"], "!", ["id", "in", [1, 2]]], {"name", "id"})

    def test_secret_and_binary_fields_never_returned(self):
        self.expose("res.users", can_read=True)
        data, _text = self.ok("odoo_describe_model", {"model": "res.users"})
        for name in ("password", "image_1920", "avatar_128"):
            self.assertNotIn(name, data["fields"])
        self.assertNotIn("binary", {f["type"] for f in data["fields"].values()})
        data, _text = self.ok("odoo_read", {"model": "res.users", "ids": [self.writer_user.id]})
        record = data["records"][0]
        self.assertEqual(record["login"], "gmcp_writer")
        for name in ("password", "image_1920", "avatar_128"):
            self.assertNotIn(name, record)
        self.err("odoo_read", {"model": "res.users", "ids": [self.writer_user.id], "fields": ["password"]})
        self.err("odoo_search", {"model": "res.users", "domain": [["password", "!=", False]]})

    def test_only_real_fields_described(self):
        # Before 19.0, res.users.fields_get also lists the user form's in_group_* / sel_groups_* pseudo-fields.
        self.expose("res.users", can_read=True)
        data, _text = self.ok("odoo_describe_model", {"model": "res.users"})
        self.assertLessEqual(set(data["fields"]), set(self.env["res.users"]._fields))
        data, _text = self.ok("odoo_search", {"model": "res.users", "domain": [["login", "=", "gmcp_writer"]],
                                              "fields": ["login"]})
        self.assertEqual([r["login"] for r in data["records"]], ["gmcp_writer"])

    def test_limit_capped_at_200_with_cursor(self):
        self.expose("res.partner", can_read=True)
        self.env["res.partner"].create([{"name": f"GMXLIM {i:03d}"} for i in range(205)])
        args = {"model": "res.partner", "domain": [["name", "like", "GMXLIM"]], "fields": ["name"],
                "order": "name asc", "limit": 500}
        data, _text = self.ok("odoo_search", args)
        self.assertEqual(len(data["records"]), 200)
        self.assertEqual(data["records"][0]["name"], "GMXLIM 000")
        self.assertEqual(base64.urlsafe_b64decode(data["next_cursor"]).decode(), "200")
        data, _text = self.ok("odoo_search", dict(args, cursor=data["next_cursor"]))
        self.assertEqual([r["name"] for r in data["records"]], [f"GMXLIM {i}" for i in range(200, 205)])
        self.assertIsNone(data["next_cursor"])
        data, _text = self.ok("odoo_search", dict(args, limit=3))
        self.assertEqual(len(data["records"]), 3)
        self.err("odoo_search", dict(args, cursor="not-a-cursor"))
        self.err("odoo_read", {"model": "res.partner", "ids": list(range(1, 202))})

    # -- writes -------------------------------------------------------------------

    def test_create_disallowed_operation_denied_and_logged(self):
        row = self.expose("res.partner", can_read=True, can_create=True)
        result = self.call("odoo_create", {"model": "res.partner", "values": {"name": "GMX Denied"}},
                           between=lambda: row.write({"can_create": False}))
        self.assertTrue(result["isError"])
        self.assertFalse(self.env["res.partner"].search([("name", "=", "GMX Denied")]))
        log = self.env["gental.mcp.log"].sudo().search(
            [("client_id", "=", self.client.id), ("tool", "=", "odoo_create")]
        )
        self.assertEqual(log.status, "denied")
        # Once the listing catches up, the tool is gone.
        self.assertNotIn("odoo_create", self.tools())

    def test_create_returns_record_and_arabic_roundtrip(self):
        """Review Focus 5: Arabic in values and domains survives intact, no \\u escapes."""
        self.expose("res.partner", can_read=True, can_create=True)
        result = self.call("odoo_create", {"model": "res.partner", "values": {"name": "شركة النخبة", "employee": True}})
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]
        self.assertEqual(data["display_name"], "شركة النخبة")
        self.assertIn("شركة النخبة", result["content"][0]["text"])
        self.assertNotIn("\\u", result["content"][0]["text"])
        partner = self.env["res.partner"].browse(data["id"])
        self.assertEqual(partner.name, "شركة النخبة")
        data, text = self.ok("odoo_search", {"model": "res.partner", "domain": [["name", "=", "شركة النخبة"]],
                                             "fields": ["name"]})
        self.assertEqual(data["records"], [{"id": partner.id, "name": "شركة النخبة"}])
        self.assertIn("شركة النخبة", text)
        self.assertNotIn("\\u", text)
        log = self.env["gental.mcp.log"].sudo().search(
            [("client_id", "=", self.client.id), ("tool", "=", "odoo_create")]
        )
        self.assertEqual((log.res_model, log.res_id, log.status), ("res.partner", partner.id, "ok"))

    def test_generic_create_honours_action_key(self):
        self.expose("res.partner", can_read=True, can_create=True)
        call_meta = {ACTION_KEY: "gen-create-1"}
        args = {"model": "res.partner", "values": {"name": "GMX Keyed"}}
        first, _text = self.ok("odoo_create", args, meta=call_meta)
        second, _text = self.ok("odoo_create", args, meta=call_meta)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["display_name"], "GMX Keyed")
        self.assertEqual(self.env["res.partner"].search_count([("name", "=", "GMX Keyed")]), 1)

    def test_generic_create_without_key_creates_each_time(self):
        self.expose("res.partner", can_read=True, can_create=True)
        args = {"model": "res.partner", "values": {"name": "GMX Unkeyed"}}
        first, _text = self.ok("odoo_create", args)
        second, _text = self.ok("odoo_create", args)
        self.assertTrue(first["created"] and second["created"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.env["res.partner"].search_count([("name", "=", "GMX Unkeyed")]), 2)

    def test_generic_create_key_from_other_client_not_returned(self):
        self.expose("res.partner", can_read=True, can_create=True)
        other = self.env["gental.mcp.client"].create(
            {"name": "other", "kind": "api_key", "user_id": self.writer_user.id})
        call_meta = {ACTION_KEY: "gen-create-shared"}
        args = {"model": "res.partner", "values": {"name": "GMX Shared Key"}}
        first, _text = self.ok("odoo_create", args, meta=call_meta, client=other)
        text = self.err("odoo_create", args, meta=call_meta)
        self.assertIn("no longer available", text)
        self.assertNotIn(str(first["id"]), text)
        self.assertEqual(self.env["res.partner"].search_count([("name", "=", "GMX Shared Key")]), 1)

    def test_create_outside_extra_domain_denied(self):
        self.expose("res.partner", can_read=True, can_create=True, extra_domain="[('employee', '=', True)]")
        self.err("odoo_create", {"model": "res.partner", "values": {"name": "GMX Outside", "employee": False}})
        self.assertFalse(self.env["res.partner"].search([("name", "=", "GMX Outside")]))  # rolled back
        p = self.partner("GMX Inside", employee=True)
        self.Exposed.search([("model", "=", "res.partner")]).can_write = True
        self.err("odoo_update", {"model": "res.partner", "ids": [p.id], "values": {"employee": False}})
        self.assertTrue(p.employee)

    def test_update_unwritable_field_rejected(self):
        self.expose("res.partner", can_read=True, can_write=True)
        p = self.partner("GMX Update")
        self.ok("odoo_update", {"model": "res.partner", "ids": [p.id], "values": {"name": "GMX Updated"}})
        self.assertEqual(p.name, "GMX Updated")
        for values in ({"create_date": "2020-01-01 00:00:00"}, {"commercial_partner_id": p.id},
                       {"nonexistent_field": 1}, {"child_ids": []}, {"name": {"a": 1}}):
            with self.subTest(values=values):
                self.err("odoo_update", {"model": "res.partner", "ids": [p.id], "values": values})
        tools = self.tools()
        self.assertTrue(tools["odoo_update"]["annotations"]["idempotentHint"])
        self.assertFalse(tools["odoo_update"]["annotations"]["destructiveHint"])

    def test_delete_requires_unlink_flag(self):
        row = self.expose("res.partner", can_read=True, can_write=True)
        p = self.partner("GMX Delete")
        self.assertNotIn("odoo_delete", self.tools())
        row.can_unlink = True
        tools = self.tools()
        self.assertTrue(tools["odoo_delete"]["annotations"]["destructiveHint"])
        self.ok("odoo_delete", {"model": "res.partner", "ids": [p.id]})
        self.assertFalse(p.exists())
        self.err("odoo_delete", {"model": "res.partner", "ids": [p.id]})

    def test_call_method_only_allowed(self):
        self.expose("res.partner", can_read=True, methods=[("open_commercial_entity", "read"), ("address_get", "read")])
        self.expose("res.country", can_read=True)
        company = self.partner("GMX Call Co", is_company=True)
        contact = self.partner("GMX Call Contact", parent_id=company.id)
        tool = self.tools()["odoo_call_method"]
        self.assertEqual(tool["inputSchema"]["properties"]["model"]["enum"], ["res.partner"])
        self.assertEqual(tool["inputSchema"]["properties"]["method"]["enum"], ["address_get", "open_commercial_entity"])
        data, _text = self.ok("odoo_call_method", {"model": "res.partner", "ids": [contact.id],
                                                   "method": "open_commercial_entity"})
        self.assertEqual(data["result"], {"type": "ir.actions.act_window", "name": None})
        data, _text = self.ok("odoo_call_method", {"model": "res.partner", "ids": [contact.id],
                                                   "method": "address_get", "kwargs": {"adr_pref": ["contact"]}})
        self.assertEqual(data["result"], {"contact": contact.id})
        self.err("odoo_call_method", {"model": "res.partner", "ids": [contact.id], "method": "unlink"})
        self.err("odoo_call_method", {"model": "res.partner", "ids": [contact.id], "method": "address_get",
                                      "kwargs": {"bogus": 1}})
        self.assertTrue(contact.exists())

    def test_call_method_kind_annotations(self):
        row = self.expose("res.partner", can_read=True, methods=[("address_get", "read")])
        ann = self.tools()["odoo_call_method"]["annotations"]
        self.assertTrue(ann["readOnlyHint"])
        self.assertFalse(ann["destructiveHint"])
        self.env["gental.mcp.exposed.method"].sudo().create(
            {"exposed_model_id": row.id, "name": "message_subscribe", "kind": "write"}
        )
        ann = self.tools()["odoo_call_method"]["annotations"]
        self.assertFalse(ann["readOnlyHint"])
        self.assertFalse(ann["destructiveHint"])
        self.env["gental.mcp.exposed.method"].sudo().create(
            {"exposed_model_id": row.id, "name": "open_commercial_entity", "kind": "destructive"}
        )
        ann = self.tools()["odoo_call_method"]["annotations"]
        self.assertFalse(ann["readOnlyHint"])
        self.assertTrue(ann["destructiveHint"])

    def test_call_method_cannot_move_record_outside_extra_domain(self):
        """Fix round 1: records must still match the extra domain after a method; the call is rolled back."""
        # A method that exists on every version (``create_company`` is gone in 20.0): following the contact
        # takes it out of the extra domain.
        self.expose("res.partner", can_read=True, extra_domain="[('message_follower_ids', '=', False)]",
                    methods=[("message_subscribe", "write")])
        p = self.env["res.partner"].with_context(mail_create_nosubscribe=True).create({"name": "GMX Move"})
        self.assertFalse(p.message_follower_ids)
        text = self.err("odoo_call_method", {"model": "res.partner", "ids": [p.id], "method": "message_subscribe",
                                             "kwargs": {"partner_ids": [self.writer_user.partner_id.id]}})
        self.assertIn("outside the records assistants may use", text)
        p.invalidate_recordset()
        self.assertFalse(p.message_follower_ids)

    def test_call_method_model_level_only_for_api_model(self):
        self.expose("res.partner", can_read=True, methods=[("address_get", "read"), ("find_or_create", "write")])
        self.err("odoo_call_method", {"model": "res.partner", "ids": [], "method": "address_get"})
        data, _text = self.ok("odoo_call_method", {"model": "res.partner", "ids": [], "method": "find_or_create",
                                                   "kwargs": {"email": "gmx.model@example.com"}})
        partner = self.env["res.partner"].search([("email", "=", "gmx.model@example.com")])
        self.assertEqual(data["result"], [{"id": partner.id, "display_name": partner.display_name}])

    def test_call_method_rejects_undeclared_kwargs(self):
        """Keywords that would land in **kwargs are refused (methods pass them on unchecked)."""
        self.expose("res.partner", can_read=True, methods=[("message_post", "write")])
        p = self.partner("GMX Kwargs")
        text = self.err("odoo_call_method", {"model": "res.partner", "ids": [p.id], "method": "message_post",
                                             "kwargs": {"body": "hi", "author_id": 1, "bogus_key": 1}})
        self.assertIn("bogus_key", text)

    def test_unknown_tool_is_tool_error(self):
        self.expose("res.partner", can_read=True)
        env = self.env(user=self.writer_user)
        ctx = CallContext.build(env, self.writer_user.id, self.client.id, {}, 1, "en")
        with self.assertRaises(ToolError):
            GenericToolProvider(env).call("odoo_nope", {"model": "res.partner"}, ctx)

    def test_model_level_call_confined_to_extra_domain(self):
        """Re-review: records a model-level call returns or creates must match the extra domain."""
        self.expose("res.partner", can_read=True, extra_domain="[('is_company', '=', True)]",
                    methods=[("find_or_create", "write")])
        outside = self.partner("GMX Outside Person", email="gmx.outside@example.com")
        data, _text = self.ok("odoo_call_method", {"model": "res.partner", "ids": [], "method": "find_or_create",
                                                   "kwargs": {"email": "gmx.outside@example.com"}})
        self.assertEqual(data["result"], [])  # existing record outside the extra domain is dropped
        self.assertTrue(outside.exists())
        self.err("odoo_call_method", {"model": "res.partner", "ids": [], "method": "find_or_create",
                                      "kwargs": {"email": "gmx.created@example.com"}})
        self.assertFalse(self.env["res.partner"].search([("email", "=", "gmx.created@example.com")]))  # rolled back

    def test_reference_links_must_be_visible(self):
        """Re-review: many2one_reference (with its model field) and reference values are link-checked."""
        Company = self.env["res.company"].sudo()
        company_a = Company.create({"name": "GMX Ref A"})
        company_b = Company.create({"name": "GMX Ref B"})
        user_b = self._make_user("gmcp_ref_b", "MCP Ref B", ["base.group_user", "base.group_partner_manager",
                                                              "gental_mcp.group_mcp_connect"])
        user_b.write({"company_ids": [(6, 0, [company_b.id])], "company_id": company_b.id})
        p_a = self.partner("GMX Ref Partner A", company_id=company_a.id)
        p_b = self.partner("GMX Ref Partner B", company_id=company_b.id)
        # ir.attachment.res_model is readonly on 17.0 (field_guard rightly refuses it): that half runs from 18.0.
        attachment_carrier = not self.env["ir.attachment"]._fields["res_model"].readonly
        if attachment_carrier:
            self.expose("ir.attachment", can_read=True, can_create=True, can_write=True)
            self.err("odoo_create", {"model": "ir.attachment", "values": {
                "name": "gmx-a.txt", "res_model": "res.partner", "res_id": p_a.id}}, user=user_b)
            self.err("odoo_create", {"model": "ir.attachment", "values": {
                "name": "gmx-x.txt", "res_model": "no.such.model", "res_id": 1}}, user=user_b)
            data, _text = self.ok("odoo_create", {"model": "ir.attachment", "values": {
                "name": "gmx-b.txt", "res_model": "res.partner", "res_id": p_b.id}}, user=user_b)
            self.err("odoo_update", {"model": "ir.attachment", "ids": [data["id"]], "values": {"res_id": p_a.id}},
                     user=user_b)
            self.assertEqual(self.env["ir.attachment"].browse(data["id"]).res_id, p_b.id)
        # Reference fields ("model,id"): checked the same way (ir.ui.menu.action as the carrier).
        self.expose("ir.ui.menu", can_read=True)
        env_b = self.env(user=user_b)
        rule = env_b["gental.mcp.exposed.model"]._mcp_rules("ir.ui.menu")["ir.ui.menu"]
        provider = GenericToolProvider(env_b)
        self.assertEqual(provider._values(env_b, rule, {"action": f"res.partner,{p_b.id}"}),
                         {"action": f"res.partner,{p_b.id}"})
        for bad in (f"res.partner,{p_a.id}", "res.partner,999999999", "nonsense", "no.model,1", 5):
            with self.subTest(value=bad), self.assertRaises(ToolError):
                provider._values(env_b, rule, {"action": bad})
        if not attachment_carrier:
            return
        # The many2one_reference check itself (ir.attachment also checks on create, so isolate ours).
        att_rule = env_b["gental.mcp.exposed.model"]._mcp_rules("ir.attachment")["ir.attachment"]
        attachment = self.env["ir.attachment"].browse(data["id"])
        for values, records in (({"res_model": "res.partner", "res_id": p_a.id}, None),
                                ({"res_id": p_a.id}, attachment.with_user(user_b)),
                                ({"res_model": "res.users"}, attachment.with_user(user_b))):
            with self.subTest(values=values), self.assertRaises(ToolError):
                provider._values(env_b, att_rule, values, records)
        provider._values(env_b, att_rule, {"res_model": "res.partner", "res_id": p_b.id})

    def test_bad_value_type_in_domain_is_tool_error(self):
        """Re-review: SQL type errors (psycopg2 DataError) come back as tool errors, not Internal error."""
        self.expose("res.partner", can_read=True)
        for domain in ([["color", "=", "abc"]], [["id", "=", "abc"]], [["create_date", ">", "not-a-date"]]):
            with self.subTest(domain=domain), mute_logger("odoo.sql_db"):  # Odoo logs the bad query
                text = self.err("odoo_search", {"model": "res.partner", "domain": domain})
                self.assertNotEqual(text, "Internal error")
        self.ok("odoo_search", {"model": "res.partner", "domain": [["name", "=", "GMX"]]})  # cursor still usable

    def test_access_error_becomes_tool_error(self):
        """The acting user's own ACLs apply: agent_user cannot create partners."""
        self.expose("res.partner", can_read=True, can_create=True)
        text = self.err("odoo_create", {"model": "res.partner", "values": {"name": "GMX No Rights"}},
                        user=self.agent_user)
        self.assertNotEqual(text, "Internal error")
        self.assertIn("res.partner", text)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "GMX No Rights")]))
        log = self.env["gental.mcp.log"].sudo().search([("tool", "=", "odoo_create")], limit=1)
        self.assertEqual(log.status, "denied")  # wall 3 is a denial, not an error


def server_env(server):
    return next(p for p in server.providers if isinstance(p, GenericToolProvider)).env
