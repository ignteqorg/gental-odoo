"""``compat.py`` helpers, run on every supported Odoo version (the version matrix runs this same file)."""
import inspect
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import errorcodes, errors

from odoo import api, fields
from odoo.exceptions import AccessError
from odoo.release import version_info
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.gental_mcp import compat
from odoo.addons.gental_mcp.services import areas, curated

from .common import GentalMcpCase

UNIQUE_INDEXES = (
    "gental_mcp_token_token_hash_uniq",
    "gental_mcp_client_client_id_uniq",
    "gental_mcp_rate_key_window_uniq",
    "gental_mcp_action_key_key_model_uniq",
    "gental_mcp_tool_setting_name_uniq",
    "gental_mcp_webhook_event_code_uniq",
    "gental_mcp_webhook_delivery_uuid_uniq",
    "gental_mcp_webhook_snapshot_uniq",
)


# Fields a curated tool declares with a ``None`` alternative, and the Odoo major that dropped them.
RETRIED_PGCODES = (errorcodes.LOCK_NOT_AVAILABLE, errorcodes.SERIALIZATION_FAILURE, errorcodes.DEADLOCK_DETECTED)


@tagged("post_install", "-at_install")
class TestCompat(GentalMcpCase):
    def test_private_is_odoo_api_private(self):
        self.assertIs(compat.private, api.private)

    def test_concurrency_retry_matches_odoo(self):
        """Odoo's own values: ``odoo.service.model`` (17.0-19.0), ``odoo.http.retrying`` / ``odoo.sql_db`` (20.0+)."""
        from odoo.service import model as service_model

        max_tries = getattr(service_model, "MAX_TRIES_ON_CONCURRENCY_FAILURE", None)
        pgcodes = getattr(service_model, "PG_CONCURRENCY_ERRORS_TO_RETRY", None)
        if max_tries is None:  # 20.0+: exception classes instead of codes
            from odoo import sql_db
            from odoo.http import retrying

            max_tries = retrying.MAX_TRIES_ON_CONCURRENCY_FAILURE
            classes = set(sql_db.PG_CONCURRENCY_EXCEPTIONS_TO_RETRY)
            pgcodes = [code for code in RETRIED_PGCODES if errors.lookup(code) in classes]
            self.assertEqual(len(classes), len(pgcodes))
        self.assertEqual(compat.concurrency_max_tries(), max_tries)
        self.assertEqual(len(pgcodes), 3)
        for pgcode in pgcodes:
            self.assertTrue(compat.is_concurrency_error(type("PgError", (Exception,), {"pgcode": pgcode})()))
        self.assertFalse(compat.is_concurrency_error(type("PgError", (Exception,), {"pgcode": "23505"})()))
        self.assertFalse(compat.is_concurrency_error(ValueError()))

    def test_get_param_bool(self):
        params = self.env["ir.config_parameter"].sudo()
        for stored, expected in (("True", True), ("1", True), ("False", False), ("0", False), ("maybe", False)):
            with self.subTest(stored=stored):
                compat.set_param(params, "gental_mcp.test_bool", stored)
                with mute_logger("odoo.addons.base.models.ir_config_parameter"):  # 20.0 logs unparsable values
                    self.assertIs(compat.get_param_bool(params, "gental_mcp.test_bool"), expected)
        compat.set_param(params, "gental_mcp.test_bool", False)
        self.assertIs(compat.get_param_bool(params, "gental_mcp.test_bool"), False)
        self.assertIs(compat.get_param_bool(params, "gental_mcp.test_bool", True), True)

    def test_users_groups_field_matches_registry(self):
        fields = self.env["res.users"]._fields
        name = compat.user_groups_field(self.env)
        self.assertIn(name, fields)
        self.assertEqual((fields[name].type, fields[name].comodel_name), ("many2many", "res.groups"))
        self.assertFalse(fields[name].compute)  # the stored membership, not 19.0's computed all_group_ids

    def test_domain_and_returns_list_and_filters(self):
        Partner = self.env["res.partner"]
        employee = Partner.create({"name": "GMC Employee", "employee": True})
        person = Partner.create({"name": "GMC Person"})
        domain = compat.domain_and([("employee", "=", True)], [("id", "in", [employee.id, person.id])])
        self.assertIsInstance(domain, list)
        self.assertEqual(Partner.search(domain), employee)

    def test_partner_phone_fields_subset_of_fields(self):
        names = compat.partner_phone_fields(self.env)
        self.assertIn("phone", names)
        self.assertTrue(set(names) <= {"phone", "mobile"})
        self.assertTrue(all(n in self.env["res.partner"]._fields for n in names))

    def test_unique_indexes_exist(self):
        self.env.cr.execute("SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN %s", [UNIQUE_INDEXES])
        found = dict(self.env.cr.fetchall())
        for name in UNIQUE_INDEXES:
            with self.subTest(index=name):
                self.assertIn(name, found)
                self.assertIn("UNIQUE", found[name])

    def test_curated_tool_fields_exist(self):
        """Every field a curated tool declares exists in this Odoo version (tools of apps that aren't
        installed are skipped; the full profile installs them all). An ``AppField`` may be missing only when
        none of the modules that add it is installed, or from its ``dropped_in`` version on."""
        installed = areas.installed_areas(self.env)
        modules = set(self.env["ir.module.module"].sudo().search([("state", "=", "installed")]).mapped("name"))
        for tool_name in sorted(curated.REGISTRY):
            tool = curated.REGISTRY[tool_name]()
            if tool.area not in installed:
                continue
            for model, names in tool.declared_fields().items():
                if model not in self.env:
                    continue
                for entry in names:
                    alternatives = curated.field_alternatives(entry)
                    with self.subTest(tool=tool_name, model=model, field=entry):
                        present = [n for n in alternatives if n in self.env[model]._fields]
                        if isinstance(entry, curated.AppField):
                            dropped = entry.dropped_in is not None and version_info[0] >= entry.dropped_in
                            if present:
                                self.assertFalse(dropped, f"{model}.{entry.name}: still here, dropped_in is wrong")
                            else:
                                self.assertTrue(dropped or (entry.modules and not set(entry.modules) & modules),
                                                f"{model}.{entry.name}: missing although expected here")
                            continue
                        self.assertEqual(len(present), 1, f"{model}: exactly one of {alternatives}")

    def test_check_field_read_refuses_group_restricted_field(self):
        """``token_hash`` is limited to ``base.group_no_one``: field groups are checked, ACLs are not."""
        user = self._make_user("gmcp_compat_plain", "Plain", ["base.group_user"])
        env = self.env(user=user)
        compat.check_field_read(env, "gental.mcp.token", ["kind", "no_such_field"])
        with self.assertRaises(AccessError):
            compat.check_field_read(env, "gental.mcp.token", ["kind", "token_hash"])
        compat.check_field_read(self.env, "gental.mcp.token", ["token_hash"])  # superuser

    def test_rpc_call_kw_refuses_private_methods(self):
        Config = self.env["gental.mcp.config"].with_user(self.manager_user)
        with self.assertRaises(AccessError):
            compat.rpc_call_kw(Config, "quote_secret", [], {})
        self.assertEqual(compat.rpc_call_kw(Config, "search_count", [[]], {}), 1)

    def test_running_as_cron_on_a_first_run(self):
        """The cron runner's context has ``lastcall`` on every version, empty on a cron's first run (17.0 has
        nothing else to tell a cron by). Never true inside a test or while serving an HTTP request."""
        from odoo.modules import module

        seen = []
        cron = self.env.ref("gental_mcp.cron_purge_logs")
        cron.lastcall = False
        if not hasattr(self.env["ir.cron"], "_commit_progress"):  # 17.0, 18.0: a manual run shares the cursor
            with patch.object(type(self.env["gental.mcp.config"]), "_cron_purge_logs",
                              lambda model: seen.append(model.env)):
                cron.method_direct_trigger()
            self.assertEqual(len(seen), 1)
            self.assertIs(seen[0].context.get("lastcall", "missing"), False)
        # The scheduler's first run on 17.0 passes lastcall=None; Run Manually passes False.
        contexts = [env.context for env in seen] + [{"lastcall": None}, {"lastcall": False}, {"ir_cron_progress_id": 1}]
        with patch.object(module, "current_test", None):
            for context in contexts:
                self.assertTrue(compat.running_as_cron(self.env(context=context)), context)
            self.assertFalse(compat.running_as_cron(self.env(context={})))
            with patch("odoo.http.request", object()):  # serving an HTTP request: never the cron
                for context in contexts:
                    self.assertFalse(compat.running_as_cron(self.env(context=context)), context)
        self.assertFalse(compat.running_as_cron(self.env(context={"lastcall": False})))

    @contextmanager
    def _registry_test_mode(self):
        """New registry cursors wrap the test cursor: ``enter_registry_test_mode`` (19.0+) or the registry's own
        ``enter_test_mode`` (17.0, 18.0)."""
        if hasattr(self, "enter_registry_test_mode"):
            with self.enter_registry_test_mode():
                yield
            return
        self.env.flush_all()
        self.registry.enter_test_mode(self.cr)
        try:
            yield
        finally:
            self.registry.leave_test_mode()

    def test_running_as_cron_under_the_scheduler(self):
        """The context the scheduler's own job path gives a cron (``_acquire_one_job`` then ``_process_job``, as
        ``_process_jobs`` does on every version; it opens the job's cursor itself) is seen as the cron. A version
        that drops or renames the runner's context keys fails here, not silently in production."""
        from odoo.modules import module

        IrCron = self.registry["ir.cron"]
        cron = self.env.ref("gental_mcp.cron_purge_logs")
        cron.write({"active": True, "nextcall": fields.Datetime.now() - timedelta(days=2)})
        seen = []
        acquire_takes_ids = "job_ids" in inspect.signature(IrCron._acquire_one_job).parameters  # 17.0
        process_takes_db = "db" in inspect.signature(IrCron._process_job).parameters  # 17.0, 18.0
        with (
            patch.object(type(self.env["gental.mcp.config"]), "_cron_purge_logs",
                         lambda model: seen.append(dict(model.env.context))),
            self._registry_test_mode(),
            self.registry.cursor() as cron_cr,
        ):
            job = IrCron._acquire_one_job(cron_cr, (cron.id,) if acquire_takes_ids else cron.id)
            self.assertTrue(job, "the cron is ready")
            IrCron._process_job(*((cron_cr.dbname,) if process_takes_db else ()), cron_cr, job)
        self.assertEqual(len(seen), 1, "the cron's method ran once")
        self.assertIn("lastcall", seen[0])
        with patch.object(module, "current_test", None):
            self.assertTrue(compat.running_as_cron(self.env(context=seen[0])), seen[0])
        self.assertFalse(compat.running_as_cron(self.env(context=seen[0])), "never inside a test")

    def test_module_crons_repeat_forever(self):
        """17.0 runs a cron once unless its data says otherwise (port-notes S6, compat/17.0/data overlays)."""
        data = self.env["ir.model.data"].search([("module", "=", "gental_mcp"), ("model", "=", "ir.cron")])
        self.assertEqual(len(data), 5)
        for cron in self.env["ir.cron"].browse(data.mapped("res_id")):
            with self.subTest(cron=cron.name):
                self.assertTrue(cron.active)
                self.assertTrue(compat.cron_repeats_forever(cron))

    def test_watch_commit_progress_without_cron_support(self):
        """``ir.cron._commit_progress`` is 19.0+: elsewhere the watch commits itself only when ``running_as_cron``
        (never inside a test), so here nothing is committed and no deadline means no limit. The cron path itself
        is pinned by ``test_webhook_detect.test_cron_scan_commits_each_watch_without_cron_progress_api``."""
        Watch = self.env["gental.mcp.webhook.watch"]
        self.assertEqual(Watch._commit_progress(), float("inf"))  # not a cron
        if not hasattr(self.env["ir.cron"], "_commit_progress"):
            cron_env = Watch.with_context(lastcall=False, ir_cron_progress_id=1)
            self.assertEqual(cron_env._commit_progress(), float("inf"))
