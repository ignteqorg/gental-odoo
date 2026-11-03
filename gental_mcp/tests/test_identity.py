import time

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.gental_mcp.compat import get_param
from odoo.addons.gental_mcp.core import meta as gmeta
from odoo.addons.gental_mcp.core.context import CallContext
from odoo.addons.gental_mcp.core.crypto import hmac_b64url
from odoo.addons.gental_mcp.core.tools import ToolDenied
from odoo.addons.gental_mcp.services import identity

from .common import GentalMcpCase, hide_records

SECRET = "test-identity-secret"
PHONE = "+966500000001"
VERIFIED_AT = "2026-11-02T10:00:00.0000000Z"
TS = 1793613600
SHARED_SIG = "MmNHcVuysbD5hFPhuNsMtYi9HQlcbdGqTbpqdkYx9Y4"
# The shared vector's secret is shorter than the settings accept; stored secrets are like Gental's (43 chars).
STORED_SECRET = "gental-identity-secret-for-tests-0123456789"


def signed_meta(secret=SECRET, channel="whatsapp", phone=PHONE, verified_at=VERIFIED_AT, ts=TS):
    sig = hmac_b64url(secret, f"{channel}|{phone}|{verified_at}|{ts}")
    return {gmeta.IDENTITY: {"channel": channel, "phone": phone, "verifiedAt": verified_at, "ts": ts, "sig": sig}}


@tagged("post_install", "-at_install", "gental_mcp")
class TestIdentity(GentalMcpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sa = cls.env.ref("base.sa")
        cls.env.company.country_id = cls.sa
        cls.Partner = cls.env["res.partner"]

    # -- signature ---------------------------------------------------------------------------

    def test_shared_vector_verifies(self):
        meta = signed_meta()
        self.assertEqual(meta[gmeta.IDENTITY]["sig"], SHARED_SIG)
        got = identity.verify(meta, SECRET, TS)
        self.assertEqual(got, identity.VerifiedIdentity("whatsapp", PHONE, VERIFIED_AT, TS))
        # ts sent as a string verifies the same way (signed verbatim).
        meta[gmeta.IDENTITY]["ts"] = str(TS)
        self.assertEqual(identity.verify(meta, SECRET, TS).ts, TS)

    def test_bad_sig_none(self):
        meta = signed_meta()
        meta[gmeta.IDENTITY]["sig"] = SHARED_SIG[:-1] + "A"
        self.assertIsNone(identity.verify(meta, SECRET, TS))
        self.assertIsNone(identity.verify(signed_meta(), "other-secret", TS))
        tampered = signed_meta()
        tampered[gmeta.IDENTITY]["phone"] = "+966500000002"
        self.assertIsNone(identity.verify(tampered, SECRET, TS))
        # Missing secret, missing or malformed identity: never verified.
        self.assertIsNone(identity.verify(signed_meta(), None, TS))
        self.assertIsNone(identity.verify(signed_meta(), "", TS))
        self.assertIsNone(identity.verify({}, SECRET, TS))
        self.assertIsNone(identity.verify({gmeta.IDENTITY: "x"}, SECRET, TS))
        incomplete = signed_meta()
        del incomplete[gmeta.IDENTITY]["verifiedAt"]
        self.assertIsNone(identity.verify(incomplete, SECRET, TS))
        bad_ts = signed_meta(ts="soon")
        self.assertIsNone(identity.verify(bad_ts, SECRET, TS))

    def test_stale_ts_none(self):
        self.assertIsNone(identity.verify(signed_meta(), SECRET, TS + 301))
        self.assertIsNone(identity.verify(signed_meta(), SECRET, TS - 301))
        self.assertIsNotNone(identity.verify(signed_meta(), SECRET, TS + 300))

    def test_unknown_channel_none(self):
        self.assertIsNone(identity.verify(signed_meta(channel="sms"), SECRET, TS))
        for channel in ("whatsapp", "whatsapp_linked", "widget", "api"):
            self.assertIsNotNone(identity.verify(signed_meta(channel=channel), SECRET, TS), channel)

    # -- phone normalisation -----------------------------------------------------------------

    def test_normalise_variants(self):
        cc = self.sa.phone_code
        for raw in ("0500000001", "+966 50 000 0001", "966500000001", "00966500000001",
                    "+966500000001", "٠٥٠٠٠٠٠٠٠١", "+966-50-000-0001"):
            self.assertEqual(identity.normalise_phone(raw, cc), "+966500000001", raw)
        self.assertEqual(identity.normalise_phone("+966500000001", None), "+966500000001")
        # A national 0 written after the country code is dropped.
        for raw in ("+966 0 50 000 0001", "+9660500000001", "00966 0500000001"):
            self.assertEqual(identity.normalise_phone(raw, cc), "+966500000001", raw)
        self.assertEqual(identity.normalise_phone("+39 06 1234 5678", "39"), "+390612345678")
        self.assertIsNone(identity.normalise_phone("12345", cc))
        self.assertIsNone(identity.normalise_phone("", cc))
        self.assertIsNone(identity.normalise_phone(False, cc))

    # -- partner matching --------------------------------------------------------------------

    def _match(self, phone=PHONE):
        return identity.match_partner(self.env(user=self.agent_user), phone)

    def test_match_single_partner(self):
        partner = self.Partner.create({"name": "Noura", "phone": "0500000001", "country_id": self.sa.id})
        self.Partner.create({"name": "Other", "phone": "0500000002"})
        self.assertEqual(self._match(), partner)
        # A partner without a country falls back to the company's country.
        partner.country_id = False
        self.assertEqual(self._match(), partner)
        self.assertEqual(self._match().env.uid, self.agent_user.id)
        self.assertIsNone(self._match("+966500000009"))

    def test_match_two_partners_none(self):
        self.Partner.create({"name": "A", "phone": "+966 50 000 0001"})
        self.Partner.create({"name": "B", "phone": "966500000001"})
        self.assertIsNone(self._match())

    def test_child_contact_resolves_to_company(self):
        company = self.Partner.create({"name": "Daam Co", "is_company": True, "phone": "0110000000"})
        self.Partner.create({"name": "Fahad", "parent_id": company.id, "phone": "0500000001"})
        self.Partner.create({"name": "Sara", "parent_id": company.id, "phone": "00966500000001"})
        self.assertEqual(self._match(), company)

    def test_company_country_fallback_setting(self):
        partner = self.Partner.create({"name": "No country", "phone": "0500000001"})
        config = self.env["gental.mcp.config"].get()
        self.assertTrue(config.identity_assume_company_country)
        self.assertEqual(self._match(), partner)
        config.identity_assume_company_country = False
        self.assertIsNone(self._match())
        # International formats still match without a country.
        partner.phone = "+966 50 000 0001"
        self.assertEqual(self._match(), partner)

    def test_partner_country_never_replaced_by_company(self):
        # A UAE contact's 05… number is +97105…, never the Saudi +96605….
        self.Partner.create({"name": "Dubai", "phone": "0500000001", "country_id": self.env.ref("base.ae").id})
        self.assertIsNone(self._match())
        self.assertTrue(self._match("+971500000001"))

    def test_archived_partner_excluded(self):
        self.Partner.create({"name": "Gone", "phone": "0500000001", "active": False})
        self.assertIsNone(self._match())

    def test_same_phone_in_two_companies_none(self):
        for name in ("Co A", "Co B"):
            company = self.Partner.create({"name": name, "is_company": True})
            self.Partner.create({"name": f"{name} buyer", "parent_id": company.id, "phone": "0500000001"})
        self.assertIsNone(self._match())

    def test_too_many_candidates_none(self):
        self.Partner.create([{"name": f"Loose {i}", "phone": f"+1 500000000{i:02d}1"} for i in range(51)])
        self.Partner.create({"name": "Real", "phone": "0500000001"})
        self.assertIsNone(self._match())

    def test_unreadable_company_none(self):
        company = self.Partner.create({"name": "Secret Co", "is_company": True})
        self.Partner.create({"name": "Visible", "parent_id": company.id, "phone": "0500000001"})
        hide_records(self.env, "res.partner", f"[('id', '!=', {company.id})]", "hide company from agent")
        self.assertIsNone(self._match())

    def test_match_respects_record_rules(self):
        partner = self.Partner.create({"name": "Hidden", "phone": "0500000001"})
        hide_records(self.env, "res.partner", f"[('id', '!=', {partner.id})]", "hide partner from agent")
        self.assertIsNone(self._match())

    # -- require -----------------------------------------------------------------------------

    def _ctx(self, meta, locale="en"):
        return CallContext(self.env(user=self.agent_user), self.agent_user.id, 0, locale, dict(meta))

    def test_require_denies_without_identity(self):
        self.env["gental.mcp.config"].get().set_identity_secret(STORED_SECRET)
        with self.assertRaises(ToolDenied) as caught:
            identity.require(self._ctx({}))
        self.assertEqual(str(caught.exception), "This needs a verified customer.")
        with self.assertRaises(ToolDenied):  # an old signature
            identity.require(self._ctx(signed_meta(STORED_SECRET)))
        now = int(time.time())
        with self.assertRaises(ToolDenied):  # signed with another secret
            identity.require(self._ctx(signed_meta(SECRET, ts=now)))
        ctx = self._ctx(signed_meta(STORED_SECRET, ts=now))
        got = identity.require(ctx)
        self.assertEqual((got.channel, got.phone, got.ts), ("whatsapp", PHONE, now))
        self.assertIs(ctx.meta["_identity"], got)
        # No secret configured: every call is refused, even a well-formed one.
        self.env["gental.mcp.config"].get().set_identity_secret(False)
        with self.assertRaises(ToolDenied):
            identity.require(self._ctx(signed_meta(STORED_SECRET, ts=now)))

    def test_require_denial_in_arabic(self):
        self.env["res.lang"]._activate_lang("ar_001")
        self.env["ir.module.module"]._load_module_terms(["gental_mcp"], ["ar_001"], overwrite=True)
        with self.assertRaises(ToolDenied) as caught:
            identity.require(self._ctx({}, locale="ar"))
        self.assertEqual(str(caught.exception), "هذي تحتاج عميل موثّق.")

    # -- secret settings ---------------------------------------------------------------------

    def test_secret_set_masked_and_cleared(self):
        env = self.env(user=self.manager_user)
        config = env["gental.mcp.config"].browse(self.env["gental.mcp.config"].get().id)
        self.assertFalse(config.identity_secret_set)
        Wizard = env["gental.mcp.identity.secret.wizard"]
        for bad in ("short", "   "):
            with self.assertRaises(UserError):
                Wizard.create({"secret": bad}).action_set()
        secret = "s3cr3t-" + "x" * 36 + "WXYZ"
        wizard = Wizard.create({"secret": f"  {secret}\n"})
        wizard.action_set()
        self.assertFalse(wizard.secret)  # not kept on the wizard row
        config.invalidate_recordset()
        self.assertTrue(config.identity_secret_set)
        self.assertEqual(config.identity_secret_hint, "••••WXYZ")
        self.assertEqual(
            get_param(self.env["ir.config_parameter"].sudo(), "gental_mcp.identity_secret"), secret
        )
        self.assertEqual(self.env["gental.mcp.config"].get().identity_secret, secret)
        self.assertNotIn(secret, str(config.message_ids.mapped("body")))
        # The plaintext is not readable by a manager through the ORM.
        with self.assertRaises(AccessError):
            config.read(["identity_secret"])
        # A plain connect user can neither open the wizard nor clear the secret.
        agent_config = config.with_user(self.agent_user)
        with self.assertRaises(AccessError):
            agent_config.action_clear_identity_secret()
        with self.assertRaises(AccessError):
            self.env["gental.mcp.identity.secret.wizard"].with_user(self.agent_user).create({"secret": secret})
        config.action_clear_identity_secret()
        config.invalidate_recordset()
        self.assertFalse(config.identity_secret_set)
        self.assertFalse(config.identity_secret_hint)
        self.assertFalse(self.env["gental.mcp.config"].get().identity_secret)
