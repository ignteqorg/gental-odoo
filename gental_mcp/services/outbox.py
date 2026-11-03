"""Generated documents: render an Odoo QWeb report to PDF as the acting user and hand back a signed,
expiring download link (Outbox › Documents, spec §8).

The report is rendered with ``ctx.env``, so access rights and record rules decide what can be rendered;
a user who can't read the record gets an error result. Only storing the file and its Outbox row is
bookkeeping (``gental.mcp.document.store``, sudo inside ``models/``).
"""
import logging
import re

from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tools.safe_eval import safe_eval, time as safe_time

from ..compat import check_read_access, report_group_ids, user_group_ids
from ..core.context import CallContext
from ..core.tools import ToolError, ToolResult
from . import curated

_logger = logging.getLogger(__name__)

MIMETYPE = "application/pdf"

MESSAGES = {
    "ready": {
        "en": "Here is the PDF of {name}: {url}\nThe link works until {expires} (UTC). Share it as is.",
        "ar": "هذا ملف PDF حق {name}: {url}\nالرابط يشتغل لين {expires} (بتوقيت UTC). أرسله زي ما هو.",
    },
    "no_report": {
        "en": "You can't print this document in Odoo.",
        "ar": "ما تقدر تطبع هذا المستند في أودو.",
    },
    "failed": {
        "en": "Odoo couldn't make this PDF. Staff can check the document in Odoo.",
        "ar": "أودو ما قدر يطلع ملف PDF هذا. الموظفين يقدرون يشيكون على المستند في أودو.",
    },
}


def _text(key: str, locale: str, **kw) -> str:
    texts = MESSAGES[key]
    return texts.get(locale, texts["en"]).format(**kw)


def _filename(report, record) -> str:
    """The report's own file name for ``record`` (``print_report_name``, evaluated as the acting user)."""
    name = ""
    if report.print_report_name:
        try:
            name = safe_eval(report.print_report_name, {"object": record, "time": safe_time})
        except Exception:  # noqa: BLE001 - a broken custom expression must not block the PDF
            _logger.info("Gental MCP: could not evaluate the file name of report %s", report.id, exc_info=True)
            name = ""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(name or record.display_name or report.name)).strip(" .-")
    return f"{name or 'document'}.pdf"


def may_print(ctx: CallContext, report) -> bool:
    """False when the report is limited to groups the acting user is in none of."""
    groups = report_group_ids(report)
    return not groups or bool(groups & user_group_ids(ctx.env.user))


def render_document(ctx: CallContext, report_xmlid: str, record) -> ToolResult:
    """Render ``report_xmlid`` for one ``record`` as the acting user, store it in the Outbox and return
    a result with the signed link (text, ``resource_link`` and ``structuredContent``)."""
    record = ctx.env[record._name].browse(record.ids)
    record.ensure_one()
    Report = ctx.env["ir.actions.report"]
    report = Report._get_report(report_xmlid)  # Odoo reads the action itself with sudo
    if report.model != record._name:
        raise ValueError(f"Report {report_xmlid} is for {report.model}, not {record._name}")
    if not may_print(ctx, report):
        raise ToolError(_text("no_report", ctx.locale))
    try:
        check_read_access(record)
        content, report_type = Report._render_qweb_pdf(report_xmlid, record.ids)
    except (AccessError, MissingError):
        raise ToolError(curated.message("not_visible", ctx.locale)) from None
    except UserError as e:
        # Odoo's text may name other records or settings: logged here, never shown to the model.
        _logger.info("Gental MCP: report %s failed for %s,%s: %s", report_xmlid, record._name, record.id,
                     e.args[0] if e.args else e)
        raise ToolError(_text("failed", ctx.locale)) from None
    if report_type != "pdf":
        _logger.info("Gental MCP: report %s rendered as %s, not pdf", report_xmlid, report_type)
        raise ToolError(_text("failed", ctx.locale))

    name = _filename(report, record)
    tool = ctx.tool_spec.name if ctx.tool_spec is not None else ""
    doc = ctx.env["gental.mcp.document"].store(
        name=name, content=content, res_model=record._name, res_id=record.id, report_xmlid=report_xmlid,
        tool=tool, client_id=ctx.client_id, user_id=ctx.user_id,
    )
    url = doc.url()
    expires = doc.expires_at.strftime("%Y-%m-%d %H:%M")
    return ToolResult(
        _text("ready", ctx.locale, name=name, url=url, expires=expires),
        structured={"url": url, "name": name, "expires_at": doc.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")},
        resource_links=[{"type": "resource_link", "uri": url, "name": name, "mimeType": MIMETYPE}],
        record=(record._name, record.id),
    )


class DocumentTool(curated.CuratedTool):
    """A read tool that renders one record's report: subclasses set ``model``, ``id_arg``,
    ``report_xmlid`` and the titles. ``fallback_report_xmlid`` is used when the acting user may not print
    ``report_xmlid`` (a group-limited report) but may print the fallback."""

    kind = "read"
    model: str = ""
    id_arg: str = ""
    id_description: str = ""
    report_xmlid: str = ""
    fallback_report_xmlid: str = ""

    def report_for(self, ctx: CallContext) -> str:
        """The report xmlid to render for the acting user."""
        if self.fallback_report_xmlid:
            Report = ctx.env["ir.actions.report"]
            if not may_print(ctx, Report._get_report(self.report_xmlid)):
                fallback = ctx.env.ref(self.fallback_report_xmlid, raise_if_not_found=False)
                if fallback and may_print(ctx, Report._get_report(fallback)):
                    return self.fallback_report_xmlid
        return self.report_xmlid

    def input_schema(self, ctx: CallContext) -> dict:
        return {
            "type": "object",
            "properties": {self.id_arg: curated.id_schema(self.id_description)},
            "required": [self.id_arg],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {self.model: []}

    def find_record(self, args: dict, ctx: CallContext):
        """The record to render, as the acting user; ``ToolError`` "not found" when it isn't one they may get."""
        try:
            return curated.visible_records(ctx, self.model, [args[self.id_arg]])
        except AccessError:  # no access to the model at all
            raise ToolError(curated.message("not_visible", ctx.locale)) from None

    def run(self, args: dict, ctx: CallContext) -> ToolResult:
        return render_document(ctx, self.report_for(ctx), self.find_record(args, ctx))
