import itertools
import json
import socket
import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.gental_mcp.compat import rpc_call_kw, set_param, user_groups_field
from odoo.addons.gental_mcp.models.mcp_client import is_metadata_url
from odoo.addons.gental_mcp.services import cimd, net_guard

from .common import PortableHttpCase

BASE = "https://odoo.example.com"
CIMD_URL = "https://client.example.com/oauth/client.json"
GETADDRINFO = "odoo.addons.gental_mcp.services.net_guard.socket.getaddrinfo"
SESSION = "odoo.addons.gental_mcp.services.net_guard.requests.Session"
NET_GUARD_TIME = "odoo.addons.gental_mcp.services.net_guard.time"
NET_GUARD_LOGGER = "odoo.addons.gental_mcp.services.net_guard"


def addrinfo(address, port=443):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


def public_addrinfo(host, port, *args, **kwargs):
    return addrinfo("93.184.216.34", port)


def fake_response(doc=None, status=200, chunks=None, headers=None):
    resp = MagicMock(status_code=status, headers=headers or {})
    resp.iter_content.return_value = chunks if chunks is not None else [json.dumps(doc).encode()]
    return resp


@tagged("post_install", "-at_install", "gental_mcp")
class TestOAuthDiscovery(PortableHttpCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.env["gental.mcp.config"].get()
        self.cfg.write({"public_url": BASE + "/", "oauth_enabled": True, "dcr_enabled": True})
        self.Client = self.env["gental.mcp.client"].sudo()

    def _get_json(self, path, status=200):
        resp = self.url_open(path)
        self.assertEqual(resp.status_code, status, resp.text)
        return resp.json()

    def _register(self, body):
        return self.url_open(
            "/mcp/oauth/register", data=json.dumps(body), headers={"Content-Type": "application/json"}
        )

    # -- discovery ----------------------------------------------------------------

    def test_prm_document(self):
        expected = {
            "resource": f"{BASE}/mcp",
            "authorization_servers": [BASE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Gental MCP (Odoo)",
        }
        self.assertEqual(self._get_json("/.well-known/oauth-protected-resource"), expected)
        self.assertEqual(self._get_json("/.well-known/oauth-protected-resource/mcp"), expected)

    def test_prm_404_when_oauth_disabled(self):
        self.cfg.oauth_enabled = False
        self.assertEqual(self.url_open("/.well-known/oauth-protected-resource").status_code, 404)
        self.assertEqual(self.url_open("/.well-known/oauth-protected-resource/mcp").status_code, 404)

    def test_discovery_uses_web_base_url_without_override(self):
        self.cfg.public_url = False
        set_param(self.env["ir.config_parameter"].sudo(), "web.base.url", "https://ERP.example.com/")
        self.assertEqual(self._get_json("/.well-known/oauth-protected-resource")["resource"],
                         "https://erp.example.com/mcp")
        self.assertEqual(self._get_json("/.well-known/oauth-authorization-server")["issuer"],
                         "https://erp.example.com")

    def test_discovery_and_registration_off_when_disabled(self):
        self.cfg.enabled = False
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
                     "/.well-known/oauth-authorization-server"):
            with self.subTest(path=path):
                self.assertEqual(self.url_open(path).status_code, 404)
        resp = self._register({"client_name": "Claude", "redirect_uris": ["https://claude.ai/cb"]})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.Client.search([("registration", "=", "dcr")]))

    def test_as_metadata_fields(self):
        doc = self._get_json("/.well-known/oauth-authorization-server")
        self.assertEqual(doc["issuer"], BASE)
        self.assertEqual(doc["authorization_endpoint"], f"{BASE}/mcp/oauth/authorize")
        self.assertEqual(doc["token_endpoint"], f"{BASE}/mcp/oauth/token")
        self.assertEqual(doc["registration_endpoint"], f"{BASE}/mcp/oauth/register")
        self.assertEqual(doc["revocation_endpoint"], f"{BASE}/mcp/oauth/revoke")
        self.assertEqual(doc["response_types_supported"], ["code"])
        self.assertEqual(doc["grant_types_supported"], ["authorization_code", "refresh_token"])
        self.assertEqual(doc["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(doc["token_endpoint_auth_methods_supported"], ["none"])
        self.assertIs(doc["client_id_metadata_document_supported"], True)
        self.assertEqual(self.url_open("/.well-known/openid-configuration").status_code, 404)

    def test_as_metadata_no_registration_when_dcr_off(self):
        self.cfg.dcr_enabled = False
        doc = self._get_json("/.well-known/oauth-authorization-server")
        self.assertNotIn("registration_endpoint", doc)
        self.assertEqual(doc["token_endpoint"], f"{BASE}/mcp/oauth/token")

    # -- dynamic client registration ---------------------------------------------------

    def test_dcr_creates_public_client(self):
        uris = ["https://claude.ai/api/mcp/auth_callback", "http://localhost:33418/callback", "http://[::1]:8080/cb"]
        resp = self._register({"client_name": "Claude", "redirect_uris": uris})
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertTrue(body["client_id"].startswith("gmci_"))
        self.assertIsInstance(body["client_id_issued_at"], int)
        self.assertEqual(body["redirect_uris"], uris)
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertEqual(body["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(body["response_types"], ["code"])
        client = self.Client.search([("client_id", "=", body["client_id"])])
        self.assertEqual(len(client), 1)
        self.assertEqual((client.kind, client.registration, client.name), ("oauth", "dcr", "Claude"))
        self.assertEqual(client.redirect_uri_list(), uris)
        self.assertFalse(client.key_hash)

    def test_dcr_rejects_http_non_loopback_redirect(self):
        before = self.Client.search_count([])
        for uris in (["http://evil.example.com/cb"], ["https://claude.ai/cb#frag"], ["https://u:p@claude.ai/cb"], []):
            resp = self._register({"client_name": "Bad", "redirect_uris": uris})
            self.assertEqual(resp.status_code, 400, uris)
            self.assertEqual(resp.json()["error"], "invalid_redirect_uri")
        resp = self._register({"client_name": "x" * 101, "redirect_uris": ["https://claude.ai/cb"]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_client_metadata")
        big = {"redirect_uris": ["https://claude.ai/cb"], "client_name": "Big", "pad": "x" * 70000}
        self.assertEqual(self._register(big).status_code, 413)
        chunked = self.url_open(  # no Content-Length: the stream itself is bounded
            "/mcp/oauth/register", data=(json.dumps(big).encode()[i:i + 4096] for i in range(0, 70100, 4096)),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(chunked.status_code, 413)
        self.assertEqual(self.Client.search_count([]), before)

    def test_dcr_403_when_disabled(self):
        self.cfg.dcr_enabled = False
        resp = self._register({"client_name": "Claude", "redirect_uris": ["https://claude.ai/cb"]})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.Client.search([("registration", "=", "dcr")]))

    def test_dcr_ignores_requested_client_auth(self):
        resp = self._register({
            "redirect_uris": ["https://claude.ai/cb"], "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code"], "response_types": ["code"],
        })
        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertNotIn("client_secret", body)
        self.assertEqual(body["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(body["client_name"], "MCP client")

    def test_cors(self):
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server"):
            with self.subTest(path=path):
                self.assertEqual(self.url_open(path).headers.get("Access-Control-Allow-Origin"), "*")
        for path in ("/mcp/oauth/register", "/.well-known/oauth-protected-resource"):
            with self.subTest(path=path):
                resp = self.opener.options(self.base_url() + path, headers={
                    "Origin": "https://inspector.example.com", "Access-Control-Request-Method": "POST",
                })
                self.assertEqual(resp.status_code, 204)
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
                self.assertNotIn("Access-Control-Allow-Credentials", resp.headers)
        resp = self._register({"redirect_uris": ["https://claude.ai/cb"]})
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

    def test_dcr_rate_limited_per_ip(self):
        Rate = type(self.env["gental.mcp.rate"])
        with patch.object(Rate, "hit", return_value=False) as hit:
            resp = self._register({"client_name": "Claude", "redirect_uris": ["https://claude.ai/cb"]})
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get("Retry-After"), "60")
        key, limit = hit.call_args_list[0].args[-2:]
        self.assertTrue(key.startswith("ip:register:"))
        self.assertEqual(limit, 20)

    # -- client ID metadata documents ------------------------------------------------

    def _patch(self, target, **kwargs):
        # Not patch.stopall: HttpCase keeps its own patchers running.
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _session(self, resp):
        session_cls = self._patch(SESSION)
        session_cls.return_value.get.return_value = resp
        return session_cls.return_value

    def test_cimd_resolves_and_validates(self):
        doc = {"client_id": CIMD_URL, "client_name": "Gental", "redirect_uris": ["https://gental.ai/oauth/callback"]}
        self._patch(GETADDRINFO, side_effect=public_addrinfo)
        session = self._session(fake_response(doc))
        client = cimd.resolve_client(self.env, CIMD_URL)
        self.assertEqual(session.get.call_count, 1)
        self.assertIs(session.get.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(session.get.call_args.kwargs["timeout"], 5)
        self.assertEqual((client.client_id, client.registration, client.name), (CIMD_URL, "cimd", "Gental"))
        self.assertTrue(client.cimd and client.cimd_fetched_at)
        self.assertEqual(client.redirect_uri_list(), ["https://gental.ai/oauth/callback"])
        # Cached for an hour: no second fetch.
        self.assertEqual(cimd.resolve_client(self.env, CIMD_URL), client)
        self.assertEqual(session.get.call_count, 1)
        # Stale: fetched again and updated in place.
        client.cimd_fetched_at = fields.Datetime.now() - timedelta(hours=2)
        doc["redirect_uris"] = ["https://gental.ai/oauth/callback2"]
        session.get.return_value = fake_response(doc)
        self.assertEqual(cimd.resolve_client(self.env, CIMD_URL), client)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(client.redirect_uri_list(), ["https://gental.ai/oauth/callback2"])
        # A non-URL client id must already be registered.
        with self.assertRaises(ValueError):
            cimd.resolve_client(self.env, "gmci_unknown")
        dcr = self.Client.register_dynamic("Claude", ["https://claude.ai/cb"])
        self.assertEqual(cimd.resolve_client(self.env, dcr.client_id), dcr)
        # A revoked metadata client is refused without a fetch.
        client.state = "revoked"
        with self.assertRaises(ValueError):
            cimd.resolve_client(self.env, CIMD_URL)
        self.assertEqual(session.get.call_count, 2)

    def test_cimd_confidential_auth_method_registered_as_public(self):
        """A document declaring client authentication is accepted as a public client, like DCR."""
        for method in ("private_key_jwt", "client_secret_basic"):
            url = f"https://client.example.com/oauth/{method}.json"
            doc = {"client_id": url, "client_name": "Conf", "redirect_uris": ["https://gental.ai/cb"],
                   "token_endpoint_auth_method": method}
            self._patch(GETADDRINFO, side_effect=public_addrinfo)
            self._session(fake_response(doc))
            client = cimd.resolve_client(self.env, url)
            with self.subTest(method=method):
                self.assertEqual((client.client_id, client.registration, client.state), (url, "cimd", "active"))
                self.assertFalse(client.key_hash)

    def test_cimd_rejects_mismatched_client_id(self):
        doc = {"client_id": "https://other.example.com/client.json", "redirect_uris": ["https://gental.ai/cb"]}
        self._patch(GETADDRINFO, side_effect=public_addrinfo)
        self._session(fake_response(doc))
        with self.assertRaisesRegex(ValueError, "does not match"):
            cimd.resolve_client(self.env, CIMD_URL)
        self.assertFalse(self.Client.search([("client_id", "=", CIMD_URL)]))

    def test_cimd_rejects_private_ip_host(self):
        url = "https://10.0.0.5/client.json"
        session = self._session(fake_response({}))
        with self.assertLogs(NET_GUARD_LOGGER, "INFO") as logs, self.assertRaisesRegex(ValueError, "^URL is not allowed$"):
            cimd.resolve_client(self.env, url)
        self.assertIn("non-public", logs.output[0])
        bad_urls = (
            "https://127.0.0.1/c.json", "https://[::1]/c.json", "https://[::ffff:192.168.1.1]/c.json",
            "https://[64:ff9b::a00:5]/c.json", "https://[2002:a00:5::1]/c.json", "https://169.254.169.254/c.json",
            "http://client.example.com/c.json", "https://user@client.example.com/c.json",
        )
        for bad in bad_urls:
            with self.subTest(url=bad), self.assertRaisesRegex(ValueError, "^URL is not allowed$"):
                net_guard.check_url(bad)
        session.get.assert_not_called()
        self.assertFalse(self.Client.search([("client_id", "=", url)]))

    def test_is_metadata_url(self):
        self.assertTrue(is_metadata_url(CIMD_URL))
        for bad in (
            "gmci_abc", "https://client.example.com", "https://client.example.com/", "http://client.example.com/c.json",
            "https://client.example.com/c.json#x", "https://client.example.com/a/../c.json",
            "https://client.example.com/./c.json", "https://u@client.example.com/c.json",
            "https://client.example.com/" + "a" * 2048,
        ):
            with self.subTest(url=bad):
                self.assertFalse(is_metadata_url(bad))

    # -- net_guard ---------------------------------------------------------------------

    def test_net_guard_connects_to_checked_ip(self):
        """DNS rebinding: a second lookup would say 10.0.0.5, but the request goes to the checked address."""
        resolver = self._patch(GETADDRINFO, side_effect=[addrinfo("93.184.216.34"), addrinfo("10.0.0.5")])
        session = self._session(fake_response({"ok": 1}))
        self.assertEqual(json.loads(net_guard.safe_get("https://client.example.com/c.json?x=1")), {"ok": 1})
        self.assertEqual(resolver.call_count, 1)
        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://93.184.216.34:443/c.json?x=1")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Host"], "client.example.com")
        self.assertEqual(session.get.call_args.kwargs["proxies"], {})
        self.assertIs(session.trust_env, False)
        adapter = session.mount.call_args.args[1]
        self.assertIsInstance(adapter, net_guard.PinnedHostAdapter)
        pool_kw = adapter.poolmanager.connection_pool_kw
        self.assertEqual((pool_kw["server_hostname"], pool_kw["assert_hostname"]), ("client.example.com",) * 2)

    def test_net_guard_tries_ipv4_first_then_other_addresses(self):
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700::1111", 443, 0, 0))
        self._patch(GETADDRINFO, return_value=[v6] + addrinfo("93.184.216.34"))
        session = self._session(None)
        session.get.side_effect = [requests.ConnectionError("refused"), fake_response({"ok": 1})]
        self.assertEqual(json.loads(net_guard.safe_get("https://client.example.com/c.json")), {"ok": 1})
        urls = [c.args[0] for c in session.get.call_args_list]
        self.assertEqual(urls, ["https://93.184.216.34:443/c.json", "https://[2606:4700::1111]:443/c.json"])

    def test_net_guard_brackets_ipv6_host_header(self):
        self._patch(GETADDRINFO, return_value=[
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700::1111", 8443, 0, 0)),
        ])
        session = self._session(fake_response({"ok": 1}))
        net_guard.safe_get("https://[2606:4700::1111]:8443/c.json")
        self.assertEqual(session.get.call_args.kwargs["headers"]["Host"], "[2606:4700::1111]:8443")

    def test_net_guard_deadline_covers_tls_handshake(self):
        """A server dripping one byte at a time during the handshake cannot outlast the deadline."""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        stop = threading.Event()

        def drip():
            conn, _addr = server.accept()
            with conn:
                while not stop.wait(0.2):
                    try:
                        conn.sendall(b"\x16")
                    except OSError:
                        return

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        self.addCleanup(server.close)
        self.addCleanup(stop.set)
        self._patch(GETADDRINFO, return_value=addrinfo("127.0.0.1", port))
        self._patch("odoo.addons.gental_mcp.services.net_guard._is_global", return_value=True)
        start = time.monotonic()
        with self.assertLogs(NET_GUARD_LOGGER, "INFO") as logs, self.assertRaisesRegex(ValueError, "^could not fetch the URL$"):
            net_guard.safe_get(f"https://drip.example.com:{port}/c.json", timeout=1)
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertIn("deadline", logs.output[-1])

    def test_net_guard_slow_drip_deadline(self):
        self._patch(GETADDRINFO, side_effect=public_addrinfo)
        clock = itertools.count(0, 1.0)  # one second passes per reading
        self._patch(NET_GUARD_TIME, **{"monotonic.side_effect": lambda: next(clock)})
        resp = fake_response(chunks=[b"x"] * 100)
        self._session(resp)
        with self.assertLogs(NET_GUARD_LOGGER, "INFO") as logs, self.assertRaisesRegex(ValueError, "^could not fetch the URL$"):
            net_guard.safe_get("https://client.example.com/c.json", timeout=5)
        self.assertIn("deadline", logs.output[0])
        resp.close.assert_called()

    def test_net_guard_rejects_large_content_length(self):
        self._patch(GETADDRINFO, side_effect=public_addrinfo)
        resp = fake_response({}, headers={"Content-Length": "100000"})
        self._session(resp)
        with self.assertRaises(ValueError), self.assertLogs(NET_GUARD_LOGGER, "INFO"):
            net_guard.safe_get("https://client.example.com/c.json")
        resp.iter_content.assert_not_called()

    # -- bookkeeping methods stay off RPC ---------------------------------------------------

    def test_registration_methods_not_rpc_callable(self):
        field = user_groups_field(self.env)
        portal = self.env["res.users"].with_context(no_reset_password=True).create({
            "login": "gmcp_portal_t6", "name": "Portal", field: [(6, 0, [self.env.ref("base.group_portal").id])],
        })
        agent = self.env["res.users"].with_context(no_reset_password=True).create({
            "login": "gmcp_agent_t6", "name": "Agent",
            field: [(6, 0, [self.env.ref("base.group_user").id, self.env.ref("gental_mcp.group_mcp_connect").id])],
        })
        calls = {
            "find_oauth_client": ["gmci_x"],
            "register_dynamic": ["Evil", ["https://evil.example.com/cb"]],
            "upsert_metadata_client": [CIMD_URL, "Evil", ["https://evil.example.com/cb"]],
        }
        for user in (agent, portal):
            for name, args in calls.items():
                with self.subTest(user=user.login, method=name), self.assertRaises(AccessError):
                    rpc_call_kw(self.env["gental.mcp.client"].with_user(user), name, args, {})
        self.assertFalse(self.Client.search([("name", "=", "Evil")]))

    def test_registration_methods_validate_input(self):
        with self.assertRaises(ValueError):
            self.Client.register_dynamic("Evil", ["http://evil.example.com/cb"])
        with self.assertRaises(ValueError):
            self.Client.upsert_metadata_client("gmci_abc", "Evil", ["https://evil.example.com/cb"])
        dcr = self.Client.register_dynamic("Claude", ["https://claude.ai/cb"])
        manual = self.Client.create({"name": "Manual", "kind": "oauth", "client_id": "https://manual.example.com/c.json",
                                     "redirect_uris": "https://manual.example.com/cb"})
        for client_id in (dcr.client_id, manual.client_id):
            with self.subTest(client_id=client_id), self.assertRaises(ValueError):
                self.Client.upsert_metadata_client(client_id, "Evil", ["https://evil.example.com/cb"])
        self.assertEqual(manual.redirect_uri_list(), ["https://manual.example.com/cb"])
        self.assertEqual(dcr.redirect_uri_list(), ["https://claude.ai/cb"])
