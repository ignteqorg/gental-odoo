"""Per-version compatibility helpers for gental_mcp.

Everything that differs between Odoo majors lives here (Plan 4 extends this module).
"""
from odoo import api

UNSUPPORTED_BUILD = (
    "Gental MCP needs an Odoo build with api.private: 17.0 or 18.0 from March 2025 or later, or 19.0 and newer. "
    "Update Odoo, then install the module again."
)


def require_api_private(odoo_api):
    """``odoo_api.private``, or ``ImportError`` on a build without it.

    ``@api.private`` blocks RPC on public-named methods (``quote_secret``, ``set_identity_secret``, OAuth client
    registration, ...). 17.0 and 18.0 got it in a February 2025 backport; on an older build those methods would
    be callable over RPC, so the module refuses to load (and so to install) there.
    """
    decorator = getattr(odoo_api, "private", None)
    if decorator is None:
        raise ImportError(UNSUPPORTED_BUILD)
    return decorator


private = require_api_private(api)


def user_groups_field(env):
    """Name of the users' groups many2many field: ``group_ids`` (19+) or ``groups_id`` (17, 18)."""
    return "group_ids" if "group_ids" in env["res.users"]._fields else "groups_id"


def all_groups_field(env):
    """Users' field holding effective groups, implied ones included (searchable)."""
    return "all_group_ids" if "all_group_ids" in env["res.users"]._fields else user_groups_field(env)


def check_write_access(records):
    """Raise ``AccessError`` unless the current user may write ``records`` (ACL and record rules)."""
    if hasattr(records, "check_access"):
        records.check_access("write")
    else:  # 17.0
        records.check_access_rights("write")
        records.check_access_rule("write")


def check_read_access(records):
    """Raise ``AccessError`` unless the current user may read ``records`` (ACL and record rules)."""
    if hasattr(records, "check_access"):
        records.check_access("read")
    else:  # 17.0
        records.check_access_rights("read")
        records.check_access_rule("read")


def user_group_ids(user):
    """Effective groups of ``user``, implied ones included (``all_group_ids`` on 19+)."""
    if "all_group_ids" in user._fields:
        return user.all_group_ids
    return user[user_groups_field(user.env)]


def report_group_ids(report):
    """Groups an ``ir.actions.report`` is limited to: ``group_ids`` (19+) or ``groups_id`` (17, 18)."""
    return report["group_ids" if "group_ids" in report._fields else "groups_id"]


def create_unique_index(cr, indexname, tablename, expressions):
    """Create a unique index unless it exists (``create_unique_index`` is deprecated from 19.0)."""
    from odoo.tools import sql

    try:
        return sql.create_index(cr, indexname, tablename, expressions, unique=True)
    except TypeError:  # 17.0 and older: create_index has no ``unique`` keyword
        return sql.create_unique_index(cr, indexname, tablename, expressions)


def domain_and(*domains):
    """Conjunction of domains as a plain list: ``Domain.AND`` (19+) or ``expression.AND`` (17, 18; deprecated from 19)."""
    try:
        from odoo.fields import Domain
    except ImportError:  # 17.0, 18.0
        from odoo.osv import expression

        return expression.AND([list(d) for d in domains])
    return list(Domain.AND(domains))


def is_model_method(func):
    """True for ``@api.model`` methods (``_api_model`` from 18+, ``_api == "model"`` on 17)."""
    return bool(getattr(func, "_api_model", False)) or getattr(func, "_api", None) == "model"


def can_read_model(model):
    """Whether the current user has the read ACL on ``model`` (record rules not included)."""
    if hasattr(model, "has_access"):
        return model.browse().has_access("read")
    return model.check_access_rights("read", raise_exception=False)  # 17.0


def partner_phone_fields(env):
    """Phone fields of ``res.partner``: ``phone`` and, before 19.0, ``mobile``."""
    fields = env["res.partner"]._fields
    return tuple(name for name in ("phone", "mobile") if name in fields)


def translate(lang, source):
    """``source`` (a string or an ``_lt`` term) in ``lang`` from this module's .po files.

    ``env._`` only exists from 18.0, so this reads the code translations directly.
    """
    from odoo.tools.translate import code_translations

    text = getattr(source, "_source", source)
    if not lang or lang == "en_US":
        return text
    return code_translations.get_python_translations("gental_mcp", lang).get(text, text)


def sale_line_taxes_field(env):
    """Taxes many2many of ``sale.order.line``: ``tax_ids`` (19+) or ``tax_id`` (17, 18)."""
    return "tax_ids" if "tax_ids" in env["sale.order.line"]._fields else "tax_id"


def check_field_read(env, model, fnames):
    """Raise ``AccessError`` unless ``env``'s user may read the fields ``fnames`` of ``model`` (field ``groups``).

    19.0 and later enforce field groups on every attribute read; 17.0 and 18.0 only in ``read()``, so a computed,
    non-stored field (``res.partner.credit``) read as an attribute silently gives 0 there. Callers check first.
    Unknown names are ignored (a field of an app that isn't installed is never read). ACLs are not checked.
    """
    records = env[model]
    known = [name for name in fnames if name in records._fields]
    check = getattr(records, "check_field_access", None) or getattr(records, "_check_field_access", None)
    if check:  # 20.0+ public ``check_field_access``; 19.0 ``_check_field_access`` (deprecated in 20.0)
        for name in known:
            check(records._fields[name], "read")
    elif known:  # 17.0, 18.0
        records.check_field_access_rights("read", known)


def rpc_call_kw(model, name, args, kwargs):
    """Call ``model.name(*args, **kwargs)`` the way an RPC client does, public-method check included.

    19.0 has ``odoo.service.model.call_kw`` (which runs ``get_public_method``); 17.0 and 18.0 have
    ``odoo.api.call_kw`` and the check separately.
    """
    try:
        from odoo.service.model import call_kw
    except ImportError:  # 17.0, 18.0
        from odoo.service.model import get_public_method

        get_public_method(model, name)
        return api.call_kw(model, name, args, kwargs)
    return call_kw(model, name, args, kwargs)


def invoice_send_model(env):
    """Invoice email wizard: ``account.move.send.wizard`` (one invoice, 18+) or ``account.move.send`` (17.0)."""
    return "account.move.send.wizard" if "account.move.send.wizard" in env else "account.move.send"


def list_view_type(env):
    """The list view's type name: ``list`` (18+) or ``tree`` (17.0)."""
    return "list" if "list" in env["ir.ui.view"]._fields["type"].get_values(env) else "tree"


def cron_repeats_forever(cron):
    """Whether ``cron`` keeps running: 17.0's ``numbercall`` must be negative (it defaults to 1, one run)."""
    return "numbercall" not in cron._fields or cron.numbercall < 0


def crm_stage_team_field(env):
    """Sales team field of ``crm.stage``: ``team_ids`` (many2many, 19+) or ``team_id`` (many2one, 17, 18)."""
    return "team_ids" if "team_ids" in env["crm.stage"]._fields else "team_id"


def running_as_cron(env):
    """Whether this code runs as a scheduled action (never inside a test).

    Every version's cron runner puts the ``lastcall`` key in the job's context (18+ also ``ir_cron_progress_id``).
    Its value is empty on a cron's first run (``None`` from the scheduler on 17.0, ``False`` from Run Manually), so
    the key's presence is what counts. Code serving an HTTP request is never the cron, whatever its context says
    (a request's context could carry ``lastcall``); the scheduler, in a thread or a prefork cron worker, has no
    request.
    """
    from odoo.http import request
    from odoo.modules import module

    if module.current_test or request:
        return False
    return "lastcall" in env.context or bool(env.context.get("ir_cron_progress_id"))


def concurrency_max_tries():
    """How many times Odoo's request retry runs a request: ``odoo.service.model`` (17-19), ``odoo.http.retrying`` (20+)."""
    try:
        from odoo.service.model import MAX_TRIES_ON_CONCURRENCY_FAILURE
    except ImportError:  # 20.0+
        from odoo.http.retrying import MAX_TRIES_ON_CONCURRENCY_FAILURE
    return MAX_TRIES_ON_CONCURRENCY_FAILURE


def is_concurrency_error(exc):
    """Whether ``exc`` is a PostgreSQL error Odoo's request retry replays (lock not available, serialization
    failure, deadlock: ``PG_CONCURRENCY_ERRORS_TO_RETRY`` up to 19.0, ``sql_db.PG_CONCURRENCY_EXCEPTIONS_TO_RETRY``
    from 20.0, the same three on all)."""
    from psycopg2 import errorcodes

    return getattr(exc, "pgcode", None) in (
        errorcodes.LOCK_NOT_AVAILABLE, errorcodes.SERIALIZATION_FAILURE, errorcodes.DEADLOCK_DETECTED,
    )


def get_param(params, key, default=False):
    """String value of system parameter ``key`` read through ``params`` (an ``ir.config_parameter`` recordset,
    sudo when needed): ``get_param`` (17-19) or ``get_str`` (20+, where ``get_param`` is gone)."""
    if hasattr(params, "get_str"):
        return params.get_str(key, default)
    return params.get_param(key, default)


def get_param_bool(params, key, default=False):
    """System parameter ``key`` as a boolean: ``get_bool`` (20+), else ``str2bool`` of ``get_param`` (17-19).
    Unset or unparsable values give ``default``, as 20.0's ``get_bool`` does."""
    if hasattr(params, "get_bool"):
        return params.get_bool(key, default)
    from odoo.tools import str2bool

    value = params.get_param(key)
    if not value:
        return default
    return str2bool(str(value), default)


def set_param(params, key, value):
    """Store ``value`` (a falsy value clears it) as system parameter ``key``: ``set_param`` (17-19) or
    ``set_str`` (20+)."""
    if hasattr(params, "set_str"):
        return params.set_str(key, value)
    return params.set_param(key, value)
