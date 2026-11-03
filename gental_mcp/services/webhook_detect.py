"""Webhook event detection by polling (plan 3, decision 1).

Each watch reads the records of its model changed since ``cursor - 10 min`` and compares them with its
snapshots. Matches are queued in the delivery outbox in the same transaction as the snapshots.

Business data is searched and read only as the subscription's run-as user (``env(user=...)``, never sudo),
so ACLs, record rules (multi-company included) and field groups apply: records that user can't see are
simply not sent. Bookkeeping (watches, snapshots, deliveries) goes through the models' private methods.
"""
import datetime
import json
import logging
import time
import uuid

from odoo.exceptions import AccessError, MissingError

from .. import compat
from ..models.exposed_model import parse_domain
from . import field_guard
from .generic_tools import DEFAULT_FIELDS_MAX, KEY_FIELDS

_logger = logging.getLogger(__name__)

LOOKBACK = datetime.timedelta(minutes=10)
# Batches one watch reads per scan; the rest waits for the next scan (``resume`` on the watch).
MAX_BATCHES = 10
# Time one scan may take on 17.0/18.0, where the scan watches its own clock (19.0+: the cron's limit).
SCAN_BUDGET_SECONDS = 50
# The scan stops (between watches, and between the batches of one watch) once less than this is left.
SCAN_RESERVE_SECONDS = 30
# A cursor stays this far behind the oldest transaction still open on the database (see ``_cursor_cap``).
OPEN_TRANSACTION_MARGIN = datetime.timedelta(seconds=5)
EVENT_NAMESPACE = uuid.UUID("5d0f3d6e-8a8e-4c7b-9a51-0c2f4e3b7a10")
# Subscription warning codes (``warning_code``), worst last: the worst wins when a subscription has
# several watches.
_PROBLEMS = (None, "payload_errors", "no_access", "scan_failed", "not_connected")


def scan(env, batch_size: int = 500) -> int:
    """Scan every watch of the active subscriptions; return how many deliveries were queued.

    Under the cron, each watch's work is committed on its own, and the scan stops early when less than
    ``SCAN_RESERVE_SECONDS`` are left (19.0+: of the cron's run; 17.0/18.0: of ``SCAN_BUDGET_SECONDS``), also
    between the batches of one watch, which then continues from its ``resume`` point. Watches go least recently
    scanned first, so the rest go first next time.
    """
    Watch = env["gental.mcp.webhook.watch"]
    deadline = time.monotonic() + SCAN_BUDGET_SECONDS
    problems = {}
    total = 0
    for job in Watch._scan_jobs():
        problems.setdefault(job.sub_id, None)
        count, problem = 0, None
        if not job.connected:
            problem = "not_connected"
        else:
            try:
                with env.cr.savepoint():
                    count, readable, errors = _scan_watch(env, job, batch_size, deadline)
                problem = "no_access" if not readable else "payload_errors" if errors else None
            except Exception:  # one bad watch must not stop the others
                _logger.exception("Gental MCP: webhook scan failed for watch %s (%s)", job.watch_id, job.model)
                env.invalidate_all()
                problem = "scan_failed"
        total += count
        _worst(problems, job.sub_id, problem)
        Watch.browse(job.watch_id)._mark_scanned()
        if Watch._commit_progress(deadline) < SCAN_RESERVE_SECONDS:
            break  # the cron is almost out of time: the other watches run next minute
    env["gental.mcp.webhook.subscription"]._set_scan_warnings(problems)
    return total


def _worst(problems, sub_id, problem):
    if _PROBLEMS.index(problem) >= _PROBLEMS.index(problems.get(sub_id)):
        problems[sub_id] = problem


def _out_of_time(env, deadline):
    return env["gental.mcp.webhook.watch"]._time_left(deadline) < SCAN_RESERVE_SECONDS


def _scan_watch(env, job, batch_size, deadline=None):
    """Scan one watch; return (deliveries queued, whether the run-as user may read the model at all, records
    that could not be read for sending)."""
    Watch = env["gental.mcp.webhook.watch"]
    Snapshot = env["gental.mcp.webhook.snapshot"]
    run_env = env(user=job.run_as_id, context=dict(env.context, lang=job.lang, active_test=False))
    records = run_env[job.model]
    if not compat.can_read_model(records) or (
        job.kind == "transition" and not records.fields_get([job.state_field], attributes=["type"])
    ):
        # The model, or the state field (field groups), is out of the user's rights: nothing is read and the
        # cursor stays, so the changes are sent once access is back.
        return 0, False, 0
    sub = env["gental.mcp.webhook.subscription"].browse(job.sub_id)
    matching = compat.domain_and(parse_domain(job.extra_domain), parse_domain(job.filter_domain))
    if not job.baselined and not _baseline(env, job, records, matching, batch_size, deadline):
        return 0, True, 0  # still baselining: continued on the next scan
    date_field = "create_date" if job.kind == "created" else "write_date"
    base = compat.domain_and(matching, [(date_field, ">", job.cursor - LOOKBACK)])
    payload_names = None  # computed once, on the first event
    count = errors = 0
    after = job.resume
    for _batch in range(MAX_BATCHES):
        domain = base if after is None else compat.domain_and(base, [
            "|", ("write_date", ">", after[0]), "&", ("write_date", "=", after[0]), ("id", ">", after[1]),
        ])
        rows = records.search_read(domain, ["create_date", "write_date"], order="write_date, id", limit=batch_size)
        if not rows:
            break
        ids = [row["id"] for row in rows]
        snapshots = Snapshot._load(job.watch_id, ids)
        states = _states(records, ids, job.state_field) if job.kind == "transition" else None
        store, fired = {}, []
        for row in rows:
            marker = _error_marker(row)
            snapshot = snapshots.get(row["id"])
            if snapshot == marker:
                continue  # detection already failed on this version of the record: not retried until it changes
            try:
                value, fire = _detect(job, row, None if _is_marker(snapshot) else snapshot, states)
            except Exception:  # noqa: BLE001 - one bad record must not freeze the watch
                _logger.exception("Gental MCP: webhook detection failed for %s,%s", job.model, row["id"])
                errors += 1
                store[row["id"]] = marker
                continue
            if value is None:
                continue  # the state is outside the user's rights, or deleted meanwhile: retried within the lookback
            if fire:
                fired.append((row, value))
            else:
                store[row["id"]] = value
        if fired:
            if payload_names is None:
                payload_names = _payload_names(run_env, job)
            payloads, failed = _payloads(records, [row["id"] for row, _value in fired], payload_names)
            for row, value in fired:
                res_id = row["id"]
                if res_id in failed:
                    errors += 1
                    store[res_id] = value  # not sent, and not tried again on every scan
                    continue
                if res_id not in payloads:
                    continue  # out of the user's rights, or deleted meanwhile: retried within the lookback
                env["gental.mcp.webhook.delivery"].enqueue(
                    sub, job.code, job.model, res_id, payloads[res_id], _event_uuid(env, job, row, value),
                    occurred_at=_occurred_at(job, row),
                )
                count += 1
                store[res_id] = value
        Snapshot._store(job.watch_id, store)
        last = rows[-1]
        if not last["write_date"]:
            break
        after = (last["write_date"], last["id"])
        if len(rows) < batch_size:
            break
        if _out_of_time(env, deadline):
            # The run is almost over: continue from here next time; the cursor only moves once the window is done.
            Watch.browse(job.watch_id)._advance(resume=after)
            return count, True, errors
    else:
        # Batch cap reached: continue from here next time; the cursor only moves once the window is done.
        Watch.browse(job.watch_id)._advance(resume=after)
        return count, True, errors
    # Rows come oldest write first, so the last one read is the newest seen.
    newest = after[0] if after else None
    cursor = newest or env.cr.now().replace(microsecond=0)
    cap = _cursor_cap(env)
    if cap is not None:
        cursor = min(cursor, cap)
    Watch.browse(job.watch_id)._advance(cursor=max(job.cursor, cursor))
    return count, True, errors


ERROR_MARKER = "!error "


def _error_marker(row):
    """The snapshot saved for a record whose detection failed: tied to its ``write_date``, so it is skipped until
    the record is written again (then it is detected as if it had no snapshot)."""
    return ERROR_MARKER + str(row["write_date"] or row["create_date"])


def _is_marker(snapshot):
    return isinstance(snapshot, str) and snapshot.startswith(ERROR_MARKER)


def _cursor_cap(env):
    """The furthest a cursor may move: just before the oldest transaction still open on the database.

    Rows that transaction writes carry its start time as ``write_date`` but appear only when it commits; a cursor
    kept behind it keeps them inside the next scans' window (``cursor - LOOKBACK``), however long it runs.
    """
    oldest = env["gental.mcp.webhook.watch"]._oldest_open_transaction()
    return oldest.replace(microsecond=0) - OPEN_TRANSACTION_MARGIN if oldest else None


def _occurred_at(job, row):
    """When the change happened: the record's ``create_date`` for a creation, else its ``write_date``."""
    if job.kind == "created":
        return row["create_date"]
    return row["write_date"] or row["create_date"]


def _baseline(env, job, records, matching, batch_size, deadline=None):
    """Snapshot the current state of every matching record (rulings R24, R25).

    Reads by id, as the run-as user, at most ``MAX_BATCHES`` batches per scan (fewer when the run is almost
    out of time), and records where it got to. A transition that happens while this runs becomes part of the
    baseline and is not sent. Returns whether the baseline is complete.
    """
    Snapshot = env["gental.mcp.webhook.snapshot"]
    Watch = env["gental.mcp.webhook.watch"].browse(job.watch_id)
    after = job.baseline_after_id
    for _batch in range(MAX_BATCHES):
        ids = records.search(compat.domain_and(matching, [("id", ">", after)]), order="id", limit=batch_size).ids
        Snapshot._store(job.watch_id, _states(records, ids, job.state_field))
        if ids:
            after = ids[-1]
        if len(ids) < batch_size:
            Watch._baseline_progress(after, done=True)
            return True
        if _out_of_time(env, deadline):
            break
    Watch._baseline_progress(after, done=False)
    return False


def _states(records, ids, field):
    """``{id: JSON state}``, read in one go; record by record only when that read is refused."""
    try:
        rows = records.browse(ids).read([field])
    except AccessError:
        rows = []
        for res_id in ids:
            try:
                rows += records.browse(res_id).read([field])
            except (AccessError, MissingError):
                continue
    return {row["id"]: json.dumps(_normalize_state(row[field])) for row in rows}


def _detect(job, row, snapshot, states):
    """Return (new snapshot value, whether this is an event). The value is ``None`` when a transition's state
    could not be read (``states``, from ``_states``, lacks the record)."""
    created, written = row["create_date"], row["write_date"] or row["create_date"]
    if job.kind == "created":
        return _iso(created), snapshot is None and created >= job.started_at
    if job.kind == "updated":
        value = written.isoformat()  # full precision: two writes in one second still differ
        # Without a snapshot, a record never written after its creation hasn't been updated.
        fire = value != snapshot and written >= job.started_at and (snapshot is not None or written > created)
        return value, fire
    value = states.get(row["id"])
    if value is None:
        return None, False
    state = json.loads(value)
    targets = job.target_values
    if value == snapshot or (targets and state not in targets):
        return value, False
    if snapshot is None:
        # Not in the baseline (rulings R24, R25): a record created since the watch started that is already in a
        # target state fires; anything else only gets a snapshot, and so does "any change".
        return value, bool(targets) and created >= job.started_at
    if targets and _loads(snapshot) in targets:
        # Moving between two target states (``in_payment`` then ``paid``) is the same event: it already fired.
        return value, False
    return value, True


def _loads(snapshot):
    try:
        return json.loads(snapshot)
    except ValueError:
        return None


def _normalize_state(value):
    """A many2one read as ``(id, name)`` compares by its id."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else False
    return value


def _payload_names(run_env, job):
    """The fields sent for ``job`` that the run-as user may see (``field_guard.filter_fields``).

    A curated event sends its own list. A generic event sends the fields its subscription chose on this model,
    else the record's name and a few key fields, as the generic tools do without ``fields``: the whole record
    could be large and hold more personal data than the receiver needs.
    """
    if job.payload_fields:
        return field_guard.filter_fields(run_env, job.model, job.payload_fields, job.hidden)
    if not job.generic:
        return field_guard.filter_fields(run_env, job.model, None, job.hidden)
    if job.sub_payload_fields:
        return field_guard.filter_fields(run_env, job.model, job.sub_payload_fields, job.hidden)
    readable = set(field_guard.filter_fields(run_env, job.model, None, job.hidden))
    model_fields = run_env[job.model]._fields
    keys = [name for name in KEY_FIELDS if name in readable and model_fields[name].store]
    return (["display_name"] if "display_name" in readable else []) + keys[:DEFAULT_FIELDS_MAX]


def _payloads(records, ids, names):
    """``({id: data}, {ids that failed})``: ``names`` of the records as the run-as user reads them.

    Read in one go; record by record only when that read fails. Records the user can't read, or that were
    deleted meanwhile, are left out of both; a record whose read fails otherwise is logged and listed as failed.
    """
    try:
        with records.env.cr.savepoint():
            rows = _read(records.browse(ids), names)
    except (AccessError, MissingError):
        rows = None
    except Exception:  # noqa: BLE001 - retried record by record, so one bad record is the only one lost
        _logger.info("Gental MCP: webhook payload batch read failed, reading %s records one by one", len(ids))
        records.env.invalidate_all()
        rows = None
    failed = set()
    if rows is None:
        rows = []
        for res_id in ids:
            try:
                with records.env.cr.savepoint():
                    rows += _read(records.browse(res_id), names)
            except (AccessError, MissingError):
                continue
            except Exception:  # noqa: BLE001
                _logger.exception("Gental MCP: webhook payload of %s,%s could not be read", records._name, res_id)
                records.env.invalidate_all()
                failed.add(res_id)
    fields = records._fields
    payloads = {
        row["id"]: {name: _jsonable(fields.get(name), value) for name, value in row.items()} for row in rows
    }
    return payloads, failed


def _read(records, names):
    """``records.read(names)``; a record whose read is refused only for some fields (e.g. links to a model outside
    the user's rights) is read field by field, leaving those fields out. Never ``read([])``: that reads every
    field."""
    compat.check_read_access(records)
    if not names:
        return [{"id": res_id} for res_id in records.exists().ids]
    try:
        return records.read(names)
    except AccessError:
        if len(records) > 1:
            raise  # the caller retries record by record
    values = {"id": records.id}
    for name in names:
        try:
            values.update(records.read([name])[0])
        except AccessError:
            continue
    return [values]


def _jsonable(field, value):
    if field is not None and field.type == "many2one":
        return {"id": value[0], "name": value[1]} if value else False
    if isinstance(value, datetime.datetime):
        return _iso(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, str):
        return str(value)  # Markup → plain str
    return value


def _iso(value):
    """UTC datetime as ISO-8601 with ``Z`` (Odoo datetimes are naive UTC)."""
    return value.isoformat(timespec="seconds") + "Z" if value else ""


def _event_uuid(env, job, row, value):
    """The same change gives the same event id for every subscription (decision 5)."""
    key = f"{env.cr.dbname}|{job.code}|{job.model}|{row['id']}|{value}|{row['write_date']}"
    return str(uuid.uuid5(EVENT_NAMESPACE, key))
