import json
import time
from types import SimpleNamespace

from odoo import api, fields, models

from ..compat import create_unique_index, running_as_cron
from .res_users import _connected_users


def _json_list(text):
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _now(env):
    """The transaction time (what the ORM writes as ``create_date``/``write_date``), in whole seconds, so a
    record written in the same second as a watch starts still counts as after it."""
    return env.cr.now().replace(microsecond=0)


class GentalMcpWebhookWatch(models.Model):
    """Detection state of one (subscription, event, model): how far the scanner has read.

    Created and removed with the subscription (``_sync``). Generic events get one watch per exposed model.
    """

    _name = "gental.mcp.webhook.watch"
    _description = "Gental MCP Webhook Watch"
    _order = "id"

    subscription_id = fields.Many2one(
        "gental.mcp.webhook.subscription", required=True, ondelete="cascade", index=True,
    )
    event_id = fields.Many2one("gental.mcp.webhook.event", required=True, ondelete="cascade")
    model = fields.Char(required=True)
    # The newest ``write_date`` seen; each scan reads from ``cursor - 10 min``.
    cursor = fields.Datetime()
    # Records created (or, without a snapshot, changed) before this never fire.
    started_at = fields.Datetime()
    # Where a scan stopped when it hit its batch cap: the next scan continues after (write_date, id).
    resume_write_date = fields.Datetime()
    resume_id = fields.Integer()
    # Transition watches first snapshot the current state of the existing records ("baselining"), so a
    # record changed later is compared with what it was; nothing fires until that is done.
    baseline_done = fields.Boolean(default=True)
    baseline_after_id = fields.Integer()
    # When the scanner last took this watch: the least recently scanned go first (fairness under the cron's
    # time limit).
    scanned_at = fields.Datetime()

    def init(self):
        create_unique_index(
            self.env.cr, "gental_mcp_webhook_watch_uniq", self._table, ["subscription_id", "event_id", "model"]
        )

    @api.model
    def _desired(self, sub):
        """``{(event id, model)}`` that ``sub`` should watch: nothing while it is archived."""
        if not sub.active:
            return set()
        exposed = sub.exposed_model_ids.mapped("model")
        return {
            (event.id, model)
            for event in sub.event_ids
            for model in (exposed if event.generic else [event.model])
            if model
        }

    @api.model
    def _sync(self, subscriptions, keep_unknown_models=False):
        """Create the missing watches and remove the stale ones. New watches start now (sudo: bookkeeping).

        With ``keep_unknown_models`` (the scanner), watches of models that aren't installed are kept, in case
        the app comes back.
        """
        Watch = self.sudo()
        now = _now(self.env)
        for sub in subscriptions.sudo():
            desired = self._desired(sub)
            existing = Watch.search([("subscription_id", "=", sub.id)])
            stale = existing.filtered(lambda w: (w.event_id.id, w.model) not in desired and not (
                keep_unknown_models and w.model not in self.env.registry
            ))
            stale.unlink()
            have = {(w.event_id.id, w.model) for w in existing - stale}
            Events = self.env["gental.mcp.webhook.event"].sudo()
            Watch.create([
                {
                    "subscription_id": sub.id, "event_id": event_id, "model": model, "cursor": now, "started_at": now,
                    "baseline_done": Events.browse(event_id).kind != "transition",
                }
                for event_id, model in sorted(desired - have)
            ])

    @api.model
    def _scan_jobs(self):
        """Plain-data jobs for the scanner, one per watch of an active subscription.

        Sudo: the scanner reads business data as each subscription's run-as user; only settings leave here.
        Watches are synced first (an exposed model archived and restored gets its watch back). Watches whose
        model is not installed are skipped.
        """
        subs = self.env["gental.mcp.webhook.subscription"].sudo().search([])
        self._sync(subs, keep_unknown_models=True)
        payload_fields = {sub.id: sub.payload_field_ids for sub in subs}
        watches = self.sudo().search(
            [("subscription_id.active", "=", True)], order="scanned_at asc nulls first, id",
        )
        connected = _connected_users(self.env, watches.subscription_id.run_as_user_id)
        Sub = self.env["gental.mcp.webhook.subscription"]
        jobs, hidden = [], {}
        for watch in watches:
            if watch.model not in self.env.registry:
                continue
            sub, event = watch.subscription_id, watch.event_id
            if watch.model not in hidden:
                hidden[watch.model] = Sub._hidden_fields(watch.model)
            user = sub.run_as_user_id
            resume = (watch.resume_write_date, watch.resume_id) if watch.resume_write_date else None
            jobs.append(SimpleNamespace(
                watch_id=watch.id,
                model=watch.model,
                cursor=watch.cursor or watch.started_at or _now(self.env),
                started_at=watch.started_at or _now(self.env),
                resume=resume,
                baselined=watch.baseline_done,
                baseline_after_id=watch.baseline_after_id,
                sub_id=sub.id,
                run_as_id=user.id,
                lang=user.lang or "en_US",
                connected=user in connected,
                filter_domain=sub.filter_domain or "[]",
                code=event.code,
                kind=event.kind,
                state_field=event.state_field,
                target_values=_json_list(event.target_values),
                extra_domain=event.extra_domain or "[]",
                payload_fields=[name for name in _json_list(event.payload_fields) if isinstance(name, str)],
                generic=event.generic,
                # Generic events: the fields the subscription chose on this model (empty: key fields).
                sub_payload_fields=[
                    f.name for f in payload_fields.get(sub.id, ()) if f.model == watch.model
                ] if event.generic else [],
                hidden=hidden[watch.model],
            ))
        return jobs

    def _advance(self, cursor=None, resume=None):
        """Save where the scan got to: a new ``cursor`` when the window is done, else the ``resume`` point."""
        self.ensure_one()
        vals = {
            "resume_write_date": resume[0] if resume else False,
            "resume_id": resume[1] if resume else 0,
        }
        if cursor is not None:
            vals["cursor"] = cursor
        self.sudo().write(vals)

    def _baseline_progress(self, after_id, done):
        """Save how far the baseline got (records by id), and whether it is complete."""
        self.ensure_one()
        self.sudo().write({"baseline_after_id": 0 if done else after_id, "baseline_done": done})

    def _mark_scanned(self):
        self.sudo().write({"scanned_at": fields.Datetime.now()})

    @api.model
    def _commit_progress(self, deadline=None):
        """Commit what one watch did when running as the cron; return the seconds the run has left.

        19.0+: ``ir.cron._commit_progress``, which also knows the cron's own time limit. 17.0 and 18.0 have no
        such method: there the scan commits itself (``env.cr.commit()``) and measures against its own
        ``deadline`` (a ``time.monotonic()`` value; ``None``: no limit). Outside a cron nothing is committed and
        the time left is infinite.
        """
        cron = self.env["ir.cron"]
        if self.env.context.get("ir_cron_progress_id") and hasattr(cron, "_commit_progress"):
            return cron._commit_progress(1)
        if not running_as_cron(self.env):
            return float("inf")
        self.env.cr.commit()
        return self._time_left(deadline)

    @api.model
    def _time_left(self, deadline=None):
        """Seconds the scan has left, without committing: the cron's own limit on 19.0+ (``cron_end_time``),
        else ``deadline`` under the cron; infinite outside a cron."""
        end = self.env.context.get("cron_end_time")
        if self.env.context.get("ir_cron_progress_id") and end is not None:
            return max(end - time.monotonic(), 0)
        if not running_as_cron(self.env) or deadline is None:
            return float("inf")
        return max(deadline - time.monotonic(), 0)

    @api.model
    def _oldest_open_transaction(self):
        """When the oldest transaction still open on this database (other connections of the same role) began,
        as naive UTC, or ``None``.

        Odoo stamps ``write_date`` with the transaction's start time, so rows such a transaction writes become
        visible only when it commits, possibly long after their ``write_date``. The scanner never moves a
        cursor past this point, so those rows are still inside the window when they appear.
        """
        self.env.cr.execute("""
            SELECT min(xact_start) AT TIME ZONE 'UTC'
              FROM pg_stat_activity
             WHERE datname = current_database() AND usename = current_user
               AND pid <> pg_backend_pid() AND xact_start IS NOT NULL
        """)
        return self.env.cr.fetchone()[0]

    @api.model
    def _cron_scan(self):
        from ..services import webhook_detect

        return webhook_detect.scan(self.env)


class GentalMcpWebhookSnapshot(models.Model):
    """The last state the scanner saw of one record for one watch (JSON state value or ISO date)."""

    _name = "gental.mcp.webhook.snapshot"
    _description = "Gental MCP Webhook Snapshot"
    _log_access = False

    watch_id = fields.Many2one("gental.mcp.webhook.watch", required=True, ondelete="cascade")
    res_id = fields.Integer(string="Record ID", required=True)
    value = fields.Char()

    def init(self):
        # Also the lookup index of ``_load``.
        create_unique_index(self.env.cr, "gental_mcp_webhook_snapshot_uniq", self._table, ["watch_id", "res_id"])

    @api.model
    def _load(self, watch_id, res_ids) -> dict:
        """``{res_id: value}`` of the snapshots that exist."""
        if not res_ids:
            return {}
        self.env.cr.execute(
            f'SELECT res_id, value FROM "{self._table}" WHERE watch_id = %s AND res_id = ANY(%s)',
            (watch_id, list(res_ids)),
        )
        return dict(self.env.cr.fetchall())

    @api.model
    def _store(self, watch_id, values: dict):
        """Upsert ``{res_id: value}`` in one statement."""
        if not values:
            return
        res_ids = list(values)
        self.env.cr.execute(
            f'INSERT INTO "{self._table}" (watch_id, res_id, value) '
            "SELECT %s, unnest(%s::int[]), unnest(%s::varchar[]) "
            "ON CONFLICT (watch_id, res_id) DO UPDATE SET value = EXCLUDED.value",
            (watch_id, res_ids, [values[res_id] for res_id in res_ids]),
        )
        self.invalidate_model(["value"])

    # Rows deleted per statement. Under the cron each batch is committed, so locks are short; elsewhere (tests,
    # a manual call) everything stays in the caller's transaction.
    PURGE_BATCH = 5000

    @api.model
    def _purge(self):
        """Delete snapshots that are no longer needed (sudo: bookkeeping, run by the purge cron).

        * Snapshots of records that no longer exist.
        * Snapshots of ``created`` and ``updated`` watches older than the scan window (``cursor - LOOKBACK``):
          the scanner never reads those records again until they are written, and then compares nothing.
          Transition snapshots always stay: they are the state a later change is compared with.

        Snapshots of deleted watches and subscriptions go with them (``ondelete="cascade"``).
        """
        from ..services.webhook_detect import LOOKBACK

        self.env.flush_all()
        models = {row[0] for row in self._read_models()}
        for model in sorted(models & set(self.env.registry)):
            table = self.env[model]._table
            self._delete_batched(
                f"""SELECT s.id FROM "{self._table}" s JOIN gental_mcp_webhook_watch w ON w.id = s.watch_id
                     WHERE w.model = %s AND NOT EXISTS (SELECT 1 FROM "{table}" t WHERE t.id = s.res_id)""",
                (model,),
            )
        self._delete_batched(
            f"""SELECT s.id FROM "{self._table}" s
                  JOIN gental_mcp_webhook_watch w ON w.id = s.watch_id
                  JOIN gental_mcp_webhook_event e ON e.id = w.event_id
                 WHERE e.kind IN ('created', 'updated') AND w.cursor IS NOT NULL
                   AND CASE WHEN s.value ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T'
                            THEN replace(s.value, 'Z', '')::timestamp
                            -- a failed-detection marker ("!error <write_date>", webhook_detect._error_marker)
                            WHEN s.value ~ '^!error [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}} '
                            THEN substr(s.value, 8)::timestamp END < w.cursor - %s * interval '1 second'""",
            (LOOKBACK.total_seconds(),),
        )
        self.invalidate_model()

    def _read_models(self):
        self.env.cr.execute("SELECT DISTINCT model FROM gental_mcp_webhook_watch")
        return self.env.cr.fetchall()

    def _delete_batched(self, select_sql, params):
        """Delete the ids ``select_sql`` finds, ``PURGE_BATCH`` rows at a time (committing each batch under the cron)."""
        while True:
            self.env.cr.execute(
                f'DELETE FROM "{self._table}" WHERE id IN ({select_sql} LIMIT %s)', (*params, self.PURGE_BATCH),
            )
            done = self.env.cr.rowcount < self.PURGE_BATCH
            if running_as_cron(self.env):
                self.env.cr.commit()
            if done:
                return
