import json
import logging
import re
from pathlib import Path

from odoo.tests import tagged

from .common import GentalMcpCase

_logger = logging.getLogger(__name__)
ORM_COLUMNS = {
    "id", "display_name", "create_uid", "create_date", "write_uid", "write_date", "__last_update",
}
# Fields mail.thread / mail.activity.mixin inject; Odoo owns them and may change them per version.
MAIL_MIXIN_FIELD = re.compile(
    r"^(message_.*|activity_.*|my_activity_.*|has_message|website_message_ids)$"
)
FINGERPRINT = Path(__file__).with_name("fingerprint.json")


@tagged("post_install", "-at_install", "gental_mcp")
class TestSchemaFingerprint(GentalMcpCase):
    """Pins the ``gental.mcp.*`` data model, identical on every Odoo version.

    A customer who upgrades Odoo keeps the module's tables, so models, fields, types, required
    flags, relations and selection keys must not drift between the 17.0, 18.0, 19.0 and 20.0 builds.
    If you change the model on purpose, update the file from the JSON this test prints on failure
    (``FINGERPRINT <json>`` in the log) and review the diff: it is a data-migration decision.
    """

    def _current(self):
        # ``modules`` is not stored, so filter in Python. The ORM's own columns are attributed to
        # the module on 19.0 for models that declare no field of their own, so skip them by name.
        fields = self.env["ir.model.fields"].sudo().search(
            [("model", "=like", "gental.mcp.%")]
        ).filtered(
            lambda f: f.name not in ORM_COLUMNS
            and not MAIL_MIXIN_FIELD.match(f.name)
            and "gental_mcp" in (f.modules or "").split(", ")
        )
        out = {}
        for f in fields.sorted(lambda r: (r.model, r.name)):
            entry = {"type": f.ttype, "required": bool(f.required)}
            if f.relation:
                entry["relation"] = f.relation
            if f.ttype in ("selection", "reference"):
                entry["selection"] = sorted(f.selection_ids.mapped("value"))
            out.setdefault(f.model, {})[f.name] = entry
        return out

    def test_fingerprint_matches_committed(self):
        current = self._current()
        self.assertTrue(current, "no gental.mcp.* fields found")
        expected = json.loads(FINGERPRINT.read_text(encoding="utf-8"))
        if current != expected:
            _logger.error("FINGERPRINT %s", json.dumps(current, sort_keys=True))
        self.assertEqual(
            current, expected,
            "gental.mcp.* data model differs from tests/fingerprint.json",
        )
