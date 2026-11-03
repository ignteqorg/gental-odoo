import json
import logging
import time
import uuid
from datetime import timedelta

from psycopg2.errors import SerializationFailure

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..compat import check_read_access, create_unique_index, private, running_as_cron
from ..services import webhook_sender
from .res_users import _connected_users
from .webhook_subscription import url_host as _url_host

_logger = logging.getLogger(__name__)

MANAGER_GROUP = "gental_mcp.group_mcp_manager"
# A run of the dispatch cron keeps claiming deliveries for this long (it runs every minute, and one send may
# take up to the 10 s timeout); the rest go next run.
DISPATCH_BUDGET_SECONDS = 50
# Buttons (Retry now, Send test) send at most this many deliveries, for at most this long, inside the request.
INLINE_MAX_ROWS = 3
INLINE_BUDGET_SECONDS = 20
# Claims that hit a row another worker just committed, before a cron run gives up until next minute.
MAX_CLAIM_CONFLICTS = 3
# A delivery whose run-as user can't connect any more waits this long before it is checked again. It is
# not an attempt: it is sent once the user is allowed again.
NOT_CONNECTED_RETRY_SECONDS = 900
_CLAIM_SQL = """
    SELECT d.id
      FROM gental_mcp_webhook_delivery d
      JOIN gental_mcp_webhook_subscription s ON s.id = d.subscription_id
     WHERE d.state = 'queued' AND d.next_attempt_at <= %(now)s AND s.active
       AND NOT (s.id = ANY(%(skip)s::int[])) {scope}
     ORDER BY d.next_attempt_at, d.id
     LIMIT 1
       FOR UPDATE OF d SKIP LOCKED
"""
# The cron honours the breaker's pause; buttons (given ids) send what the manager asked for.
_CLAIM_CRON = "AND (s.paused_until IS NULL OR s.paused_until <= %(now)s)"
_CLAIM_IDS = "AND d.id IN %(ids)s"

# A queued delivery that failed this many times counts as failing (and shows up in Needs attention).
FAILING_ATTEMPTS = 3
# Deliveries deleted per batch by the purge (committed per batch under the cron).
PURGE_BATCH = 5000
MAX_BODY_BYTES = 256 * 1024
TRUNCATED = {"truncated": True}
# ``_send_claimed`` outcomes.
SKIPPED, DELIVERED, FAILED, UNREACHABLE = "skipped", "delivered", "failed", "unreachable"


def build_body(event_uuid, event_code, occurred_at, database, model, res_id, data) -> str:
    """The JSON body (UTF-8, Arabic kept literally). Over 256 KB, ``data`` becomes ``{"truncated": true}``."""
    body = {
        "id": event_uuid,
        "event": event_code,
        "occurredAt": occurred_at,
        "database": database,
        "model": model,
        "recordId": res_id,
        "data": data,
    }
    text = json.dumps(body, ensure_ascii=False, default=str)
    if len(text.encode()) > MAX_BODY_BYTES:
        text = json.dumps(dict(body, data=TRUNCATED), ensure_ascii=False, default=str)
    return text


class GentalMcpWebhookDelivery(models.Model):
    """The webhook outbox: one row per (event, subscription), sent and retried by a cron (spec §10)."""

    _name = "gental.mcp.webhook.delivery"
    _description = "Gental MCP Webhook Delivery"
    _order = "id desc"
    _rec_name = "event_code"

    subscription_id = fields.Many2one(
        "gental.mcp.webhook.subscription", string="Subscription", required=True, ondelete="cascade", index=True,
    )
    event_code = fields.Char(string="Event", required=True)
    # The event's id (the body's ``id``): the same for every subscription the event fans out to.
    event_uuid = fields.Char(string="Event ID", required=True, index=True)
    # ``X-Gental-Delivery``: unique per delivery and kept across retries, so receivers can dedupe on it.
    delivery_uuid = fields.Char(
        string="Delivery ID", required=True, copy=False, default=lambda self: str(uuid.uuid4()),
    )
    model = fields.Char(required=True)
    res_id = fields.Integer(string="Record ID")
    body = fields.Text(help="The JSON sent, exactly as signed.")
    state = fields.Selection(
        [("queued", "Queued"), ("sending", "Sending"), ("delivered", "Delivered"), ("failed", "Failed"),
         ("expired", "Expired")],
        default="queued", required=True, index=True,
        help="Expired: still queued when its archived webhook was restored more than a day later, so it was not sent.",
    )
    attempts = fields.Integer(default=0)
    next_attempt_at = fields.Datetime(string="Next Attempt", default=fields.Datetime.now, index=True)
    last_status_code = fields.Integer(string="Last Status")
    last_error = fields.Char(size=500)
    delivered_at = fields.Datetime()
    subscription_name = fields.Char(related="subscription_id.name", string="Subscription Name")
    # Shown as plain text only: the host is the part of the URL that is safe to list.
    url_host = fields.Char(string="URL Host", compute="_compute_url_host")
    record_label = fields.Char(string="Record", compute="_compute_record_label")
    body_pretty = fields.Text(string="Formatted Body", compute="_compute_body_pretty")
    headers_text = fields.Text(string="Headers", compute="_compute_headers_text")

    def init(self):
        create_unique_index(self.env.cr, "gental_mcp_webhook_delivery_uuid_uniq", self._table, ["delivery_uuid"])
        # One delivery per event and subscription, whatever happens to the scan.
        create_unique_index(
            self.env.cr, "gental_mcp_webhook_delivery_event_uniq", self._table, ["subscription_id", "event_uuid"]
        )

    def _compute_url_host(self):
        for rec in self:
            rec.url_host = _url_host(rec.subscription_id.url)

    def _compute_record_label(self):
        for rec in self:
            rec.record_label = f"{rec.model},{rec.res_id}" if rec.res_id else rec.model

    @api.depends("body")
    def _compute_body_pretty(self):
        for rec in self:
            try:
                rec.body_pretty = json.dumps(json.loads(rec.body or ""), ensure_ascii=False, indent=2)
            except ValueError:
                rec.body_pretty = rec.body or ""

    def _compute_headers_text(self):
        """The headers sent, minus the signature (and the timestamp it covers, which changes on every attempt)."""
        for rec in self:
            rec.headers_text = "\n".join([
                "Content-Type: application/json",
                f"X-Gental-Event: {rec.event_code}",
                f"X-Gental-Delivery: {rec.delivery_uuid}",
            ])

    @private
    def enqueue(self, sub, event_code: str, model: str, res_id: int, data: dict, event_uuid: str | None = None,
                occurred_at=None):
        """Queue one delivery of ``data`` for ``sub``, in the current transaction (the outbox).

        An event already queued for ``sub`` (same ``event_uuid``) is not queued twice; that delivery is returned.
        ``occurred_at`` (a naive UTC datetime) is when the change happened: the record's ``write_date`` (its
        ``create_date`` for a creation) as the scanner found it. Default: now (``test.ping``).

        ``data`` must already be filtered for the subscription's run-as user. Sudo: bookkeeping only; the
        caller builds the payload as the run-as user.
        """
        sub.ensure_one()
        event_uuid = event_uuid or str(uuid.uuid4())
        existing = self.sudo().search([("subscription_id", "=", sub.id), ("event_uuid", "=", event_uuid)], limit=1)
        if existing:
            return existing.sudo(False)
        occurred = (occurred_at or fields.Datetime.now()).isoformat(timespec="seconds") + "Z"
        body = build_body(event_uuid, event_code, occurred, self.env.cr.dbname, model, res_id, data)
        delivery = self.sudo().create({
            "subscription_id": sub.id,
            "event_code": event_code,
            "event_uuid": event_uuid,
            "model": model,
            "res_id": res_id,
            "body": body,
        })
        return delivery.sudo(False)

    # -- sending ------------------------------------------------------------------
    #
    # Each delivery is claimed alone with ``FOR UPDATE SKIP LOCKED``, sent while that row lock is held, and its
    # outcome is committed right after (under the cron). A second worker skips the locked row, so a delivery
    # is never sent twice at the same time. If the process dies after the receiver got the request but before
    # the commit, the transaction rolls back and the row is still ``queued``: it is sent again later. Delivery
    # is therefore at-least-once, and receivers dedupe on ``X-Gental-Delivery`` (kept across retries).

    @private
    def dispatch(self, limit: int = 50) -> int:
        """Send up to ``limit`` due deliveries in this transaction; return how many were tried.

        Deliveries of archived subscriptions stay queued. Sudo: crons and buttons dispatch bookkeeping rows;
        the payloads were built as each subscription's run-as user when they were queued.
        """
        return self.sudo()._dispatch(limit)

    def _dispatch(self, limit=None, ids=None, budget=None, commit=False):
        """Claim and send due deliveries one by one; return how many were tried.

        Stops after ``limit`` rows (``None``: no limit) or once ``budget`` seconds have passed. With ``commit``
        (the cron only), each outcome is committed on its own.
        """
        deadline = time.monotonic() + budget if budget is not None else None
        sent = tried = conflicts = 0
        # Subscriptions whose receiver didn't answer (connect error or timeout) in this run: their other
        # deliveries wait for the next run, so one slow receiver can't use up the whole budget.
        skip = set()
        while limit is None or tried < limit:
            if deadline is not None and time.monotonic() > deadline:
                break
            now = fields.Datetime.now()
            self.env.flush_all()  # the claim reads the table directly
            try:
                row = self._claim(now, ids, skip)
            except SerializationFailure:
                # Another worker sent and committed a row that this transaction's snapshot still sees as queued.
                # Under the cron, commit (nothing of ours is pending) to get a fresh snapshot and claim again;
                # inline, leave the rest to the cron.
                conflicts += 1
                if not commit or conflicts > MAX_CLAIM_CONFLICTS:
                    break
                self.env.cr.commit()
                continue
            if not row:
                break
            tried += 1
            delivery = self.browse(row[0])
            delivery.invalidate_recordset()
            outcome = delivery._send_claimed_safely(now)
            sent += outcome != SKIPPED
            if outcome == UNREACHABLE:
                skip.add(delivery.subscription_id.id)
            if commit:
                self.env.cr.commit()  # the outcome of this send, and the row lock released
        return sent

    def _claim(self, now, ids, skip=()):
        """Lock one due delivery (``FOR UPDATE SKIP LOCKED``) and return its row, or ``None``.

        Deliveries of the ``skip`` subscriptions are left alone, and so (under the cron, without ``ids``) are
        those of subscriptions the circuit breaker paused.
        """
        params = {"now": now, "skip": list(skip)}
        if ids is None:
            query = _CLAIM_SQL.format(scope=_CLAIM_CRON)
        else:
            query, params["ids"] = _CLAIM_SQL.format(scope=_CLAIM_IDS), tuple(ids) or (0,)
        with self.env.cr.savepoint():  # a failed claim must not abort the transaction
            self.env.cr.execute(query, params, log_exceptions=False)
            return self.env.cr.fetchone()

    def _send_claimed_safely(self, now):
        """``_send_claimed``, but an unexpected error counts as a failed attempt instead of ending the run."""
        try:
            with self.env.cr.savepoint():  # the claim's row lock is older than this savepoint and stays
                return self._send_claimed(now)
        except Exception as exc:  # noqa: BLE001 - one broken delivery must not stop the others
            _logger.exception("Gental MCP: webhook delivery %s could not be sent", self.id)
            self.env.invalidate_all()
            self._record_attempt(now, False, None, f"internal error ({type(exc).__name__})")
            return FAILED

    def _send_claimed(self, now):
        """Send one claimed (locked) delivery and record the outcome (``SKIPPED``, ``DELIVERED``, ``FAILED`` or
        ``UNREACHABLE``: no answer at all, a connect error or a timeout)."""
        self.ensure_one()
        sub = self.subscription_id
        if not _connected_users(self.env, sub.run_as_user_id):
            self.write({"next_attempt_at": now + timedelta(seconds=NOT_CONNECTED_RETRY_SECONDS)})
            sub._set_scan_warnings({sub.id: "not_connected"})
            return SKIPPED
        ok, status, error = webhook_sender.send(self)
        self._record_attempt(now, ok, status, error)
        if ok:
            return DELIVERED
        return UNREACHABLE if status is None else FAILED

    def _record_attempt(self, now, ok, status, error):
        """Write one attempt's outcome, with its back-off, and keep the subscription's circuit breaker."""
        self.ensure_one()
        sub = self.subscription_id
        attempts = self.attempts + 1
        vals = {"attempts": attempts, "last_status_code": status or 0, "last_error": error or False}
        if ok:
            vals.update(state="delivered", delivered_at=now)
            if sub.warning_code == "not_connected":
                sub._set_scan_warnings({sub.id: False})
        elif attempts >= webhook_sender.MAX_ATTEMPTS:
            vals.update(state="failed")
            _logger.warning(
                "Gental MCP: webhook delivery %s failed for good after %s attempts: %s", self.id, attempts, error,
            )
        else:
            vals.update(state="queued", next_attempt_at=now + timedelta(seconds=webhook_sender.next_delay(attempts)))
            _logger.info("Gental MCP: webhook delivery %s attempt %s failed: %s", self.id, attempts, error)
        self.write(vals)
        sub._record_send_outcome(ok, now)

    def _dispatch_now(self):
        """Send a few of these deliveries now, inside the caller's request; the rest wait for the cron.

        Capped (``INLINE_MAX_ROWS`` rows, ``INLINE_BUDGET_SECONDS``) so a button can't run into the request
        time limit, which would roll everything back and send the same deliveries again. Sudo: bookkeeping.
        """
        if not self:
            return 0
        return self.sudo()._dispatch(INLINE_MAX_ROWS, ids=self.ids, budget=INLINE_BUDGET_SECONDS)

    def _cron_dispatch(self):
        """The dispatch cron: send due deliveries until the run's time budget is spent."""
        return self.sudo()._dispatch(
            budget=DISPATCH_BUDGET_SECONDS, commit=running_as_cron(self.env),
        )

    # -- retention ------------------------------------------------------------------

    @private
    def purge(self) -> int:
        """Delete finished deliveries older than ``webhook_log_retention_days`` (queued ones stay, and so do all
        when the setting is 0), then the scan bookkeeping nobody needs any more; return the deliveries deleted.

        Sudo: a cron housekeeping job over bookkeeping rows.
        """
        days = self.env["gental.mcp.config"].get().webhook_log_retention_days
        deleted = 0
        if days > 0:
            cutoff = fields.Datetime.now() - timedelta(days=days)
            # Age counts from the last attempt (``write_date``: a Retry now restarts it), not from creation.
            # Queued deliveries of archived webhooks are never sent while archived: they go by age in the queue.
            domain = [
                "|",
                "&", ("state", "in", ("delivered", "failed", "expired")), ("write_date", "<", cutoff),
                "&", "&", ("state", "=", "queued"), ("subscription_id.active", "=", False), ("create_date", "<", cutoff),
            ]
            Delivery = self.sudo().with_context(active_test=False)
            while True:
                old = Delivery.search(domain, limit=PURGE_BATCH)
                old.unlink()
                deleted += len(old)
                if running_as_cron(self.env):
                    self.env.cr.commit()
                if len(old) < PURGE_BATCH:
                    break
        self.env["gental.mcp.webhook.snapshot"]._purge()
        return deleted

    @api.model
    def _expire_stale(self, subscriptions, max_age):
        """Mark the queued deliveries of ``subscriptions`` queued more than ``max_age`` ago as expired (sudo:
        bookkeeping; called when archived subscriptions are restored, so stale events are not sent)."""
        stale = self.sudo().search([
            ("subscription_id", "in", subscriptions.ids), ("state", "=", "queued"),
            ("create_date", "<", fields.Datetime.now() - max_age),
        ])
        stale.write({"state": "expired"})
        return len(stale)

    def _cron_purge(self):
        self.purge()
        return True

    def _check_manager(self):
        if not (self.env.su or self.env.user.has_group(MANAGER_GROUP)):
            raise AccessError(_("Only Gental MCP managers can send or retry webhooks."))

    def action_retry_now(self):
        """Queue failed or queued deliveries for now; send a few at once and leave the rest to the cron.

        A failed delivery gets one more try (its attempts keep counting, so one more failure leaves it failed).
        The delivery id is kept, so receivers still dedupe.
        """
        self._check_manager()
        check_read_access(self)
        retry = self.sudo().filtered(lambda d: d.state in ("queued", "failed"))
        retry.write({"state": "queued", "next_attempt_at": fields.Datetime.now()})
        sent = retry._dispatch_now()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Retry now"),
                "message": _(
                    "Sent now: %(sent)s. Queued for the next run (within a minute): %(queued)s.",
                    sent=sent, queued=len(retry) - sent,
                ),
                "type": "info",
                "sticky": False,
            },
        }

    # -- dashboard ------------------------------------------------------------------

    @api.model
    def _failing_domain(self):
        """Failed for good, or still retrying after 3 attempts."""
        return ["|", ("state", "=", "failed"), "&", ("state", "=", "queued"), ("attempts", ">=", FAILING_ATTEMPTS)]

    @api.model
    def stats(self, date_from) -> dict:
        """Delivery counts for rows created on or after ``date_from``: delivered, failing and queued."""
        since = [("create_date", ">=", date_from)]
        return {
            "delivered": self.search_count(since + [("state", "=", "delivered")]),
            "failing": self.search_count(since + self._failing_domain()),
            "queued": self.search_count(since + [("state", "=", "queued")]),
        }

    @api.model
    def attention_items(self, limit: int = 5) -> list:
        """Dashboard rows: subscriptions with failing deliveries, then subscriptions with a warning."""
        items = []
        groups = self._read_group(
            self._failing_domain(), ["subscription_id"], ["__count", "attempts:max"], order="__count desc", limit=limit,
        )
        for sub, count, attempts in groups:
            items.append({
                "kind": "webhook_failed",
                "label": _("Webhook “%(name)s” → %(host)s", name=sub.name, host=_url_host(sub.url)),
                "detail": _("Attempts so far: %s", attempts),
                "count": count,
                "action": {
                    "res_model": self._name,
                    "domain": [("subscription_id", "=", sub.id)] + self._failing_domain(),
                    "name": _("Webhook deliveries"),
                },
            })
        Sub = self.env["gental.mcp.webhook.subscription"]
        warned = Sub.search(Sub._attention_domain(), limit=limit)
        for sub in warned:
            items.append({
                "kind": "webhook_warning",
                "label": _("Webhook “%(name)s”: %(warning)s", name=sub.name, warning=sub.warning),
                "detail": sub.warning,
                "count": 1,
                "action": {
                    "res_model": sub._name, "domain": [("id", "=", sub.id)], "name": _("Webhooks"),
                },
            })
        return items
