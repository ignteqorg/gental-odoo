import hashlib
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessDenied, AccessError, ValidationError
from odoo.tests import tagged

from odoo.addons.gental_mcp.core.crypto import pkce_s256
from odoo.addons.gental_mcp.compat import rpc_call_kw, set_param, user_groups_field
from odoo.addons.gental_mcp.models.mcp_token import _check_protected_vals

from .common import GentalMcpCase, tracking_text

RESOURCE = "https://odoo.example.com/mcp"


@tagged("post_install", "-at_install", "gental_mcp")
class TestModels(GentalMcpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Client = cls.env["gental.mcp.client"].sudo()
        cls.Token = cls.env["gental.mcp.token"].sudo()
        cls.oauth_client = cls.Client.create({
            "name": "Claude",
            "kind": "oauth",
            "client_id": "https://claude.ai/oauth/client.json",
            "redirect_uris": "https://claude.ai/callback\nhttps://claude.ai/cb2",
            "cimd": True,
            "registration": "cimd",
        })

    def _denied(self, func, *args):
        """Call ``func`` expecting ``AccessDenied`` *without* a savepoint, like the OAuth controller.

        Odoo's ``assertRaises`` rolls back to a savepoint, which would undo the side effects that
        must survive a denial (burnt code, revoked family); the controller answers with an error
        response instead, so the request transaction commits them.
        """
        try:
            func(*args)
        except AccessDenied:
            return
        self.fail("AccessDenied not raised")

    def _flush_tracking(self):
        self.env.flush_all()
        self.env.cr.precommit.run()

    # -- config -----------------------------------------------------------------

    def test_config_singleton(self):
        cfg = self.env["gental.mcp.config"].get()
        self.assertEqual(cfg, self.env.ref("gental_mcp.config_main"))
        self.assertEqual(cfg, self.env["gental.mcp.config"].get())
        self.assertEqual(self.env["gental.mcp.config"].sudo().search_count([]), 1)
        self.assertTrue(cfg.enabled and cfg.oauth_enabled and cfg.dcr_enabled and cfg.api_keys_enabled)
        self.assertEqual(cfg.default_lang, "ar")
        self.assertEqual(cfg.rate_limit_per_min, 300)
        self.assertEqual(cfg.log_retention_days, 90)
        self.assertIn(self.env.ref("gental_mcp.group_mcp_connect"), cfg.connect_group_ids)
        cfg.public_url = "https://odoo.example.com/"
        self.assertEqual(cfg.mcp_url(), RESOURCE)
        with self.assertRaises(ValidationError):
            self.env["gental.mcp.config"].sudo().create({})

    def test_effective_url_follows_web_base_url(self):
        """Public URL is an optional override; without it the live web.base.url is used (never frozen)."""
        Config = self.env["gental.mcp.config"]
        self.assertFalse(Config.default_get(["public_url"]).get("public_url"))
        cfg = Config.get()
        cfg.public_url = False
        params = self.env["ir.config_parameter"].sudo()
        set_param(params, "web.base.url", "https://Odoo.Example.com/")
        self.assertEqual(cfg.mcp_url(), RESOURCE)
        set_param(params, "web.base.url", "https://erp.example.com")
        self.assertEqual(cfg.mcp_url(), "https://erp.example.com/mcp")
        self.assertEqual(cfg.mcp_endpoint, "https://erp.example.com/mcp")
        cfg.public_url = "https://mcp.example.com"
        self.assertEqual(cfg.base_url(), "https://mcp.example.com")
        self.assertEqual(cfg.mcp_url(), "https://mcp.example.com/mcp")

    def test_public_url_must_be_https_root(self):
        cfg = self.env["gental.mcp.config"].get()
        for bad in ("http://odoo.example.com", "https://odoo.example.com/odoo", "https://odoo.example.com/?db=x",
                    "https://u:p@odoo.example.com", "odoo.example.com", "https://odoo.example.com:99999"):
            with self.subTest(url=bad), self.assertRaises(ValidationError):
                cfg.public_url = bad
                self.env.flush_all()
        for good in ("https://odoo.example.com", "https://odoo.example.com/", "https://odoo.example.com:8443"):
            cfg.public_url = good
            self.env.flush_all()
            self.assertEqual(cfg.base_url(), good.rstrip("/"))

    def test_config_change_tracked(self):
        cfg = self.env["gental.mcp.config"].get()
        before = len(cfg.message_ids)
        cfg.rate_limit_per_min = 60
        self._flush_tracking()
        self.assertGreater(len(cfg.message_ids), before)
        self.assertIn(cfg._fields["rate_limit_per_min"].string, tracking_text(cfg.message_ids))

    # -- exposed models / methods ----------------------------------------------

    def _expose_partner(self):
        return self.env["gental.mcp.exposed.model"].sudo().create({
            "model_id": self.env["ir.model"]._get_id("res.partner"),
            "can_read": True,
        })

    def test_method_name_rules(self):
        exposed = self._expose_partner()
        Method = self.env["gental.mcp.exposed.method"].sudo()
        bad_names = (
            "_compute", "unlink", "sudo", "nonexistent_x", "action_archive", "write", "_get_name",
            "web_save", "fetch", "exists", "lock_for_update", "is_transient",  # forbidden, @api.private, classmethod
        )
        for bad in bad_names:
            with self.subTest(name=bad), self.assertRaises(ValidationError):
                Method.create({"exposed_model_id": exposed.id, "name": bad})
        ok = Method.create({"exposed_model_id": exposed.id, "name": " address_get ", "kind": "read"})
        self.assertEqual(ok.name, "address_get")
        self.assertEqual(Method.new({}).kind, "write")

    def test_extra_domain_must_parse(self):
        exposed = self._expose_partner()
        exposed.extra_domain = "[('is_company', '=', True)]"
        for bad in ("[('a',", "{'a': 1}", "[('a', '=')]", "__import__('os')"):
            with self.subTest(domain=bad), self.assertRaises(ValidationError):
                exposed.extra_domain = bad
                self.env.flush_all()

    # -- clients / api keys ----------------------------------------------------

    def test_api_key_shown_once_and_hashed(self):
        client, plaintext = self.Client.create_api_key("Gental", self.agent_user)
        self.assertTrue(plaintext.startswith("gmcp_"))
        self.assertEqual(client.key_hash, hashlib.sha256(plaintext.encode()).hexdigest())
        self.assertEqual(client.key_hint, plaintext[-4:])
        self.assertNotIn(plaintext, str(client.read()[0]))
        self.assertEqual(self.Client.find_by_api_key(plaintext), client)
        self.assertFalse(self.Client.find_by_api_key(plaintext + "x"))
        self.assertFalse(self.Client.find_by_api_key(""))
        self.assertEqual(self.make_api_key(self.agent_user)[:5], "gmcp_")
        client.action_revoke()
        self.assertEqual(client.state, "revoked")
        self.assertTrue(client.revoked_at)
        self.assertFalse(self.Client.find_by_api_key(plaintext))

    def test_api_key_expired_not_found(self):
        past = fields.Datetime.now() - timedelta(minutes=1)
        _client, plaintext = self.Client.create_api_key("Old", self.agent_user, expires_at=past)
        self.assertFalse(self.Client.find_by_api_key(plaintext))

    def test_manager_cannot_bind_client_to_another_user(self):
        """Wall against escalation through the ORM: a manager who is not a system admin can neither create
        nor re-bind a client acting as someone else, nor set a key hash directly."""
        admin = self.env.ref("base.user_admin")
        Client = self.env["gental.mcp.client"].with_user(self.manager_user)
        with self.assertRaises(AccessError):
            Client.create_api_key("Escalate", admin)
        with self.assertRaises(AccessError):
            Client.create({"name": "Escalate", "kind": "api_key", "user_id": admin.id})
        mine, _key = Client.create_api_key("Mine", self.manager_user)
        self.assertEqual(mine.user_id, self.manager_user)
        with self.assertRaises(AccessError):
            mine.with_user(self.manager_user).write({"user_id": admin.id})
        with self.assertRaises(AccessError):
            mine.with_user(self.manager_user).write({"key_hash": hashlib.sha256(b"gmcp_known").hexdigest()})
        mine.with_user(self.manager_user).write({"name": "Renamed"})  # other fields stay editable
        self.assertEqual(mine.user_id, self.manager_user)
        # A system administrator may create a key for another user.
        _client, key = self.env["gental.mcp.client"].with_user(admin).create_api_key("For agent", self.agent_user)
        self.assertEqual(self.Client.find_by_api_key(key).user_id, self.agent_user)

    def test_manager_cannot_rebind_tokens_or_codes(self):
        """A manager who is not a system admin can't make their own token or code act as the admin (RPC)."""
        admin = self.env.ref("base.user_admin")
        mine, _key = self.Client.create_api_key("Mine", self.manager_user)
        oauth = self.Client.create({"name": "OAuth", "kind": "oauth", "client_id": "rebind"})
        pair = self.Token.issue_pair(oauth, self.manager_user, RESOURCE)
        token = self.Token.search([("client_id", "=", oauth.id)], limit=1)
        code_plain = self.env["gental.mcp.auth.code"].issue(oauth, self.manager_user, "https://claude.ai/cb",
                                                            pkce_s256("v" * 43), RESOURCE)
        code = self.env["gental.mcp.auth.code"].sudo().search([("client_id", "=", oauth.id)], limit=1)
        for model, rec, vals in (
            ("gental.mcp.token", token, {"user_id": admin.id}),
            ("gental.mcp.token", token, {"revoked": True}),
            ("gental.mcp.auth.code", code, {"user_id": admin.id}),
        ):
            with self.subTest(model=model, vals=vals), self.assertRaises(AccessError):
                rpc_call_kw(self.env[model].with_user(self.manager_user), "write", [rec.ids, vals], {})
        # The guard itself, past the ACL: only superuser code or a system admin may re-bind.
        for rec in (token, code):
            as_manager = rec.with_user(self.manager_user)
            with self.subTest(guard=rec._name), self.assertRaises(AccessError):
                _check_protected_vals(as_manager, [{"user_id": admin.id}], rec.PROTECTED_FIELDS)
            _check_protected_vals(as_manager, [{"revoked": True, "used": True}], rec.PROTECTED_FIELDS)
            _check_protected_vals(rec.with_user(admin), [{"user_id": admin.id}], rec.PROTECTED_FIELDS)
        self.assertEqual((token.user_id, code.user_id), (self.manager_user, self.manager_user))
        self.assertEqual(self.Token.find_access(pair["access_token"]).user_id, self.manager_user)
        self.assertTrue(code_plain.startswith("gmac_") and mine)
        # Managers still revoke through the button (sudo inside, write access checked on the client).
        oauth.with_user(self.manager_user).action_revoke()
        self.assertTrue(token.revoked)

    # -- tokens -----------------------------------------------------------------

    def test_issue_pair_and_find_access(self):
        pair = self.Token.issue_pair(self.oauth_client, self.agent_user, RESOURCE)
        self.assertEqual(pair["expires_in"], 3600)
        self.assertEqual(pair["token_type"], "Bearer")
        self.assertTrue(pair["access_token"].startswith("gmat_"))
        self.assertTrue(pair["refresh_token"].startswith("gmrt_"))
        access = self.Token.find_access(pair["access_token"])
        self.assertEqual(access.user_id, self.agent_user)
        self.assertEqual(access.resource, RESOURCE)
        self.assertEqual(access.token_hash, hashlib.sha256(pair["access_token"].encode()).hexdigest())
        self.assertFalse(self.Token.find_access(pair["refresh_token"]))
        access.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertFalse(self.Token.find_access(pair["access_token"]))

    def test_refresh_rotation_and_reuse_revokes_family(self):
        first = self.Token.issue_pair(self.oauth_client, self.agent_user, RESOURCE)
        second = self.Token.rotate_refresh(first["refresh_token"], self.oauth_client)
        self.assertNotEqual(first["refresh_token"], second["refresh_token"])
        self.assertTrue(self.Token.find_access(second["access_token"]))
        # A different client cannot use the refresh token.
        other = self.Client.create({"name": "Other", "kind": "oauth", "client_id": "other"})
        with self.assertRaises(AccessDenied):
            self.Token.rotate_refresh(second["refresh_token"], other)
        # Reusing the rotated token revokes the whole family.
        self._denied(self.Token.rotate_refresh, first["refresh_token"], self.oauth_client)
        self.assertFalse(self.Token.find_access(second["access_token"]))
        with self.assertRaises(AccessDenied):
            self.Token.rotate_refresh(second["refresh_token"], self.oauth_client)

    def test_auth_code_single_use_and_pkce(self):
        Code = self.env["gental.mcp.auth.code"].sudo()
        verifier = "v" * 43
        redirect = "https://claude.ai/callback"
        code = Code.issue(self.oauth_client, self.agent_user, redirect, pkce_s256(verifier), RESOURCE)
        self.assertTrue(code.startswith("gmac_"))
        rec = Code.consume(code, self.oauth_client, redirect, verifier)
        self.assertEqual(rec.user_id, self.agent_user)
        self.assertTrue(rec.used)
        with self.assertRaises(AccessDenied):
            Code.consume(code, self.oauth_client, redirect, verifier)
        # Wrong verifier: refused, and the code is burnt anyway.
        code2 = Code.issue(self.oauth_client, self.agent_user, redirect, pkce_s256(verifier), RESOURCE)
        self._denied(Code.consume, code2, self.oauth_client, redirect, "w" * 43)
        rec2 = Code.search([("code_hash", "=", hashlib.sha256(code2.encode()).hexdigest())])
        self.assertTrue(rec2.used)
        with self.assertRaises(AccessDenied):
            Code.consume(code2, self.oauth_client, redirect, verifier)
        # Redirect mismatch and expiry are refused too.
        code3 = Code.issue(self.oauth_client, self.agent_user, redirect, pkce_s256(verifier), RESOURCE)
        with self.assertRaises(AccessDenied):
            Code.consume(code3, self.oauth_client, "https://evil.example/cb", verifier)
        code4 = Code.issue(self.oauth_client, self.agent_user, redirect, pkce_s256(verifier), RESOURCE)
        Code.search([("code_hash", "=", hashlib.sha256(code4.encode()).hexdigest())]).expires_at = (
            fields.Datetime.now() - timedelta(seconds=1)
        )
        with self.assertRaises(AccessDenied):
            Code.consume(code4, self.oauth_client, redirect, verifier)

    # -- revocation hook --------------------------------------------------------

    def test_user_archived_revokes_tokens(self):
        user = self._make_user("gmcp_arch", "Archived", ["base.group_user", "gental_mcp.group_mcp_connect"])
        pair = self.Token.issue_pair(self.oauth_client, user, RESOURCE)
        _client, key = self.Client.create_api_key("k", user)
        user.active = False
        self.assertFalse(self.Token.find_access(pair["access_token"]))
        self.assertFalse(self.Client.find_by_api_key(key))
        with self.assertRaises(AccessDenied):
            self.Token.rotate_refresh(pair["refresh_token"], self.oauth_client)

    def test_user_removed_from_connect_group_revokes_tokens(self):
        user = self._make_user("gmcp_rm", "Removed", ["base.group_user", "gental_mcp.group_mcp_connect"])
        bystander = self.Token.issue_pair(self.oauth_client, self.agent_user, RESOURCE)
        pair = self.Token.issue_pair(self.oauth_client, user, RESOURCE)
        # An unrelated group change keeps the tokens.
        user.write({user_groups_field(self.env): [(4, self.env.ref("base.group_partner_manager").id)]})
        self.assertTrue(self.Token.find_access(pair["access_token"]))
        user.write({user_groups_field(self.env): [(3, self.env.ref("gental_mcp.group_mcp_connect").id)]})
        self.assertFalse(self.Token.find_access(pair["access_token"]))
        self.assertTrue(self.Token.find_access(bystander["access_token"]))

    def test_group_side_changes_revoke_tokens(self):
        connect = self.env.ref("gental_mcp.group_mcp_connect")
        extra = self.env["res.groups"].create({"name": "Gental MCP test extra"})
        user = self._make_user("gmcp_grp", "Grp", ["base.group_user", "gental_mcp.group_mcp_connect"])
        pair = self.Token.issue_pair(self.oauth_client, user, RESOURCE)
        users_field = "user_ids" if "user_ids" in connect._fields else "users"
        connect.write({users_field: [(3, user.id)]})
        self.assertFalse(self.Token.find_access(pair["access_token"]))
        # Connected only through a group that implies connect: dropping the implication revokes.
        user2 = self._make_user("gmcp_grp2", "Grp2", ["base.group_user"])
        extra.write({"implied_ids": [(4, connect.id)], users_field: [(4, user2.id)]})
        pair2 = self.Token.issue_pair(self.oauth_client, user2, RESOURCE)
        extra.write({"implied_ids": [(3, connect.id)]})
        if "all_group_ids" not in self.env["res.users"]._fields:
            # Before 19.0, implied groups are copied into the user's own groups: dropping the implication
            # leaves user2 directly in connect, so (as in Odoo) they still qualify until removed from it.
            self.assertIn(connect, user2[user_groups_field(self.env)])
            self.assertTrue(self.Token.find_access(pair2["access_token"]))
            connect.write({users_field: [(3, user2.id)]})
        self.assertFalse(self.Token.find_access(pair2["access_token"]))
        # Narrowing the settings' connect groups revokes users who no longer qualify.
        user3 = self._make_user("gmcp_grp3", "Grp3", ["base.group_user", "gental_mcp.group_mcp_connect"])
        pair3 = self.Token.issue_pair(self.oauth_client, user3, RESOURCE)
        self.env["gental.mcp.config"].get().connect_group_ids = extra
        self.assertFalse(self.Token.find_access(pair3["access_token"]))

    # -- inbox log / rate -------------------------------------------------------

    def test_mask_args(self):
        Log = self.env["gental.mcp.log"]
        masked = Log.mask_args({
            "password": "p", "client_secret": "s", "access_token": "t", "apiKey": "k", "api_key": "k",
            "Authorization": "Bearer x", "totp_code": "1", "otp": "2", "name": "Azure",
            "nested": {"token": "t", "x": 1},
        })
        for key in ("password", "client_secret", "access_token", "apiKey", "api_key", "Authorization", "totp_code", "otp"):
            self.assertEqual(masked[key], "•••", key)
        self.assertEqual(masked["name"], "Azure")
        self.assertEqual(masked["nested"], {"token": "•••", "x": 1})

    def test_record_call_masks_and_truncates(self):
        rec_id = self.env["gental.mcp.log"].record_call(
            kind="call", client_id=self.oauth_client.id, user_id=self.agent_user.id, tool="search",
            tool_kind="read", status="ok", args={"password": "hunter2", "q": "x"}, result_text="r" * 5000,
        )
        self.assertIsInstance(rec_id, int)
        rec = self.env["gental.mcp.log"].sudo().browse(rec_id)
        self.assertNotIn("hunter2", rec.args_json)
        self.assertIn("•••", rec.args_json)
        self.assertEqual(len(rec.result_text), 4000)

    def test_rate_hit_counts_per_window(self):
        Rate = self.env["gental.mcp.rate"]
        clock = "odoo.addons.gental_mcp.models.mcp_rate.time.time"
        with patch(clock, return_value=1_800_000_000.0):  # 30 s into a minute
            self.assertTrue(Rate.hit("client:1", 2))
            self.assertTrue(Rate.hit("client:1", 2))
            self.assertFalse(Rate.hit("client:1", 2))
            self.assertTrue(Rate.hit("client:2", 2))
        with patch(clock, return_value=1_800_000_000.0 + 60):  # next window starts over
            self.assertTrue(Rate.hit("client:1", 2))

    # -- access -----------------------------------------------------------------

    def test_non_manager_cannot_read_clients(self):
        with self.assertRaises(AccessError):
            self.env["gental.mcp.client"].with_user(self.agent_user).search([])
        with self.assertRaises(AccessError):
            self.env["gental.mcp.token"].with_user(self.agent_user).search([])
        self.assertTrue(self.env["gental.mcp.client"].with_user(self.manager_user).search([]))

    def test_bookkeeping_methods_not_rpc_callable(self):
        portal = self._make_user("gmcp_portal", "Portal", ["base.group_portal"])
        blocked = {
            "gental.mcp.config": ["get", "may_connect", "base_url", "mcp_url"],
            "gental.mcp.client": ["create_api_key", "find_by_api_key", "is_known_api_key", "touch_last_used"],
            "gental.mcp.token": [
                "issue_pair", "find_access", "is_known_access", "rotate_refresh", "revoke_family", "revoke_for_user",
            ],
            "gental.mcp.auth.code": ["issue", "consume"],
            "gental.mcp.log": ["record_call", "record_throttled", "mask_args"],
            "gental.mcp.rate": ["hit"],
        }
        for user in (self.agent_user, portal, self.manager_user):
            for model, methods in blocked.items():
                for name in methods:
                    with self.subTest(user=user.login, method=f"{model}.{name}"), \
                            self.assertRaises(AccessError):
                        rpc_call_kw(self.env[model].with_user(user), name, [], {})
        for user in (self.agent_user, portal):
            with self.subTest(user=user.login), self.assertRaises(AccessError):
                rpc_call_kw(self.env["gental.mcp.client"].with_user(user), "action_revoke", [[self.oauth_client.id]], {})
        self.assertEqual(self.oauth_client.state, "active")
        rpc_call_kw(self.env["gental.mcp.client"].with_user(self.manager_user), "action_revoke", [[self.oauth_client.id]], {})
        self.assertEqual(self.oauth_client.state, "revoked")
