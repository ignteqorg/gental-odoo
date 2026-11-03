import calendar
import json
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

import requests
from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, Command, api, sql_db
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.gental_mcp.services import webhook_sender

from .common import freeze_time
from .test_webhook_subscription import (
    GETADDRINFO, HOOK_URL, NET_GUARD_LOGGER, SESSION, WebhookCase, addrinfo, fake_response, public_addrinfo,
)

DELIVERY_LOGGER = "odoo.addons.gental_mcp.models.webhook_delivery"
DELIVERY_CLOCK = "odoo.addons.gental_mcp.models.webhook_delivery.time.monotonic"


def answer(status=200, body=b"", headers=None):
    """A receiver's answer as ``net_guard.safe_post`` sees it (``content`` is what it kept)."""
    resp = fake_response(status, chunks=[body] if body else [], headers=headers)
    resp.content = body
    return resp


@tagged("post_install", "-at_install", "gental_mcp")
class TestWebhookSender(WebhookCase):
    """Signing, sending, backoff and retries of the webhook outbox (plan 3, task 3)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Delivery = cls.env["gental.mcp.webhook.delivery"]
        # The subscription runs as the agent, so only a system administrator may press its buttons.
        cls.admin_user = cls._make_user("gmcp_wh_admin", "MCP Admin", ["base.group_user", "base.group_system"])
        with patch(GETADDRINFO, side_effect=public_addrinfo):
            cls.sub = cls.env["gental.mcp.webhook.subscription"].create({
                "name": "ERP sync",
                "url": HOOK_URL,
                "event_ids": [Command.set(cls.env.ref("gental_mcp.webhook_event_contact_created").ids)],
                "run_as_user_id": cls.agent_user.id,
            })

    def setUp(self):
        super().setUp()
        self.session = self._patch(SESSION).return_value
        self.session.post.return_value = answer(200)

    # -- helpers ------------------------------------------------------------------

    def queue(self, data=None, sub=None):
        """A queued delivery, due since before any frozen clock the tests use."""
        delivery = self.Delivery.enqueue(
            sub or self.sub, "contact.created", "res.partner", 7, data if data is not None else {"name": "Test"},
        ).sudo()
        delivery.next_attempt_at = datetime(2026, 1, 1)
        return delivery

    def dispatch(self, delivery=None):
        Delivery = self.Delivery.sudo()
        count = Delivery.dispatch() if delivery is None else delivery._dispatch_now()
        self.env.invalidate_all()
        return count

    def sent(self, index=-1):
        call = self.session.post.call_args_list[index]
        return call.kwargs["data"], call.kwargs["headers"]

    # -- signing and headers ------------------------------------------------------

    def test_headers_exact(self):
        delivery = self.queue()
        with freeze_time("2026-11-02 10:00:00"):
            self.assertEqual(self.dispatch(), 1)
        body, headers = self.sent()
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-Gental-Event"], "contact.created")
        self.assertEqual(headers["X-Gental-Delivery"], delivery.delivery_uuid)
        timestamp = calendar.timegm(datetime(2026, 11, 2, 10, 0, 0).timetuple())
        self.assertEqual(headers["X-Gental-Timestamp"], str(timestamp))
        self.assertEqual(headers["X-Gental-Signature"], webhook_sender.sign(self.sub.sudo().secret, timestamp, body))
        self.assertRegex(headers["X-Gental-Signature"], r"^[0-9a-f]{64}$")
        self.assertEqual(body, delivery.body.encode("utf-8"))
        self.assertEqual(self.session.post.call_args.kwargs["timeout"], webhook_sender.TIMEOUT_SECONDS)

    def test_sign_matches_hmac_over_timestamp_dot_body(self):
        import hashlib
        import hmac

        expected = hmac.new(b"gmwh_k", b"1700000000.{\"a\":1}", hashlib.sha256).hexdigest()
        self.assertEqual(webhook_sender.sign("gmwh_k", 1700000000, b'{"a":1}'), expected)

    def test_signature_over_utf8_arabic_body(self):
        import hashlib
        import hmac

        self.queue({"name": "مؤسسة الريان"})
        self.dispatch()
        body, headers = self.sent()
        self.assertIn("مؤسسة الريان".encode("utf-8"), body)
        self.assertEqual(json.loads(body.decode("utf-8"))["data"]["name"], "مؤسسة الريان")
        expected = hmac.new(
            self.sub.sudo().secret.encode("utf-8"), headers["X-Gental-Timestamp"].encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["X-Gental-Signature"], expected)

    # -- outcomes -----------------------------------------------------------------

    def test_2xx_marks_delivered(self):
        delivery = self.queue()
        self.session.post.return_value = answer(204)
        self.dispatch()
        self.assertEqual(delivery.state, "delivered")
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.last_status_code, 204)
        self.assertFalse(delivery.last_error)
        self.assertTrue(delivery.delivered_at)
        self.dispatch()  # delivered rows are not sent again
        self.assertEqual(self.session.post.call_count, 1)

    def test_failure_backoff_schedule(self):
        delivery = self.queue()
        self.session.post.return_value = answer(500, b"oops")
        clock = datetime(2026, 11, 2, 10, 0, 0)
        delays = []
        with self.assertLogs(DELIVERY_LOGGER, "INFO"):
            for attempt in range(1, webhook_sender.MAX_ATTEMPTS + 1):
                with freeze_time(clock):
                    self.assertEqual(self.dispatch(), 1)
                self.assertEqual(delivery.attempts, attempt)
                if attempt < webhook_sender.MAX_ATTEMPTS:
                    self.assertEqual(delivery.state, "queued")
                    delays.append(int((delivery.next_attempt_at - clock).total_seconds()))
                    with freeze_time(delivery.next_attempt_at - timedelta(seconds=1)):
                        self.assertEqual(self.dispatch(), 0)  # not due yet
                    clock = delivery.next_attempt_at
        self.assertEqual(tuple(delays), webhook_sender.BACKOFF_SECONDS)
        self.assertEqual(delivery.state, "failed")
        self.assertEqual(delivery.last_status_code, 500)
        self.assertEqual(delivery.last_error, "HTTP 500: oops")
        self.assertEqual(self.session.post.call_count, webhook_sender.MAX_ATTEMPTS)
        with freeze_time(clock + timedelta(days=2)):
            self.assertEqual(self.dispatch(), 0)  # failed is final until Retry now
        self.assertEqual(self.sub.failing_count, 1)

    def test_response_excerpt_is_capped_plain_text(self):
        delivery = self.queue()
        self.session.post.return_value = answer(503, b"<b>down</b>\r\n\x1b[31m" + b"x" * 3000)
        with self.assertLogs(DELIVERY_LOGGER, "INFO"):
            self.dispatch()
        self.assertTrue(delivery.last_error.startswith("HTTP 503: <b>down</b> [31mxxx"))
        self.assertLessEqual(len(delivery.last_error), webhook_sender.ERROR_MAX_CHARS)
        self.assertNotIn("\n", delivery.last_error)

    def test_redirect_not_followed_counts_as_failure(self):
        delivery = self.queue()
        self.session.post.return_value = answer(302, headers={"Location": "https://other.example.com/"})
        with self.assertLogs(DELIVERY_LOGGER, "INFO"):
            self.dispatch()
        self.assertEqual(self.session.post.call_count, 1)
        self.assertIs(self.session.post.call_args.kwargs["allow_redirects"], False)
        self.assertEqual((delivery.state, delivery.attempts, delivery.last_status_code), ("queued", 1, 302))
        self.assertIn("redirect", delivery.last_error)

    def test_private_ip_at_send_time_blocked(self):
        delivery = self.queue()  # the URL was public when the subscription was saved
        self.resolver.side_effect = lambda host, port, *a, **k: addrinfo("127.0.0.1", port)
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"), self.assertLogs(DELIVERY_LOGGER, "INFO"):
            self.dispatch()
        self.session.post.assert_not_called()
        self.assertEqual((delivery.state, delivery.attempts), ("queued", 1))
        self.assertEqual(delivery.last_error, "URL is not allowed")
        self.assertFalse(delivery.last_status_code)

    def test_timeout_is_failure(self):
        delivery = self.queue()
        self.session.post.side_effect = requests.ReadTimeout("read timed out")
        with self.assertLogs(NET_GUARD_LOGGER, "INFO"), self.assertLogs(DELIVERY_LOGGER, "INFO"):
            self.dispatch()
        self.assertEqual((delivery.state, delivery.attempts), ("queued", 1))
        self.assertEqual(delivery.last_error, "could not reach the URL")

    def test_secret_and_body_not_logged(self):
        self.queue({"name": "سر العميل"})
        self.session.post.return_value = answer(500)
        with self.assertLogs(level="DEBUG") as logs:
            self.dispatch()
        text = "\n".join(logs.output)
        self.assertNotIn(self.sub.sudo().secret, text)
        self.assertNotIn("سر العميل", text)

    # -- run-as user and subscription state ---------------------------------------

    def test_disconnected_run_as_keeps_queued_with_warning(self):
        delivery = self.queue()
        self.agent_user.active = False
        self.dispatch()
        self.session.post.assert_not_called()
        self.assertEqual((delivery.state, delivery.attempts), ("queued", 0))
        self.assertGreater(delivery.next_attempt_at, datetime.now())
        self.assertEqual(self.sub.warning_code, "not_connected")
        # Allowed again: sent at the next due time, and the warning clears.
        self.agent_user.active = True
        with freeze_time(delivery.next_attempt_at):
            self.dispatch()
        self.assertEqual(delivery.state, "delivered")
        self.assertFalse(self.sub.warning_code)

    def test_archived_subscription_keeps_queued(self):
        delivery = self.queue()
        self.sub.active = False
        self.assertEqual(self.dispatch(), 0)
        self.session.post.assert_not_called()
        self.assertEqual((delivery.state, delivery.attempts), ("queued", 0))

    # -- retry now and send test --------------------------------------------------

    def test_retry_now_keeps_delivery_uuid(self):
        delivery = self.queue()
        uuid_before = delivery.delivery_uuid
        delivery.write({"state": "failed", "attempts": 8, "next_attempt_at": datetime(2026, 1, 1)})
        with self.assertLogs(DELIVERY_LOGGER, "WARNING"):
            self.session.post.return_value = answer(500)
            delivery.with_user(self.admin_user).action_retry_now()
        self.assertEqual(delivery.state, "failed")  # tried again, failed again
        self.assertEqual(delivery.attempts, 9)
        self.session.post.return_value = answer(200)
        delivery.with_user(self.admin_user).action_retry_now()
        self.env.invalidate_all()
        self.assertEqual((delivery.state, delivery.attempts), ("delivered", 10))
        self.assertEqual(delivery.delivery_uuid, uuid_before)
        self.assertEqual([self.sent(i)[1]["X-Gental-Delivery"] for i in range(2)], [uuid_before] * 2)
        delivery.with_user(self.admin_user).action_retry_now()  # delivered: nothing to retry
        self.assertEqual(self.session.post.call_count, 2)

    def test_retry_now_sends_only_the_chosen_rows(self):
        chosen, other = self.queue(), self.queue()
        self.env.invalidate_all()
        chosen.with_user(self.admin_user).action_retry_now()
        self.env.invalidate_all()
        self.assertEqual((chosen.state, other.state), ("delivered", "queued"))

    def test_retry_now_needs_manager(self):
        delivery = self.queue()
        with self.assertRaises(AccessError):
            delivery.with_user(self.agent_user).action_retry_now()
        self.session.post.assert_not_called()

    def test_send_test_button(self):
        action = self.sub.with_user(self.admin_user).action_send_test()
        self.env.invalidate_all()
        pings = self.Delivery.sudo().search([("subscription_id", "=", self.sub.id), ("event_code", "=", "test.ping")])
        self.assertEqual(len(pings), 1)
        self.assertEqual(pings.state, "delivered")
        self.assertEqual(self.session.post.call_count, 1)
        body, headers = self.sent()
        self.assertEqual(headers["X-Gental-Event"], "test.ping")
        self.assertEqual(json.loads(body)["data"], {"message": "Gental MCP test"})
        self.assertEqual(action["params"]["type"], "success")
        with self.assertRaises(AccessError):
            self.sub.with_user(self.agent_user).action_send_test()

    def test_send_test_failure_notifies(self):
        self.session.post.return_value = answer(404)
        with self.assertLogs(DELIVERY_LOGGER, "INFO"):
            action = self.sub.with_user(self.admin_user).action_send_test()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertIn("HTTP 404", action["params"]["message"])

    def test_dispatch_cron_registered(self):
        cron = self.env.ref("gental_mcp.cron_webhook_dispatch")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "minutes"))
        self.assertEqual(cron.model_id.model, "gental.mcp.webhook.delivery")
        self.assertTrue(cron.active)

    # -- concurrency --------------------------------------------------------------

    def committed_deliveries(self, count):
        """``count`` due deliveries committed on a separate connection (deleted after the test).

        Other connections only see committed rows, so concurrency tests can't use the test transaction.
        """
        dbname = self.env.cr.dbname

        def env_for(cr, **ctx):
            cr.execute("SET lock_timeout = '10s'")  # a lock wait fails the test instead of hanging it
            return api.Environment(cr, SUPERUSER_ID, {"tracking_disable": True, **ctx})

        with closing(sql_db.db_connect(dbname).cursor()) as cr:
            env = env_for(cr)
            sub = env["gental.mcp.webhook.subscription"].create({
                "name": "Concurrency",
                "url": HOOK_URL,
                "event_ids": [Command.set(env.ref("gental_mcp.webhook_event_contact_created").ids)],
                "run_as_user_id": env.ref("base.user_admin").id,
            })
            Delivery = env["gental.mcp.webhook.delivery"]
            uuids = []
            for i in range(count):
                delivery = Delivery.enqueue(sub, "contact.created", "res.partner", i, {"i": i}).sudo()
                delivery.next_attempt_at = datetime(2026, 1, 1) + timedelta(minutes=i)  # sent in this order
                uuids.append(delivery.delivery_uuid)
            sub_id = sub.id
            cr.commit()

        def cleanup():
            with closing(sql_db.db_connect(dbname).cursor()) as cr:
                env_for(cr)["gental.mcp.webhook.subscription"].browse(sub_id).unlink()
                cr.commit()
        self.addCleanup(cleanup)
        return dbname, env_for, sub_id, uuids

    def test_skip_locked_no_double_send(self):
        """Two workers on separate connections: the row one worker is sending is skipped by the other.

        The rows must be committed to be seen from another connection, so this test commits its own data on
        separate cursors and deletes it afterwards.
        """
        dbname, env_for, sub_id, uuids = self.committed_deliveries(3)

        sent = []
        with closing(sql_db.db_connect(dbname).cursor()) as cr_a, \
                closing(sql_db.db_connect(dbname).cursor()) as cr_b:
            worker_a = env_for(cr_a)["gental.mcp.webhook.delivery"]
            worker_b = env_for(cr_b)["gental.mcp.webhook.delivery"]
            counts = {}

            def post(url, headers, **kwargs):
                sent.append(headers["X-Gental-Delivery"])
                if len(sent) == 1:  # worker A holds the first row: worker B runs meanwhile
                    counts["b"] = worker_b._dispatch(commit=True)
                return answer(200)

            self.session.post.side_effect = post
            counts["a"] = worker_a._dispatch(commit=True)
            cr_a.execute(
                "SELECT state, count(*) FROM gental_mcp_webhook_delivery WHERE subscription_id = %s GROUP BY state",
                (sub_id,),
            )
            states = dict(cr_a.fetchall())
        self.assertEqual(sorted(sent), sorted(uuids))  # each row sent exactly once
        self.assertEqual((counts["a"], counts["b"]), (1, 2))
        self.assertEqual(states, {"delivered": 3})

    def test_claim_after_other_worker_committed(self):
        """Worker B's snapshot predates A's commit of a row B then tries to claim: B starts over, no resend."""
        dbname, env_for, sub_id, uuids = self.committed_deliveries(2)
        sent = []

        def post(url, headers, **kwargs):
            sent.append(headers["X-Gental-Delivery"])
            return answer(200)

        self.session.post.side_effect = post
        with closing(sql_db.db_connect(dbname).cursor()) as cr_a, \
                closing(sql_db.db_connect(dbname).cursor()) as cr_b:
            worker_a = env_for(cr_a)["gental.mcp.webhook.delivery"]
            worker_b = env_for(cr_b)["gental.mcp.webhook.delivery"]
            cr_b.execute("SELECT count(*) FROM gental_mcp_webhook_delivery")  # B's snapshot starts now
            self.assertEqual(worker_a._dispatch(limit=1, commit=True), 1)  # A sends and commits the first row
            cr_b.execute("SELECT 1")  # still B's old snapshot: no commit in between
            real_claim, conflicts = type(worker_b)._claim, []

            def claim(records, *args):
                try:
                    return real_claim(records, *args)
                except SerializationFailure:
                    conflicts.append(records.env.cr is cr_b)
                    raise

            with patch.object(type(worker_b), "_claim", claim):
                self.assertEqual(worker_b._dispatch(commit=True), 1)
            self.assertEqual(conflicts, [True])  # B's stale claim conflicted once, then B started over
            cr_b.execute(
                "SELECT state, count(*) FROM gental_mcp_webhook_delivery WHERE subscription_id = %s GROUP BY state",
                (sub_id,),
            )
            states = dict(cr_b.fetchall())
        self.assertEqual(sent, uuids)  # first by A, second by B, each once
        self.assertEqual(states, {"delivered": 2})

    def test_serialization_failure_inline_leaves_rest_to_cron(self):
        delivery = self.queue()
        with patch.object(type(self.Delivery), "_claim", side_effect=SerializationFailure("concurrent update")):
            self.assertEqual(delivery._dispatch_now(), 0)
        self.assertEqual(delivery.state, "queued")
        self.session.post.assert_not_called()

    # -- inline caps and the cron budget -------------------------------------------

    def slow_sends(self, seconds):
        """Each send takes ``seconds`` on the dispatcher's clock."""
        clock = {"now": 1000.0}
        self._patch(DELIVERY_CLOCK, side_effect=lambda: clock["now"])

        def post(*args, **kwargs):
            clock["now"] += seconds
            return answer(200)

        self.session.post.side_effect = post

    def test_retry_now_caps_inline_sends(self):
        rows = self.Delivery.sudo().browse([self.queue().id for _i in range(5)])
        rows.write({"state": "failed", "attempts": 8})
        action = rows.with_user(self.admin_user).action_retry_now()
        self.env.invalidate_all()
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(sorted(rows.mapped("state")), ["delivered"] * 3 + ["queued"] * 2)
        self.assertIn("3", action["params"]["message"])
        self.assertIn("2", action["params"]["message"])
        self.Delivery.sudo().dispatch()  # the cron sends the rest
        self.env.invalidate_all()
        self.assertEqual(set(rows.mapped("state")), {"delivered"})

    def test_retry_now_stops_at_time_budget(self):
        rows = self.Delivery.sudo().browse([self.queue().id for _i in range(3)])
        rows.write({"state": "failed", "attempts": 8})
        self.slow_sends(15)  # 0 s, 15 s, then past the 20 s budget
        rows.with_user(self.admin_user).action_retry_now()
        self.assertEqual(self.session.post.call_count, 2)

    def test_cron_dispatch_runs_until_budget(self):
        for _i in range(6):
            self.queue()
        self.slow_sends(20)  # starts at 0 s, 20 s and 40 s; 60 s is past the 50 s budget
        self.assertEqual(self.Delivery._cron_dispatch(), 3)
        self.session.post.reset_mock()
        self.slow_sends(1)
        self.assertEqual(self.Delivery._cron_dispatch(), 3)  # not a fixed batch: the rest go too

    def test_cron_does_not_commit_in_tests(self):
        self.queue()
        self.assertEqual(self.Delivery.with_context(ir_cron_progress_id=1)._cron_dispatch(), 1)

    def test_send_test_refused_on_archived_subscription(self):
        self.sub.active = False
        with self.assertRaisesRegex(UserError, "archived"):
            self.sub.with_user(self.admin_user).action_send_test()
        self.session.post.assert_not_called()

    def test_manager_buttons_on_own_subscription(self):
        with patch(GETADDRINFO, side_effect=public_addrinfo):
            mine = self.env["gental.mcp.webhook.subscription"].with_user(self.manager_user).create({
                "name": "Mine", "url": HOOK_URL,
                "event_ids": [Command.set(self.env.ref("gental_mcp.webhook_event_contact_created").ids)],
            })
        action = mine.action_send_test()
        self.assertEqual(action["params"]["type"], "success")
        delivery = self.queue(sub=mine)
        delivery.write({"state": "failed", "attempts": 8})
        delivery.with_user(self.manager_user).action_retry_now()
        self.env.invalidate_all()
        self.assertEqual(delivery.state, "delivered")
        # The agent-run subscription's deliveries are out of the manager's reach.
        other = self.queue()
        with self.assertRaises(AccessError):
            other.with_user(self.manager_user).action_retry_now()

    # -- fairness and the circuit breaker --------------------------------------------

    def second_sub(self):
        with patch(GETADDRINFO, side_effect=public_addrinfo):
            return self.env["gental.mcp.webhook.subscription"].create({
                "name": "Other receiver", "url": "https://other.example.com/hook",
                "event_ids": [Command.set(self.env.ref("gental_mcp.webhook_event_contact_created").ids)],
                "run_as_user_id": self.agent_user.id,
            })

    def test_unreachable_receiver_skipped_for_the_rest_of_the_run(self):
        slow = [self.queue() for _i in range(3)]
        other = self.second_sub()
        fine = self.queue(sub=other)

        def post(url, **kwargs):
            if kwargs["headers"]["Host"] == "hooks.example.com":
                raise requests.ConnectTimeout("connect timed out")
            return answer(200)

        self.session.post.side_effect = post
        with self.assertLogs(DELIVERY_LOGGER, "INFO"), self.assertLogs(NET_GUARD_LOGGER, "INFO"):
            self.Delivery._cron_dispatch()
        self.env.invalidate_all()
        self.assertEqual([d.attempts for d in slow], [1, 0, 0], "one timeout: that receiver waits for the next run")
        self.assertEqual(fine.state, "delivered")

    def test_circuit_breaker_pauses_a_failing_subscription(self):
        self.session.post.return_value = answer(500)
        clock = datetime(2026, 11, 2, 10, 0, 0)
        rows = [self.queue() for _i in range(webhook_sender.MAX_ATTEMPTS)]
        sub = self.sub.sudo()
        with self.assertLogs(DELIVERY_LOGGER, "INFO"), freeze_time(clock):
            for _i in range(5):
                self.assertEqual(self.Delivery.sudo().dispatch(limit=1), 1)
            self.env.invalidate_all()
            self.assertEqual(sub.consecutive_failures, 5)
            self.assertEqual(sub.paused_until, clock + timedelta(minutes=30))
            self.assertEqual(self.Delivery.sudo().dispatch(), 0, "paused: the cron leaves it alone")
            self.assertTrue(sub.warning)
            self.assertIn(sub, self.env["gental.mcp.webhook.subscription"].search(sub._attention_domain()))
            # A button still sends what an administrator chose.
            self.session.post.return_value = answer(200)
            rows[-1].with_user(self.admin_user).action_retry_now()
        self.env.invalidate_all()
        self.assertEqual(rows[-1].state, "delivered")
        self.assertEqual((sub.consecutive_failures, sub.paused_until), (0, False), "a success resets it")
        self.assertFalse(sub.warning)

    def test_unexpected_send_error_is_a_failed_attempt(self):
        broken, fine = self.queue(), self.queue()
        calls = []

        def send(delivery):
            calls.append(delivery.id)
            if delivery.id == broken.id:
                raise RuntimeError("bug")
            return True, 200, None

        with patch.object(webhook_sender, "send", side_effect=send), self.assertLogs(DELIVERY_LOGGER, "INFO") as logs:
            self.assertEqual(self.Delivery._cron_dispatch(), 2)
        self.assertIn("ERROR", [record.levelname for record in logs.records])
        self.env.invalidate_all()
        self.assertEqual((broken.state, broken.attempts), ("queued", 1))
        self.assertIn("RuntimeError", broken.last_error)
        self.assertEqual(fine.state, "delivered")

    def test_webhook_crons_run_after_other_jobs(self):
        for xmlid in ("cron_webhook_scan", "cron_webhook_dispatch", "cron_webhook_purge"):
            self.assertEqual(self.env.ref(f"gental_mcp.{xmlid}").priority, 10)
