"""Signed download of generated documents: ``GET /mcp/files/<id>?sig=…`` (spec §8).

The link is the capability, so the route is public. Without a valid signature it answers 404 whether
or not the id exists; a valid signature on an expired link answers 410.
"""
from odoo import http
from odoo.http import request

from ..models.document import DOWNLOAD_EXPIRED, DOWNLOAD_OK

NO_STORE = [
    ("Cache-Control", "private, no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
]
# A public page with no session language: both languages, Arabic first.
GONE = "انتهت صلاحية هذا الرابط. اطلب رابط جديد.\nThis link has expired. Ask for a new one.\n"
NOT_FOUND = "الرابط غلط أو ما هو موجود.\nThis link isn't valid.\n"


def _plain(status, text):
    headers = [("Content-Type", "text/plain; charset=utf-8"), *NO_STORE]
    return request.make_response(text, status=status, headers=headers)


class GentalMcpFiles(http.Controller):
    @http.route(
        "/mcp/files/<int:doc_id>", type="http", auth="public", methods=["GET"], csrf=False, save_session=False,
    )
    def download(self, doc_id, sig=None, **_kw):
        status, doc = request.env["gental.mcp.document"].open_for_download(doc_id, sig)
        if status == DOWNLOAD_EXPIRED:
            return _plain(410, GONE)
        if status != DOWNLOAD_OK:
            return _plain(404, NOT_FOUND)
        response = doc.pdf_stream().get_response(as_attachment=True, conditional=False, etag=False)
        # Never cached or revalidated: every download is checked and counted.
        for header in ("ETag", "Last-Modified", "Expires"):
            response.headers.pop(header, None)
        response.headers.update(dict(NO_STORE))
        return response
