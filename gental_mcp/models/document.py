"""Outbox › Documents: PDFs that tools rendered for an assistant, each behind a signed, expiring link."""
from datetime import timedelta, timezone

from odoo import api, fields, models

from ..compat import private
from ..core.crypto import consteq, hmac_b64url

DOWNLOAD_OK = "ok"
DOWNLOAD_NOT_FOUND = "not_found"
DOWNLOAD_EXPIRED = "expired"


def _epoch(dt) -> int:
    """Seconds since the epoch of an Odoo (naive UTC) datetime."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class GentalMcpDocument(models.Model):
    """One generated PDF. The file is deleted when the link expires; the row stays for the log period."""

    _name = "gental.mcp.document"
    _description = "Gental MCP Generated Document"
    _order = "id desc"

    name = fields.Char(string="Document", required=True, readonly=True)
    attachment_id = fields.Many2one("ir.attachment", string="File", ondelete="set null", readonly=True)
    res_model = fields.Char(string="Record Model", readonly=True)
    res_id = fields.Integer(string="Record ID", readonly=True)
    report_xmlid = fields.Char(string="Report", readonly=True)
    tool = fields.Char(readonly=True)
    client_id = fields.Many2one("gental.mcp.client", string="Client", ondelete="set null", readonly=True, index=True)
    user_id = fields.Many2one("res.users", string="User", ondelete="set null", readonly=True)
    expires_at = fields.Datetime(string="Expires", required=True, readonly=True, index=True)
    download_count = fields.Integer(string="Downloads", default=0, readonly=True)
    state = fields.Selection(
        [("active", "Active"), ("expired", "Expired")], default="active", required=True, readonly=True, index=True
    )

    def _signature(self) -> str:
        self.ensure_one()
        secret = self.env["gental.mcp.config"].quote_secret()
        return hmac_b64url(secret, f"doc|{self.id}|{_epoch(self.expires_at)}")

    @private
    def url(self) -> str:
        """The signed download link of this document."""
        self.ensure_one()
        base = self.env["gental.mcp.config"].get().base_url()
        return f"{base}/mcp/files/{self.id}?sig={self._signature()}"

    @private
    @api.model
    def store(self, *, name, content, res_model, res_id, report_xmlid, tool, client_id, user_id):
        """Bookkeeping: save ``content`` (PDF bytes) as an attachment owned by a new document row."""
        hours = max(1, self.env["gental.mcp.config"].get().document_link_hours or 0)
        expires_at = fields.Datetime.now().replace(microsecond=0) + timedelta(hours=hours)
        doc = self.sudo().create({
            "name": name,
            "res_model": res_model,
            "res_id": res_id,
            "report_xmlid": report_xmlid,
            "tool": tool,
            "client_id": client_id or False,
            "user_id": user_id or False,
            "expires_at": expires_at,
        })
        doc.attachment_id = self.env["ir.attachment"].sudo().create({
            "name": name,
            "raw": content,  # bytes; ``datas`` (base64) is gone in 20.0
            "mimetype": "application/pdf",
            "res_model": self._name,
            "res_id": doc.id,
        })
        return doc

    @private
    @api.model
    def open_for_download(self, doc_id, sig):
        """``(status, document)`` for a download request. The signature is checked before anything else
        is revealed: a missing row and a wrong signature both give ``not_found``."""
        doc_id = int(doc_id)
        doc = self.sudo().browse(doc_id).exists() if 0 < doc_id < 2**31 else self.browse()
        if not doc or not sig or not consteq(doc._signature(), str(sig)):
            return DOWNLOAD_NOT_FOUND, self.browse()
        if doc.state != "active" or not doc.attachment_id or doc.expires_at <= fields.Datetime.now():
            return DOWNLOAD_EXPIRED, self.browse()
        doc.download_count += 1
        return DOWNLOAD_OK, doc

    @private
    def pdf_stream(self):
        """An ``odoo.http.Stream`` over this document's PDF (the attachment is read with sudo)."""
        self.ensure_one()
        return self.env["ir.binary"]._get_stream_from(
            self.sudo().attachment_id, "raw", filename=self.name, mimetype="application/pdf"
        )

    @api.model
    def _cron_purge_documents(self):
        """Delete the files of expired links, and the rows themselves once both the Inbox retention period
        and the link have run out."""
        now = fields.Datetime.now()
        expired = self.sudo().search([("state", "=", "active"), ("expires_at", "<=", now)])
        expired.attachment_id.unlink()
        expired.write({"state": "expired"})
        retention = self.env["gental.mcp.config"].get().log_retention_days
        if retention > 0:
            # A row goes at the later of the retention period and its link's expiry.
            self.sudo().search([
                ("create_date", "<", now - timedelta(days=retention)), ("expires_at", "<=", now),
            ]).unlink()
        return True
