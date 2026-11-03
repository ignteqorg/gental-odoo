from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged

from odoo.addons.gental_mcp.compat import get_param
from odoo.addons.gental_mcp.core.crypto import hmac_b64url
from odoo.addons.gental_mcp.models.document import _epoch
from odoo.addons.gental_mcp.models.mcp_config import QUOTE_SECRET_PARAM

from .common import GentalMcpCase, PortableHttpCase, freeze_time, use_base_url

PDF = b"%PDF-1.4\n% gental test document\n"


@tagged("post_install", "-at_install", "gental_mcp")
class TestDocuments(GentalMcpCase, PortableHttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.env["gental.mcp.config"].get()
        use_base_url(cls.env, cls.base_url())
        cls.Document = cls.env["gental.mcp.document"]

    def make_doc(self, name="Invoice INV-0001.pdf"):
        return self.Document.store(
            name=name, content=PDF, res_model="res.partner", res_id=self.agent_user.partner_id.id,
            report_xmlid="test.report", tool="get_invoice_document", client_id=False, user_id=self.agent_user.id,
        )

    def get(self, url):
        return self.url_open(url, allow_redirects=False)

    @staticmethod
    def sig_of(url):
        return parse_qs(urlsplit(url).query)["sig"][0]

    def test_secret_generated_at_install_and_stable(self):
        secret = get_param(self.env["ir.config_parameter"].sudo(), QUOTE_SECRET_PARAM)
        self.assertTrue(secret and len(secret) >= 40)
        self.assertEqual(self.env["gental.mcp.config"].quote_secret(), secret)
        # Never shown on the settings record.
        self.assertNotIn("quote_secret", self.env["gental.mcp.config"]._fields)

    def test_signed_url_roundtrip(self):
        with freeze_time("2030-03-04 10:20:30.900000"):
            doc = self.make_doc("فاتورة INV-0001.pdf")
            self.assertEqual(doc.expires_at, fields.Datetime.now().replace(microsecond=0) + timedelta(hours=24))
        url = doc.url()
        self.assertTrue(url.startswith(f"{self.base_url()}/mcp/files/{doc.id}?sig="))
        resp = self.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, PDF)
        self.assertEqual(resp.headers["Content-Type"], "application/pdf")
        self.assertTrue(resp.headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn("filename*=UTF-8''", resp.headers["Content-Disposition"])  # RFC 5987 Arabic name
        self.assertNotIn("ETag", resp.headers)
        self.assertIn("no-store", resp.headers["Cache-Control"])
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Set-Cookie", resp.headers)
        self.assertEqual(doc.download_count, 1)
        self.get(url)
        self.assertEqual(doc.download_count, 2)

    def test_attachment_private_and_unreadable(self):
        """The PDF is reachable only through the signed link: its attachment is not public, and the users who
        asked for it (connect) or a portal user can't read it through the ORM or ``/web/content``."""
        doc = self.make_doc()
        attachment = doc.attachment_id
        self.assertFalse(attachment.public)
        self.assertEqual(attachment.res_model, "gental.mcp.document")
        raw = attachment.sudo().raw
        self.assertEqual(getattr(raw, "content", raw), PDF)  # 20.0 returns a binary value object, not bytes
        portal = self._make_user("gmcp_portal_doc", "Doc Portal", ["base.group_portal"])
        for user in (self.agent_user, portal):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    attachment.with_user(user).read(["name", "raw"])
                try:  # a portal user has no ir.attachment ACL on 17.0, so the search itself is refused there
                    found = self.env["ir.attachment"].with_user(user).search([("id", "=", attachment.id)])
                except AccessError:
                    found = self.env["ir.attachment"]
                self.assertFalse(found)
        resp = self.url_open(f"/web/content/{attachment.id}?download=true", allow_redirects=False)
        self.assertNotEqual(resp.status_code, 200)
        self.assertNotIn(PDF, resp.content)

    def test_link_hours_setting(self):
        for bad in (0, 721):
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.config.document_link_hours = bad
                self.config.flush_recordset()
        self.config.document_link_hours = 720
        self.config.document_link_hours = 2
        before = fields.Datetime.now().replace(microsecond=0)
        doc = self.make_doc()
        after = fields.Datetime.now().replace(microsecond=0)  # the clock may cross a second boundary
        self.assertTrue(before + timedelta(hours=2) <= doc.expires_at <= after + timedelta(hours=2))

    def test_tampered_sig_404(self):
        doc = self.make_doc()
        url = doc.url()
        sig = self.sig_of(url)
        tampered = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        for bad in (url.replace(sig, tampered), url.split("?")[0], url.replace(sig, "")):
            resp = self.get(bad)
            self.assertEqual(resp.status_code, 404, bad)
            self.assertNotIn(b"%PDF", resp.content)
        # A signature of another kind (a quote) over the same id and expiry is not a document signature.
        secret = self.env["gental.mcp.config"].quote_secret()
        quote_sig = hmac_b64url(secret, f"quote|{doc.id}|{_epoch(doc.expires_at)}")
        self.assertEqual(self.get(url.replace(sig, quote_sig)).status_code, 404)
        self.assertEqual(doc.download_count, 0)

    def test_other_id_same_sig_404(self):
        first, second = self.make_doc("a.pdf"), self.make_doc("b.pdf")
        sig = self.sig_of(first.url())
        resp = self.get(f"{self.base_url()}/mcp/files/{second.id}?sig={sig}")
        self.assertEqual(resp.status_code, 404)
        # An id that doesn't exist answers exactly like a bad signature: no hint that ids exist.
        missing = self.get(f"{self.base_url()}/mcp/files/{second.id + 1000}?sig={sig}")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.content, resp.content)
        self.assertEqual(self.get(f"{self.base_url()}/mcp/files/99999999999?sig={sig}").status_code, 404)
        self.assertEqual((first.download_count, second.download_count), (0, 0))

    def test_expired_410(self):
        doc = self.make_doc()
        doc.sudo().expires_at = fields.Datetime.now() - timedelta(minutes=1)
        url = doc.url()  # correctly signed for the new expiry
        resp = self.get(url)
        self.assertEqual(resp.status_code, 410)
        self.assertNotIn(b"%PDF", resp.content)
        # Without a valid signature an expired link is still just not found.
        self.assertEqual(self.get(url.replace(self.sig_of(url), "x" * 43)).status_code, 404)

    def test_purge_cron_expires(self):
        live, old = self.make_doc("live.pdf"), self.make_doc("old.pdf")
        attachment = old.attachment_id
        old.sudo().expires_at = fields.Datetime.now() - timedelta(hours=1)
        url = old.url()
        self.Document._cron_purge_documents()
        self.assertEqual(old.state, "expired")
        self.assertFalse(attachment.exists())
        self.assertFalse(old.attachment_id)
        self.assertTrue(old.exists())  # the row stays for the log period
        self.assertEqual((live.state, bool(live.attachment_id)), ("active", True))
        self.assertEqual(self.get(url).status_code, 410)
        # Past the Inbox retention the row goes too.
        self.env.cr.execute(
            "UPDATE gental_mcp_document SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=self.config.log_retention_days + 1), old.id),
        )
        self.Document.invalidate_model(["create_date"])
        self.Document._cron_purge_documents()
        self.assertFalse(old.exists())
        self.assertTrue(live.exists())
        # A link that outlives the retention period keeps its row until it expires.
        self.config.log_retention_days = 1
        self.env.cr.execute(
            "UPDATE gental_mcp_document SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=2), live.id),
        )
        self.Document.invalidate_model(["create_date"])
        self.Document._cron_purge_documents()
        self.assertTrue(live.exists())
        self.assertEqual(live.state, "active")

    def test_documents_screen(self):
        doc = self.make_doc()
        menu = self.env.ref("gental_mcp.menu_logs_outbox_documents")
        self.assertEqual(menu.parent_id, self.env.ref("gental_mcp.menu_logs_outbox"))
        self.assertEqual(menu.parent_id.parent_id, self.env.ref("gental_mcp.menu_logs"))
        manager_docs = self.env(user=self.manager_user)["gental.mcp.document"]
        self.assertIn(doc, manager_docs.search([]))
        list_view = manager_docs.get_views([(False, "list")])["views"]["list"]["arch"]
        for field in ("name", "tool", "client_id", "create_date", "expires_at", "download_count", "state"):
            self.assertIn(f'name="{field}"', list_view)
        # Plain connect users can't see the Outbox.
        with self.assertRaises(AccessError):
            self.env(user=self.agent_user)["gental.mcp.document"].search([])
