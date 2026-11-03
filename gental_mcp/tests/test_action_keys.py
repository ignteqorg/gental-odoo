import contextlib
import json
from unittest.mock import patch

from odoo.service import model as service_model
from odoo.sql_db import Cursor
from odoo.tests import tagged

from odoo.addons.gental_mcp.core import meta
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.tools import (
    ToolAnnotations, ToolError, ToolOutcomeUnknown, ToolResult, ToolSpec,
)
from odoo.addons.gental_mcp.services import registry, result_meta
from odoo.addons.gental_mcp.services.action_keys import once_per_key

from .common import GentalMcpCase, PortableHttpCase, use_base_url

CLAIM_SQL = "ON CONFLICT (key, model) DO NOTHING"
# Odoo's request retry loop: ``odoo.service.model.retrying`` up to 19.0, ``odoo.http.retrying`` from 20.0.
RETRY_MODULE = "odoo.service.model" if hasattr(service_model, "retrying") else "odoo.http.retrying"


@contextlib.contextmanager
def serialization_failure_on_claim(times):
    """Make the next ``times`` key claims fail like a key committed by a concurrent transaction (40001)."""
    original = Cursor.execute
    left = [times]

    def execute(self, query, params=None, log_exceptions=True):
        if left[0] and CLAIM_SQL in str(query):
            left[0] -= 1
            query, params = "DO $$ BEGIN RAISE EXCEPTION 'simulated' USING ERRCODE = '40001'; END $$", None
        return original(self, query, params, log_exceptions=log_exceptions)

    with patch.object(Cursor, "execute", execute):
        yield


def _make_partner_user(case_cls):
    return case_cls._make_user(
        "gmcp_key_agent", "MCP Key Agent",
        ["base.group_user", "base.group_partner_manager", "gental_mcp.group_mcp_connect"],
    )


@tagged("post_install", "-at_install", "gental_mcp")
class TestActionKeys(GentalMcpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = _make_partner_user(cls)
        Client = cls.env["gental.mcp.client"]
        cls.client = Client.create({"name": "keys", "kind": "api_key", "user_id": cls.agent_user.id})
        cls.other_client = Client.create({"name": "other", "kind": "api_key", "user_id": cls.agent_user.id})
        cls.user_env = cls.env(user=cls.agent_user)

    def ctx(self, key=None, locale="en", client=None):
        call_meta = {meta.ACTION_KEY: key} if key is not None else {}
        return CallContext(self.user_env, self.agent_user.id, (client or self.client).id, locale, call_meta)

    def partner_fn(self, name="gmcp key contact"):
        return lambda: self.user_env["res.partner"].create({"name": name})

    def count(self, name="gmcp key contact"):
        return self.env["res.partner"].search_count([("name", "=", name)])

    def test_no_key_creates_each_time(self):
        first, created1 = once_per_key(self.ctx(), "res.partner", self.partner_fn())
        second, created2 = once_per_key(self.ctx(), "res.partner", self.partner_fn())
        self.assertTrue(created1 and created2)
        self.assertNotEqual(first, second)
        self.assertEqual(self.count(), 2)
        self.assertFalse(self.env["gental.mcp.action.key"].search_count([]))

    def test_same_key_returns_same_record(self):
        first, created1 = once_per_key(self.ctx("k-1"), "res.partner", self.partner_fn())
        second, created2 = once_per_key(self.ctx("k-1"), "res.partner", self.partner_fn())
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first, second)
        self.assertEqual(second.env.uid, self.agent_user.id, "the existing record comes back as the acting user")
        self.assertEqual(self.count(), 1)
        row = self.env["gental.mcp.action.key"].search([("key", "=", "k-1")])
        self.assertEqual((row.model, row.res_id, row.client_id), ("res.partner", first.id, self.client))

    def test_same_key_other_model_independent(self):
        once_per_key(self.ctx("k-2"), "res.partner", self.partner_fn())
        tag, created = once_per_key(
            self.ctx("k-2"), "res.partner.category",
            lambda: self.env["res.partner.category"].create({"name": "gmcp key tag"}),
        )
        self.assertTrue(created)
        self.assertEqual(tag._name, "res.partner.category")
        self.assertEqual(self.env["gental.mcp.action.key"].search_count([("key", "=", "k-2")]), 2)
        self.assertEqual(self.count(), 1)

    def test_key_taken_in_same_transaction_resolves_to_existing(self):
        # Covers a lost claim whose winner is visible to this transaction (the ON CONFLICT branch);
        # the cross-transaction race is covered by the serialization-failure tests below.
        winner = self.user_env["res.partner"].create({"name": "gmcp key winner"})
        Keys = self.env["gental.mcp.action.key"].sudo()

        def racing_create():
            Keys.create({"key": "k-3", "model": "res.partner", "res_id": winner.id, "client_id": self.client.id})
            return self.user_env["res.partner"].create({"name": "gmcp key loser"})

        record, created = once_per_key(self.ctx("k-3"), "res.partner", racing_create)
        self.assertFalse(created)
        self.assertEqual(record, winner)
        self.assertEqual(self.count("gmcp key loser"), 0, "the duplicate is rolled back")

    def test_serialization_failure_escapes_when_retryable(self):
        ctx = self.ctx("k-race")
        ctx.is_retryable = lambda e: getattr(e, "pgcode", None) == "40001"
        with serialization_failure_on_claim(1), self.assertRaises(Exception) as caught:
            once_per_key(ctx, "res.partner", self.partner_fn())
        self.assertEqual(getattr(caught.exception, "pgcode", None), "40001")
        self.assertEqual(self.count(), 0, "this attempt's record is rolled back")

    def test_serialization_failure_without_retry_is_outcome_unknown(self):
        with serialization_failure_on_claim(1), self.assertRaises(ToolOutcomeUnknown):
            once_per_key(self.ctx("k-race-2"), "res.partner", self.partner_fn())
        self.assertEqual(self.count(), 0)

    def test_key_of_other_client_is_not_returned(self):
        once_per_key(self.ctx("k-6", client=self.other_client), "res.partner", self.partner_fn())
        with self.assertRaises(ToolError) as caught:
            once_per_key(self.ctx("k-6"), "res.partner", self.partner_fn())
        self.assertEqual(str(caught.exception), "The record this action created is no longer available.")
        self.assertEqual(self.count(), 1)

    def test_missing_record_is_a_tool_error(self):
        record, _created = once_per_key(self.ctx("k-4"), "res.partner", self.partner_fn())
        record.sudo().unlink()
        with self.assertRaises(ToolError):
            once_per_key(self.ctx("k-4"), "res.partner", self.partner_fn())
        self.assertEqual(self.count(), 0)

    def test_tool_error_text_follows_locale(self):
        record, _created = once_per_key(self.ctx("k-5"), "res.partner", self.partner_fn())
        record.sudo().unlink()
        with self.assertRaises(ToolError) as caught:
            once_per_key(self.ctx("k-5", locale="ar"), "res.partner", self.partner_fn())
        self.assertEqual(str(caught.exception), "السجل اللي أنشأه هذا الإجراء ما عاد موجود.")

    def test_invalid_key_is_a_tool_error(self):
        for bad in ("x" * 201, "has space", "ملف", 42):
            with self.subTest(key=bad), self.assertRaises(ToolError) as caught:
                once_per_key(self.ctx(bad), "res.partner", self.partner_fn())
        self.assertIn("1 to 200 characters", str(caught.exception))
        once_per_key(self.ctx("A-z_0.9:x" + "y" * 191), "res.partner", self.partner_fn())
        self.assertEqual(self.count(), 1)

    def test_create_fn_must_return_one_record(self):
        with self.assertRaises(ValueError):
            once_per_key(self.ctx("k-7"), "res.partner", lambda: self.env["res.partner"])

    def test_outcome_unknown_meta(self):
        result = result_meta.outcome_unknown(ToolResult("sent"))
        self.assertEqual(result.to_mcp()["_meta"], {meta.OUTCOME: "unknown"})

    def test_staff_review_meta(self):
        result = result_meta.staff_review(ToolResult("ok"), "name differs")
        self.assertEqual(result.meta, {meta.FLAG: "staff_review", meta.FLAG_REASON: "name differs"})


class _KeyedCreateProvider:
    def __init__(self, env):
        self.env = env

    def list_tools(self, ctx):
        return [ToolSpec("test_keyed_create", "Keyed create", "Creates a contact once per action key.",
                         {"type": "object"}, ToolAnnotations(read_only=False))]

    def call(self, name, args, ctx):
        record, created = once_per_key(
            ctx, "res.partner", lambda: ctx.env["res.partner"].create({"name": "gmcp replay contact"}),
        )
        return ToolResult("ok", structured={"id": record.id, "created": created})


@tagged("post_install", "-at_install", "gental_mcp")
class TestActionKeyReplay(GentalMcpCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent_user = _make_partner_user(cls)
        registry.register_provider(_KeyedCreateProvider)
        use_base_url(cls.env, cls.base_url())

    @classmethod
    def tearDownClass(cls):
        registry.unregister_provider(_KeyedCreateProvider)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.client, self.key = self.env["gental.mcp.client"].create_api_key("replay", self.agent_user)
        # No real back-off between replays.
        patcher = patch(f"{RETRY_MODULE}.random.uniform", return_value=0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call_keyed(self, key):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "test_keyed_create", "arguments": {}, "_meta": {meta.ACTION_KEY: key, meta.LOCALE: "en"}}}
        resp = self.url_open("/mcp", data=json.dumps(msg), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.key}",
        })
        self.assertEqual(resp.status_code, 200)
        return resp.json()["result"]

    def count(self):
        return self.env["res.partner"].search_count([("name", "=", "gmcp replay contact")])

    def test_concurrent_winner_is_returned_after_replay(self):
        # A concurrent call committed the winner after our snapshot: the first attempt can't see its key
        # (lookup misses, the claim fails with 40001); Odoo replays the request, which finds the winner.
        winner = self.env["res.partner"].create({"name": "gmcp replay winner"})
        Keys = self.env["gental.mcp.action.key"]
        Keys.sudo().create({"key": "k-replay", "model": "res.partner", "res_id": winner.id,
                            "client_id": self.client.id})
        original = type(Keys)._find_res_id
        lookups = []

        def find(self_, key, model, client_id):
            lookups.append(key)
            return (False, None) if len(lookups) == 1 else original(self_, key, model, client_id)

        with patch.object(type(Keys), "_find_res_id", find), serialization_failure_on_claim(1):
            result = self.call_keyed("k-replay")
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"], {"id": winner.id, "created": False})
        self.assertEqual(self.count(), 0, "the first attempt's record is rolled back")
        logs = self.env["gental.mcp.log"].sudo().search([("client_id", "=", self.client.id), ("kind", "=", "call")])
        self.assertEqual(len(logs), 1, "only the replay that answered is logged")

    def test_retries_exhausted_reports_outcome_unknown(self):
        with serialization_failure_on_claim(100):
            result = self.call_keyed("k-exhausted")
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["_meta"], {meta.OUTCOME: "unknown"})
        self.assertEqual(result["content"][0]["text"], "This action may already be done. Check before doing it again.")
        self.assertEqual(self.count(), 0)
        # Logged as its own Inbox status, not "ok" (and not an error).
        log = self.env["gental.mcp.log"].sudo().search([("client_id", "=", self.client.id), ("kind", "=", "call")])
        self.assertEqual(log.status, "unknown")
