import io
import json
import socket
from unittest.mock import MagicMock, patch

import requests
import urllib3

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from odoo.addons.gental_mcp.services import cimd, field_guard, net_guard

from .common import GentalMcpCase, tracking_text

GETADDRINFO = "odoo.addons.gental_mcp.services.net_guard.socket.getaddrinfo"
SESSION = "odoo.addons.gental_mcp.services.net_guard.requests.Session"
NET_GUARD_LOGGER = "odoo.addons.gental_mcp.services.net_guard"
HOOK_URL = "https://hooks.example.com/gental"
CATALOG = {
    "invoice.posted", "invoice.paid", "order.created", "order.confirmed", "order.cancelled", "delivery.done",
    "lead.created", "lead.stage_changed", "contact.created", "record.created", "record.updated",
}


def addrinfo(address, port=443):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


def public_addrinfo(host, port, *args, **kwargs):
    return addrinfo("93.184.216.34", port)


def fake_response(status=200, chunks=None, headers=None):
    resp = MagicMock(status_code=status, headers=headers or {})
    resp.iter_content.return_value = chunks if chunks is not None else [b'{"ok": true}']
    return resp


class WebhookCase(GentalMcpCase):
    def setUp(self):
        super().setUp()
        self.resolver = self._patch(GETADDRINFO, side_effect=public_addrinfo)

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()


@tagged("post_install", "-at_install", "gental_mcp")
class TestWebhookSubscription(WebhookCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Event = cls.env["gental.mcp.webhook.event"]
        cls.Sub = cls.env["gental.mcp.webhook.subscription"].with_user(cls.manager_user)

    def event(self, code):
        return self.env.ref(f"gental_mcp.webhook_event_{code.replace('.', '_')}")

    def make_sub(self, **vals):
        return self.Sub.create({
            "name": "ERP sync",
            "url": HOOK_URL,
            "event_ids": [(6, 0, self.event("contact.created").ids)],
            **vals,
        })

    def expose(self, model, hidden=()):
        ir_model = self.env["ir.model"]._get(model)
        fields_ = self.env["ir.model.fields"].search([("model_id", "=", ir_model.id), ("name", "in", list(hidden))])
        exposed = self.env["gental.mcp.exposed.model"].sudo().search([("model_id", "=", ir_model.id)])
        vals = {"hidden_field_ids": [(6, 0, fields_.ids)], "active": True}
        if exposed:
            exposed.write(vals)
            return exposed
        return exposed.create({"model_id": ir_model.id, **vals})

    def test_catalog_loaded(self):
        events = self.Event.search([])
        self.assertEqual(set(events.mapped("code")), CATALOG)
        posted = self.event("invoice.posted")
        self.assertEqual((posted.model, posted.kind, posted.state_field), ("account.move", "transition", "state"))
        self.assertEqual(json.loads(posted.target_values), ["posted"])
        self.assertIn("amount_residual", json.loads(posted.payload_fields))
        self.assertEqual(json.loads(self.event("invoice.paid").target_values), ["paid", "in_payment"])
        self.assertEqual(json.loads(self.event("lead.stage_changed").target_values), [])
        generic = events.filtered("generic")
        self.assertEqual(set(generic.mapped("code")), {"record.created", "record.updated"})
        self.assertEqual(generic.mapped("model"), [False, False])
        self.assertTrue(all(generic.mapped("available")))
        self.assertTrue(self.event("contact.created").available)
        # Event names are translated.
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["gental_mcp"], ["ar_001"], overwrite=True)
        self.env.invalidate_all()
        order_created = self.event("order.created")
        self.assertEqual(order_created.with_context(lang="en_US").name, "Order created")
        self.assertNotEqual(order_created.with_context(lang="ar_001").name, "Order created")

    def test_events_unavailable_without_module(self):
        if "account.move" in self.env:
            self.skipTest("account is installed")
        posted = self.event("invoice.posted")
        self.assertFalse(posted.available)
        available = self.Event.search([("available", "=", True)])
        self.assertNotIn(posted, available)
        self.assertIn(self.event("contact.created"), available)
        self.assertIn(self.event("record.created"), available)
        self.assertEqual(self.Event.search([("available", "=", False)]), self.Event.search([]) - available)

    def test_events_required_on_create(self):
        with self.assertRaises(ValidationError):
            self.Sub.create({"name": "No events", "url": HOOK_URL})

    def test_url_is_stored_stripped(self):
        sub = self.make_sub(url="  https://hooks.example.com/gental \n")
        self.assertEqual(sub.url, HOOK_URL)
        sub.url = " https://hooks.example.com/other "
        self.assertEqual(sub.url, "https://hooks.example.com/other")

    def test_url_must_be_https(self):
        with self.assertRaises(ValidationError):
            self.make_sub(url="http://example.com/h")
        with self.assertRaises(ValidationError):
            self.make_sub(url="https://user:pw@example.com/h")

    def test_url_private_ip_rejected(self):
        self.resolver.side_effect = lambda host, port, *a, **k: addrinfo("10.0.0.5", port)
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"), self.assertRaises(ValidationError):
            self.make_sub(url="https://internal.example.com/h")
        self.resolver.side_effect = public_addrinfo
        sub = self.make_sub()
        self.resolver.side_effect = lambda host, port, *a, **k: addrinfo("127.0.0.1", port)
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"), self.assertRaises(ValidationError):
            sub.url = "https://rebound.example.com/h"

    def test_filter_honours_hidden_fields_of_archived_rows(self):
        exposed = self.expose("res.partner", hidden=["ref"])
        exposed.active = False
        with self.assertRaises(ValidationError):
            self.make_sub(filter_domain="[('ref', '=', 'x')]")
        self.assertEqual(self.Sub._hidden_fields("res.partner"), frozenset({"ref"}))

    def test_generic_event_requires_exposed_models(self):
        generic = self.event("record.created")
        with self.assertRaises(ValidationError):
            self.make_sub(event_ids=[(6, 0, generic.ids)])
        exposed = self.expose("res.partner")
        sub = self.make_sub(event_ids=[(6, 0, generic.ids)], exposed_model_ids=[(6, 0, exposed.ids)])
        self.assertEqual(sub.exposed_model_ids, exposed)
        # Exposed models only make sense with a generic event.
        with self.assertRaises(ValidationError):
            self.make_sub(exposed_model_ids=[(6, 0, exposed.ids)])
        with self.assertRaises(ValidationError):
            self.make_sub(event_ids=[(5,)])

    def test_filter_domain_validated(self):
        self.expose("res.partner", hidden=["ref"])
        sub = self.make_sub(filter_domain="[('is_company', '=', True), ('country_id', 'in', [1, 2])]")
        self.assertTrue(sub)
        bad_domains = (
            "[('ref', '=', 'x')]",  # hidden on the exposed model
            "[('country_id', 'ilike', 'Saudi')]",  # relational: ids only
            "[('country_id.name', '=', 'Saudi Arabia')]",  # paths
            "[('display_name', 'ilike', 'x')]",  # not stored
            "[('nope', '=', 1)]",
            "not a domain",
            "{'a': 1}",
        )
        for bad in bad_domains:
            with self.subTest(domain=bad), self.assertRaises(ValidationError):
                self.make_sub(filter_domain=bad)
        # A filter needs one model shared by every event.
        generic = self.event("record.created")
        exposed = self.expose("res.users")
        with self.assertRaises(ValidationError):
            self.make_sub(
                event_ids=[(6, 0, (self.event("contact.created") | generic).ids)],
                exposed_model_ids=[(6, 0, exposed.ids)],
                filter_domain="[('id', '=', 1)]",
            )
        # Without a filter, mixing models is fine.
        self.make_sub(
            event_ids=[(6, 0, (self.event("contact.created") | generic).ids)], exposed_model_ids=[(6, 0, exposed.ids)]
        )

    def test_manager_runs_as_themselves_only(self):
        other_manager = self._make_user("gmcp_manager2", "MCP Manager 2", ["base.group_user", "gental_mcp.group_mcp_manager"])
        with self.assertRaises(ValidationError):
            self.make_sub(run_as_user_id=other_manager.id)
        with self.assertRaises(ValidationError):
            self.make_sub(run_as_user_id=self.agent_user.id)
        mine = self.make_sub()
        with self.assertRaises(ValidationError):
            mine.run_as_user_id = self.agent_user
        # An administrator may choose anyone; a manager then can't redirect that subscription elsewhere.
        admin = self._make_user("gmcp_admin", "MCP Admin", ["base.group_user", "base.group_system"])
        theirs = self.Sub.with_user(admin).create({
            "name": "Admin hook", "url": HOOK_URL, "run_as_user_id": self.agent_user.id,
            "event_ids": [(6, 0, self.event("contact.created").ids)],
        })
        self.assertEqual(theirs.run_as_user_id, self.agent_user)
        with self.assertRaises(ValidationError):
            theirs.with_user(self.manager_user).url = "https://attacker.example.com/h"

    def admin_and_hook(self, **vals):
        admin = self._make_user("gmcp_admin", "MCP Admin", ["base.group_user", "base.group_system"])
        hook = self.Sub.with_user(admin).create({
            "name": "Admin hook", "url": HOOK_URL, "run_as_user_id": self.agent_user.id,
            "event_ids": [(6, 0, self.event("contact.created").ids)], **vals,
        })
        return admin, hook

    def test_manager_cannot_change_a_subscription_running_as_someone_else(self):
        # Otherwise a manager could widen what it sends (events, models, filter) and read it in the Outbox.
        _admin, theirs = self.admin_and_hook()
        exposed = self.expose("res.partner")
        as_manager = theirs.with_user(self.manager_user)
        changes = (
            {"name": "Renamed"},
            {"event_ids": [(6, 0, self.event("record.created").ids)], "exposed_model_ids": [(6, 0, exposed.ids)]},
            {"filter_domain": "[('id', '>', 0)]"},
            {"active": False},
        )
        for vals in changes:
            with self.subTest(vals=vals), self.assertRaisesRegex(ValidationError, "Only administrators can change"):
                as_manager.write(vals)
        with self.assertRaisesRegex(ValidationError, "Only administrators can change"):
            as_manager.action_rotate_secret()
        with self.assertRaisesRegex(ValidationError, "Only administrators can change"):
            as_manager.unlink()
        self.assertEqual(theirs.sudo().name, "Admin hook")

    def test_managers_see_only_their_own_subscriptions_and_deliveries(self):
        admin, theirs = self.admin_and_hook()
        mine = self.make_sub()
        Delivery = self.env["gental.mcp.webhook.delivery"]
        theirs_sent = Delivery.enqueue(theirs, "contact.created", "res.partner", 1, {"name": "Secret client"})
        mine_sent = Delivery.enqueue(mine, "contact.created", "res.partner", 2, {"name": "Mine"})
        as_manager = self.Sub.with_user(self.manager_user)
        self.assertEqual(as_manager.with_context(active_test=False).search([]), mine)
        self.assertEqual(Delivery.with_user(self.manager_user).search([]), mine_sent)
        for record, field in ((theirs, "secret"), (theirs, "url"), (theirs_sent, "body")):
            with self.subTest(field=field), self.assertRaises(AccessError):
                record.with_user(self.manager_user).read([field])
        self.assertIn("Secret client", theirs_sent.with_user(admin).body)
        self.assertTrue(theirs.with_user(admin).secret)
        self.assertEqual(as_manager.with_user(admin).search([("id", "in", (mine | theirs).ids)]), mine | theirs)
        # The Configuration > Webhooks list, as the manager opens it.
        config = self.env["gental.mcp.config"].get().with_user(self.manager_user)
        self.assertEqual(config.webhook_subscription_ids, mine)

    def test_filter_needs_the_editors_field_access_too(self):
        # The run-as user may filter on ``ref``; the administrator editing the subscription may not.
        admin = self._make_user("gmcp_admin", "MCP Admin", ["base.group_user", "base.group_system"])
        real = field_guard.filterable_fields

        def filterable(env, exposed):
            allowed = real(env, exposed)
            return {k: v for k, v in allowed.items() if k != "ref"} if env.uid == admin.id else allowed

        vals = {"name": "Hook", "url": HOOK_URL, "run_as_user_id": self.agent_user.id,
                "event_ids": [(6, 0, self.event("contact.created").ids)], "filter_domain": "[('ref', '=', 'x')]"}
        with patch.object(field_guard, "filterable_fields", side_effect=filterable):
            with self.assertRaises(ValidationError):
                self.Sub.with_user(admin).create(vals)
            self.assertTrue(self.Sub.with_user(admin).create(dict(vals, filter_domain="[('name', '=', 'x')]")))

    def test_url_is_not_tracked_and_listed_by_host(self):
        sub = self.make_sub(url="https://hooks.example.com/gental?token=s3cr3t")
        sub.url = "https://hooks.example.com/other?token=0th3r"
        self.env.flush_all()
        messages = sub.sudo().message_ids
        tracked = messages.tracking_value_ids.mapped("new_value_char") if "tracking_value_ids" in messages._fields else []
        text = str(messages.mapped("body")) + str(tracked)
        self.assertNotIn("s3cr3t", text)
        self.assertNotIn("0th3r", text)
        self.assertEqual(sub.url_host, "hooks.example.com")
        self.assertNotIn("url", self.env["gental.mcp.webhook.subscription"].with_user(self.agent_user).fields_get())
        self.assertIn("url", self.Sub.fields_get())

    def test_payload_fields_offered_for_the_chosen_models(self):
        exposed = self.expose("res.partner")
        sub = self.make_sub(event_ids=[(6, 0, self.event("record.created").ids)], exposed_model_ids=[(6, 0, exposed.ids)])
        # Read with sudo: 20.0 shows ir.model records only to access-rights administrators.
        self.assertEqual(sub.sudo().exposed_ir_model_ids, exposed.model_id)

    def test_run_as_must_be_connect_user(self):
        sub = self.make_sub()
        self.assertEqual(sub.run_as_user_id, self.manager_user)
        plain = self._make_user("gmcp_plain", "Plain", ["base.group_user"])
        admin = self._make_user("gmcp_admin", "MCP Admin", ["base.group_user", "base.group_system"])
        as_admin = self.Sub.with_user(admin)
        vals = {"name": "Hook", "url": HOOK_URL, "event_ids": [(6, 0, self.event("contact.created").ids)]}
        with self.assertRaises(ValidationError):
            as_admin.create({**vals, "run_as_user_id": plain.id})
        self.assertTrue(as_admin.create({**vals, "run_as_user_id": self.agent_user.id}))
        self.agent_user.active = False
        with self.assertRaises(ValidationError):
            as_admin.create({**vals, "run_as_user_id": self.agent_user.id})

    def test_secret_generated_and_rotate_changes_it(self):
        sub = self.make_sub()
        first = sub.secret
        self.assertTrue(first.startswith("gmwh_"))
        self.assertNotEqual(self.make_sub().secret, first)
        sub.action_rotate_secret()
        self.assertTrue(sub.secret.startswith("gmwh_"))
        self.assertNotEqual(sub.secret, first)
        messages = sub.sudo().message_ids
        self.assertIn("rotated", str(messages.mapped("body")))
        bodies = str(messages.mapped("body")) + tracking_text(messages)
        self.assertNotIn(first, bodies)
        self.assertNotIn(sub.secret, bodies)
        self.assertIn("gmwh_", sub.copy().secret)
        self.assertNotEqual(sub.copy().secret, sub.secret)

    def test_secret_and_subscriptions_are_manager_only(self):
        sub = self.make_sub()
        with self.assertRaises(AccessError):
            sub.with_user(self.agent_user).read(["name"])
        with self.assertRaises(AccessError):
            self.Event.with_user(self.agent_user).search([])
        self.assertNotIn("secret", self.env["gental.mcp.webhook.subscription"].with_user(self.agent_user).fields_get())
        self.assertIn("secret", self.Sub.fields_get())

    def test_cimd_uses_net_guard(self):
        self.assertIs(cimd.net_guard, net_guard)
        session_cls = self._patch(SESSION)
        self.resolver.side_effect = lambda host, port, *a, **k: addrinfo("10.0.0.5", port)
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"), self.assertRaisesRegex(ValueError, "^URL is not allowed$"):
            cimd.resolve_client(self.env, "https://10.0.0.5/client.json")
        session_cls.return_value.get.assert_not_called()


@tagged("post_install", "-at_install", "gental_mcp")
class TestNetGuardPost(WebhookCase):
    def _session(self, resp):
        session_cls = self._patch(SESSION)
        session_cls.return_value.post.return_value = resp
        return session_cls.return_value

    def test_safe_post_sends_to_checked_ip_without_redirects(self):
        self.resolver.side_effect = [addrinfo("93.184.216.34"), addrinfo("10.0.0.5")]
        session = self._session(fake_response(status=302, headers={"Location": "https://10.0.0.5/"}))
        resp = net_guard.safe_post(
            HOOK_URL + "?x=1", b'{"a": 1}', {"Content-Type": "application/json", "Host": "evil.example"}, timeout=10
        )
        self.assertEqual(resp.status_code, 302)  # returned as is, never followed
        self.assertEqual(self.resolver.call_count, 1)
        call = session.post.call_args
        self.assertEqual(call.args[0], "https://93.184.216.34:443/gental?x=1")
        self.assertEqual(call.kwargs["data"], b'{"a": 1}')
        self.assertEqual(call.kwargs["headers"]["Host"], "hooks.example.com")
        self.assertEqual(call.kwargs["headers"]["Content-Type"], "application/json")
        self.assertIs(call.kwargs["allow_redirects"], False)
        self.assertEqual(call.kwargs["proxies"], {})
        self.assertEqual(call.kwargs["timeout"], 10)
        self.assertIs(session.trust_env, False)
        self.assertIsInstance(session.mount.call_args.args[1], net_guard.PinnedHostAdapter)
        session.get.assert_not_called()

    def test_safe_post_reads_a_capped_body(self):
        real = requests.Response()
        real.status_code = 200
        real.raw = io.BytesIO(b"x" * 40000 + b"y" * 40000 + b"z" * 40000)
        self._session(real)
        resp = net_guard.safe_post(HOOK_URL, b"{}", {}, timeout=10)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"x" * 40000 + b"y" * (net_guard.POST_MAX_RESPONSE_BYTES - 40000))
        self.assertTrue(real.raw.closed)

    def test_safe_post_refuses_private_targets_before_sending(self):
        session = self._session(fake_response())
        self.resolver.side_effect = lambda host, port, *a, **k: addrinfo("169.254.169.254", port)
        for url in (HOOK_URL, "http://hooks.example.com/gental", "https://u@hooks.example.com/gental"):
            with self.subTest(url=url), self.assertLogs(NET_GUARD_LOGGER, "INFO"), \
                    self.assertRaisesRegex(ValueError, "^URL is not allowed$"):
                net_guard.safe_post(url, b"{}", {}, timeout=10)
        session.post.assert_not_called()

    def test_safe_post_retries_next_address_only_before_sending(self):
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2606:4700::1111", 443, 0, 0))
        self.resolver.side_effect = None
        self.resolver.return_value = [v6] + addrinfo("93.184.216.34")
        session = self._session(None)
        refused = requests.ConnectionError(urllib3.exceptions.MaxRetryError(
            None, "/", urllib3.exceptions.NewConnectionError(None, "refused")))
        session.post.side_effect = [refused, fake_response()]
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"):
            self.assertEqual(net_guard.safe_post(HOOK_URL, b"{}", {}, timeout=10).status_code, 200)
        self.assertEqual(session.post.call_count, 2)
        session.post.reset_mock()
        session.post.side_effect = [requests.ConnectTimeout("slow connect"), fake_response()]
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"):
            net_guard.safe_post(HOOK_URL, b"{}", {}, timeout=10)
        self.assertEqual(session.post.call_count, 2)
        # Dropped after the request may have gone out: no second send.
        session.post.reset_mock()
        session.post.side_effect = [requests.ConnectionError("connection reset by peer"), fake_response()]
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"), self.assertRaisesRegex(ValueError, "^could not reach the URL$"):
            net_guard.safe_post(HOOK_URL, b"{}", {}, timeout=10)
        self.assertEqual(session.post.call_count, 1)

    def test_safe_post_connection_failure_is_generic(self):
        session = self._session(None)
        session.post.side_effect = requests.ConnectionError("refused by 93.184.216.34")
        with self.assertLogs(NET_GUARD_LOGGER, "INFO") as logs, \
                self.assertRaisesRegex(ValueError, "^could not reach the URL$"):
            net_guard.safe_post(HOOK_URL, b"{}", {}, timeout=10)
        self.assertIn("refused", logs.output[-1])

    def test_logs_show_only_scheme_and_host(self):
        session = self._session(None)
        session.post.side_effect = requests.ConnectionError(
            "HTTPSConnectionPool(host='93.184.216.34', port=443): Max retries exceeded with url: /gental/s3cr3t?token=t0k3n"
        )
        with self.assertLogs(NET_GUARD_LOGGER, "INFO") as logs, self.assertRaises(ValueError):
            net_guard.safe_post(HOOK_URL + "/s3cr3t?token=t0k3n", b"{}", {}, timeout=10)
        self.resolver.side_effect = lambda host, port, *a, **k: addrinfo("10.0.0.5", port)
        with self.assertLogs(NET_GUARD_LOGGER, "INFO") as refused, self.assertRaises(ValueError):
            net_guard.check_url("https://internal.example.com/private/path?key=k3y")
        output = "\n".join(logs.output + refused.output)
        for secret in ("s3cr3t", "t0k3n", "k3y", "/private"):
            self.assertNotIn(secret, output)
        self.assertIn("https://hooks.example.com", output)
        self.assertIn("https://internal.example.com", output)
        self.assertEqual(net_guard.log_url("https://h.example.com:8443/a?b=c"), "https://h.example.com:8443")
