import json
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.gental_mcp.compat import user_groups_field
from odoo.addons.gental_mcp.core.crypto import pkce_s256

from .common import GentalMcpCase, PortableHttpCase, use_base_url

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
CIMD_URL = "https://client.example.com/oauth/client.json"
VERIFIER = "v" * 43 + "-correct-verifier"
OTHER_VERIFIER = "w" * 43 + "-another-verifier"
PASSWORD = "gmcp-Agent-pass-1"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@tagged("post_install", "-at_install", "gental_mcp")
class TestOAuthFlow(GentalMcpCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cfg = cls.env["gental.mcp.config"].get()
        use_base_url(cls.env, cls.base_url())
        cls.cfg.write({"oauth_enabled": True, "enabled": True})
        cls.agent_user.password = PASSWORD
        cls.oauth = cls.env["gental.mcp.client"].register_dynamic("Claude", [REDIRECT])

    def setUp(self):
        super().setUp()
        # Freeze the rate counters' clock so a test's requests share one minute window (the limits and
        # the once-a-minute Inbox throttle are asserted by count; a minute boundary would reset them).
        clock = self.startPatcher(patch("odoo.addons.gental_mcp.models.mcp_rate.time"))
        clock.time.return_value = 1_800_000_000.0
        self.authenticate(self.agent_user.login, PASSWORD)

    # -- helpers ------------------------------------------------------------------

    def params(self, **over):
        params = {
            "response_type": "code",
            "client_id": self.oauth.client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": pkce_s256(VERIFIER),
            "code_challenge_method": "S256",
            "resource": self.cfg.mcp_url(),
            "state": "st-123",
        }
        params.update(over)
        return {k: v for k, v in params.items() if v is not None}

    def get_authorize(self, **over):
        return self.url_open("/mcp/oauth/authorize?" + urlencode(self.params(**over)), allow_redirects=False)

    def decide(self, decision="approve", **over):
        page = self.get_authorize(**over)
        self.assertEqual(page.status_code, 200, page.text)
        csrf = CSRF_RE.search(page.text).group(1)
        data = dict(self.params(**over), csrf_token=csrf, decision=decision)
        return self.url_open("/mcp/oauth/authorize", data=data, allow_redirects=False)

    def redirect_query(self, resp):
        self.assertIn(resp.status_code, (302, 303), resp.text)
        location = resp.headers["Location"]
        self.assertTrue(location.startswith(REDIRECT + "?"), location)
        return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}

    def get_code(self, **over):
        query = self.redirect_query(self.decide(**over))
        self.assertTrue(query["code"].startswith("gmac_"))
        return query["code"]

    def token(self, **data):
        return self.url_open("/mcp/oauth/token", data=data, allow_redirects=False)

    def redeem(self, code, verifier=VERIFIER, **over):
        data = {
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": self.oauth.client_id, "code_verifier": verifier, "resource": self.cfg.mcp_url(),
        }
        data.update(over)
        return self.token(**{k: v for k, v in data.items() if v is not None})

    def refresh(self, refresh_token):
        return self.token(grant_type="refresh_token", refresh_token=refresh_token, client_id=self.oauth.client_id)

    def pair(self):
        resp = self.redeem(self.get_code())
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def mcp_status(self, access_token):
        resp = self.url_open(
            "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        )
        return resp.status_code

    def assert_oauth_error(self, resp, error):
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"], error)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

    def assert_error_page(self, resp, status, text):
        self.assertEqual(resp.status_code, status, resp.text)
        self.assertNotIn("Location", resp.headers)
        self.assertIn(text, resp.text)

    # -- authorization code flow --------------------------------------------------

    def test_full_flow_then_mcp_call_ok(self):
        page = self.get_authorize()
        self.assertEqual(page.status_code, 200)
        self.assertIn("Claude", page.text)
        self.assertIn("claude.ai", page.text)  # the redirect host
        self.assertIn("MCP Agent", page.text)  # acts as
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", page.headers)
        query = self.redirect_query(self.decide())
        self.assertEqual(query["state"], "st-123")
        self.assertEqual(query["iss"], self.cfg.base_url())
        resp = self.redeem(query["code"])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")
        body = resp.json()
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["expires_in"], 3600)
        self.assertTrue(body["refresh_token"].startswith("gmrt_"))
        self.assertEqual(self.mcp_status(body["access_token"]), 200)
        log = self.env["gental.mcp.log"].sudo().search(
            [("kind", "=", "auth"), ("client_id", "=", self.oauth.id), ("user_id", "=", self.agent_user.id)],
            order="id asc",
        )
        self.assertEqual(log.mapped("status"), ["ok", "ok"])
        self.assertEqual(log.mapped("result_text"), ["approved", "token issued (code exchange)"])

    def auth_log(self, **domain):
        return self.env["gental.mcp.log"].sudo().search(
            [("client_id", "=", self.oauth.id)] + [(k, "=", v) for k, v in domain.items()], order="id asc"
        )

    def test_token_events_logged(self):
        """Spec §5.5: code exchange, refresh and revocation each leave an auth row; a failed grant a denied one."""
        tokens = self.pair()
        refreshed = self.refresh(tokens["refresh_token"]).json()
        self.assertNotIn("user_id", refreshed)  # Inbox data never reaches the client
        self.assertNotIn("client_id", refreshed)
        self.url_open("/mcp/oauth/revoke", data={"token": refreshed["access_token"]})
        texts = self.auth_log(kind="auth", status="ok").mapped("result_text")
        self.assertEqual(texts, ["approved", "token issued (code exchange)", "token refreshed", "token revoked"])
        self.assertEqual(self.auth_log(kind="auth", status="ok").user_id, self.agent_user)
        self.assert_oauth_error(self.refresh(tokens["refresh_token"]), "invalid_grant")  # reused
        denied = self.auth_log(kind="auth", status="denied")
        self.assertEqual(denied.mapped("result_text"), ["refresh_token: invalid_grant"])
        # Repeated failures from the same client are written once a minute.
        self.assert_oauth_error(self.redeem("gmac_bogus"), "invalid_grant")
        self.assertEqual(len(self.auth_log(kind="auth", status="denied")), 1)

    def test_ip_rate_limit_logged_once_a_minute(self):
        for _i in range(20):
            self.token(grant_type="password")
        for _i in range(3):
            self.assertEqual(self.token(grant_type="password").status_code, 429)
        rows = self.env["gental.mcp.log"].sudo().search([("kind", "=", "rate_limited"), ("result_text", "=", "oauth token")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.ip, "127.0.0.1")

    def test_not_signed_in_goes_to_odoo_login(self):
        self.authenticate(None, None)
        resp = self.get_authorize()
        self.assertIn(resp.status_code, (302, 303))
        self.assertIn("/web/login", resp.headers["Location"])

    def test_wrong_verifier_invalid_grant(self):
        """The code is burnt by the failed attempt: the controller answers instead of raising, so it commits."""
        code = self.get_code()
        self.assert_oauth_error(self.redeem(code, verifier=OTHER_VERIFIER), "invalid_grant")
        self.assert_oauth_error(self.redeem(code), "invalid_grant")

    def test_code_reused_invalid_grant(self):
        code = self.get_code()
        self.assertEqual(self.redeem(code).status_code, 200)
        self.assert_oauth_error(self.redeem(code), "invalid_grant")

    def test_wrong_redirect_or_client_at_token_invalid_grant(self):
        code = self.get_code()
        self.assert_oauth_error(self.redeem(code, redirect_uri=REDIRECT + "/x"), "invalid_grant")
        self.assert_oauth_error(self.redeem(self.get_code(), client_id="gmci_unknown"), "invalid_client")

    def test_resource_mismatch_rejected(self):
        self.get_code()  # an approved client: errors may be redirected to it
        query = self.redirect_query(self.get_authorize(resource="https://other.example/mcp"))
        self.assertEqual(query["error"], "invalid_target")
        self.assertEqual(query["state"], "st-123")
        self.assertNotIn("code", query)
        code = self.get_code()
        self.assert_oauth_error(self.redeem(code, resource="https://other.example/mcp"), "invalid_target")

    def test_resource_missing_defaults_and_is_normalized(self):
        """RFC 8707: no resource means this server; case and a trailing slash don't matter."""
        code = self.get_code(resource=None)
        self.assertEqual(self.env["gental.mcp.auth.code"].sudo().search(
            [("client_id", "=", self.oauth.id)], limit=1).resource, self.cfg.mcp_url())
        self.assertEqual(self.redeem(code, resource=None).status_code, 200)
        scheme, rest = self.cfg.mcp_url().split("://", 1)
        shouting = scheme.upper() + "://" + rest.upper().replace("/MCP", "/mcp") + "/"
        code = self.get_code(resource=shouting)
        resp = self.redeem(code, resource=self.cfg.mcp_url() + "/")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.mcp_status(resp.json()["access_token"]), 200)
        refreshed = self.token(grant_type="refresh_token", refresh_token=resp.json()["refresh_token"],
                               client_id=self.oauth.client_id, resource=shouting)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        # Still a different server: rejected.
        self.assertEqual(self.redirect_query(self.get_authorize(resource=self.cfg.mcp_url() + "/x"))["error"],
                         "invalid_target")

    def test_refresh_refused_after_the_url_changed(self):
        """Tokens bound to the old URL: refresh gives invalid_grant, so the client signs in again."""
        pair = self.pair()
        self.cfg.public_url = "https://moved.example.com"
        self.assert_oauth_error(self.refresh(pair["refresh_token"]), "invalid_grant")
        self.assertEqual(self.mcp_status(pair["access_token"]), 401)

    def test_redirect_uri_mismatch_shows_error_page_no_redirect(self):
        self.assert_error_page(self.get_authorize(redirect_uri="https://evil.example/cb"), 400, "doesn't match")
        self.assert_error_page(self.get_authorize(redirect_uri=REDIRECT + "/"), 400, "doesn't match")
        self.assert_error_page(self.get_authorize(client_id="gmci_unknown"), 400, "isn't registered")
        # A forged decision POST is refused the same way.
        page = self.get_authorize()
        csrf = CSRF_RE.search(page.text).group(1)
        data = dict(self.params(redirect_uri="https://evil.example/cb"), csrf_token=csrf, decision="approve")
        self.assert_error_page(self.url_open("/mcp/oauth/authorize", data=data, allow_redirects=False), 400,
                               "doesn't match")
        self.assertFalse(self.env["gental.mcp.auth.code"].sudo().search([("client_id", "=", self.oauth.id)]))

    def test_unapproved_dcr_client_errors_shown_not_redirected(self):
        """RFC 9700 4.11.2: a self-registered client nobody approved yet must not turn errors into redirects."""
        self.assert_error_page(self.get_authorize(resource="https://other.example/mcp"), 400, "isn't valid")
        self.assert_error_page(self.get_authorize(code_challenge_method="plain"), 400, "isn't valid")
        manual = self.env["gental.mcp.client"].sudo().create({
            "name": "Manual", "kind": "oauth", "client_id": "manual-client", "redirect_uris": REDIRECT,
        })
        self.assertEqual(self.redirect_query(self.get_authorize(client_id=manual.client_id, response_type="token"))
                         ["error"], "unsupported_response_type")
        self.get_code()
        self.assertEqual(self.redirect_query(self.get_authorize(resource="https://other.example/mcp"))["error"],
                         "invalid_target")

    def test_plain_pkce_method_rejected(self):
        self.get_code()
        plain = self.redirect_query(self.get_authorize(code_challenge_method="plain", code_challenge=VERIFIER))
        self.assertEqual(plain["error"], "invalid_request")
        self.assertEqual(self.redirect_query(self.get_authorize(code_challenge=None))["error"], "invalid_request")
        self.assertEqual(self.redirect_query(self.get_authorize(code_challenge_method=None))["error"],
                         "invalid_request")
        self.assertEqual(self.redirect_query(self.get_authorize(response_type="token"))["error"],
                         "unsupported_response_type")

    def test_post_without_csrf_refused(self):
        with mute_logger("odoo.http"):
            resp = self.url_open("/mcp/oauth/authorize", data=dict(self.params(), decision="approve"),
                                 allow_redirects=False)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(self.env["gental.mcp.auth.code"].sudo().search([("client_id", "=", self.oauth.id)]))

    def test_deny_redirects_access_denied(self):
        query = self.redirect_query(self.decide("deny"))
        self.assertEqual(query["error"], "access_denied")
        self.assertEqual(query["state"], "st-123")
        self.assertNotIn("code", query)
        log = self.env["gental.mcp.log"].sudo().search([("kind", "=", "auth"), ("client_id", "=", self.oauth.id)])
        self.assertEqual(log.mapped("status"), ["denied"])

    def test_user_not_in_connect_group_error_page(self):
        user = self._make_user("gmcp_plain", "Plain", ["base.group_user"])
        user.password = PASSWORD
        self.authenticate(user.login, PASSWORD)
        self.assert_error_page(self.get_authorize(), 403, "isn't allowed to connect")

    def test_authorize_get_rate_limited(self):
        for _i in range(20):
            self.assertEqual(self.get_authorize().status_code, 200)
        self.assert_error_page(self.get_authorize(), 429, "Too many attempts")

    def test_token_rate_limited(self):
        for _i in range(20):
            self.assert_oauth_error(self.token(grant_type="password"), "unsupported_grant_type")
        resp = self.token(grant_type="password")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"], "slow_down")

    def test_token_request_errors(self):
        self.assert_oauth_error(self.token(grant_type="password"), "unsupported_grant_type")
        self.assert_oauth_error(self.token(grant_type="authorization_code"), "invalid_request")
        self.assert_oauth_error(self.token(grant_type="refresh_token", client_id=self.oauth.client_id),
                                "invalid_request")
        resp = self.url_open("/mcp/oauth/token", data=json.dumps({"grant_type": "refresh_token"}),
                             headers={"Content-Type": "application/json"})
        self.assert_oauth_error(resp, "invalid_request")

    # -- refresh and revocation ---------------------------------------------------

    def test_refresh_rotates_and_old_refresh_reuse_revokes_family(self):
        first = self.pair()
        resp = self.refresh(first["refresh_token"])
        self.assertEqual(resp.status_code, 200, resp.text)
        second = resp.json()
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertEqual(self.mcp_status(second["access_token"]), 200)
        # Replaying the rotated token revokes the family (and the revocation is committed).
        self.assert_oauth_error(self.refresh(first["refresh_token"]), "invalid_grant")
        self.assertEqual(self.mcp_status(second["access_token"]), 401)
        self.assert_oauth_error(self.refresh(second["refresh_token"]), "invalid_grant")

    def test_refresh_by_other_client_invalid_grant(self):
        first = self.pair()
        other = self.env["gental.mcp.client"].register_dynamic("Other", [REDIRECT])
        resp = self.token(grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=other.client_id)
        self.assert_oauth_error(resp, "invalid_grant")
        self.assertEqual(self.refresh(first["refresh_token"]).status_code, 200)

    def test_archived_user_token_401(self):
        tokens = self.pair()
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)
        self.agent_user.active = False
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        self.assert_oauth_error(self.refresh(tokens["refresh_token"]), "invalid_grant")

    def test_removed_from_group_token_401(self):
        tokens = self.pair()
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)
        self.agent_user.write({user_groups_field(self.env): [(3, self.env.ref("gental_mcp.group_mcp_connect").id)]})
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        self.assert_oauth_error(self.refresh(tokens["refresh_token"]), "invalid_grant")

    def test_refresh_after_leaving_group_unseen_by_hooks_invalid_grant(self):
        """Membership removed by raw SQL (no write hook revokes anything): refresh still refuses, revoking the family."""
        tokens = self.pair()
        self.env.cr.execute(
            "DELETE FROM res_groups_users_rel WHERE uid = %s AND gid = %s",
            (self.agent_user.id, self.env.ref("gental_mcp.group_mcp_connect").id),
        )
        self.env.invalidate_all()
        self.assert_oauth_error(self.refresh(tokens["refresh_token"]), "invalid_grant")
        family = self.env["gental.mcp.token"].sudo().search([("user_id", "=", self.agent_user.id)])
        self.assertTrue(family)
        self.assertTrue(all(family.mapped("revoked")))

    def test_code_redeemed_after_user_left_group_invalid_grant(self):
        code = self.get_code()
        self.agent_user.write({user_groups_field(self.env): [(3, self.env.ref("gental_mcp.group_mcp_connect").id)]})
        self.assert_oauth_error(self.redeem(code), "invalid_grant")

    def test_revoke_endpoint_revokes_token(self):
        tokens = self.pair()
        resp = self.url_open(
            "/mcp/oauth/revoke", data={"token": tokens["access_token"], "token_type_hint": "access_token"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(self.mcp_status(tokens["access_token"]), 401)
        self.assert_oauth_error(self.refresh(tokens["refresh_token"]), "invalid_grant")
        # Unknown tokens and a token named under another client still answer 200.
        self.assertEqual(self.url_open("/mcp/oauth/revoke", data={"token": "gmat_unknown"}).status_code, 200)
        self.assertEqual(self.url_open("/mcp/oauth/revoke", data={"token_type_hint": "access_token"}).status_code, 200)
        other = self.pair()
        resp = self.url_open("/mcp/oauth/revoke", data={"token": other["refresh_token"], "client_id": "gmci_other"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.mcp_status(other["access_token"]), 200)

    def test_revoke_rate_limited_and_off_when_disabled(self):
        tokens = self.pair()
        self.cfg.enabled = False
        self.assertEqual(self.url_open("/mcp/oauth/revoke", data={"token": tokens["access_token"]}).status_code, 200)
        self.cfg.enabled = True
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)  # nothing was revoked while off
        for _i in range(19):
            self.assertEqual(self.url_open("/mcp/oauth/revoke", data={"token": "gmat_x"}).status_code, 200)
        resp = self.url_open("/mcp/oauth/revoke", data={"token": tokens["access_token"]})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self.mcp_status(tokens["access_token"]), 200)

    # -- consent page ---------------------------------------------------------------

    def test_consent_shows_metadata_host_and_models(self):
        cimd = self.env["gental.mcp.client"].sudo().create({
            "name": "Claude", "kind": "oauth", "registration": "cimd", "cimd": True, "client_id": CIMD_URL,
            "redirect_uris": "https://cb.example.net/callback", "cimd_fetched_at": fields.Datetime.now(),
        })
        self.env["gental.mcp.exposed.model"].create({
            "model_id": self.env["ir.model"]._get_id("res.partner"), "can_read": True, "can_write": True,
        })
        page = self.get_authorize(client_id=cimd.client_id, redirect_uri="https://cb.example.net/callback")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("cb.example.net", page.text)
        self.assertIn("client.example.com", page.text)
        self.assertIn("Contact", page.text)
        self.assertIn("Can make changes", page.text)

    def test_consent_page_arabic(self):
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["gental_mcp"], ["ar_001"], overwrite=True)
        self.agent_user.lang = "ar_001"
        self.authenticate(self.agent_user.login, PASSWORD)
        page = self.get_authorize()
        self.assertEqual(page.status_code, 200)
        self.assertIn('dir="rtl"', page.text)
        self.assertIn("اسمح", page.text)
        self.assertNotIn(">Allow<", page.text)
