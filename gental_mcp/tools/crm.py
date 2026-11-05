"""CRM tools (spec §6.1): leads, pipeline, notes and activities.

``log_note`` and ``schedule_activity`` work on a fixed set of declared models only (never arbitrary ones) and
are registered under the Contacts area, so they are useful without CRM; their ``model`` enum lists the declared
models whose app is installed and whose area is on. Everything runs as the acting user.
"""
import math
import re
from datetime import date

from odoo import _
from odoo.tools.translate import _lt

from .. import compat
from ..core.tools import ToolResult, ToolError
from ..services import areas, curated
from ..services.action_keys import once_per_key
from ..services.curated import CuratedTool, id_schema, register, visible_records
from .common import enum, html_text, lang_context

NOTE_MAX = 4000
SUMMARY_MAX = 200
REVENUE_MAX = 1e12
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# model -> area that owns it (a declared model is offered only when its area is installed and on)
NOTE_MODELS = {
    "crm.lead": "crm",
    "sale.order": "sales",
    "res.partner": "contacts",
    "account.move": "invoicing",
}
ACTIVITY_TYPES = (
    "mail.mail_activity_data_call",
    "mail.mail_activity_data_todo",
    "mail.mail_activity_data_meeting",
)


def _labels(ctx):
    """Display titles of the enum values (localized to ``ctx.locale``)."""
    context = lang_context(ctx)  # noqa: F841 - read by ``_``
    return {
        "lead": _("Lead"),
        "opportunity": _("Opportunity"),
        "crm.lead": _("Lead or opportunity"),
        "sale.order": _("Quotation or order"),
        "res.partner": _("Contact"),
        "account.move": _("Invoice"),
        "mail.mail_activity_data_call": _("Phone call"),
        "mail.mail_activity_data_todo": _("To do"),
        "mail.mail_activity_data_meeting": _("Meeting"),
    }


def _text(ctx, which, **values):
    context = lang_context(ctx)  # noqa: F841 - read by ``_``
    if which == "lead_created":
        return _("Lead “%(name)s” created.", **values)
    if which == "opportunity_created":
        return _("Opportunity “%(name)s” created.", **values)
    if which == "lead_updated":
        return _("“%(name)s” updated.", **values)
    if which == "nothing":
        return _("Nothing to update: pass at least one field to change.")
    if which == "note_added":
        return _("Note added to the %(label)s.", **values)
    if which == "activity_scheduled":
        return _("Activity “%(summary)s” scheduled for %(date)s.", **values)
    if which == "pipeline":
        return _("Pipeline: %(count)s open opportunities, expected revenue %(revenue)s.", **values)
    if which == "bad_model":
        return _("Notes and activities aren't available on this kind of record here.")
    if which == "empty_note":
        return _("The note is empty.")
    if which == "no_stage":
        return _("No stage")
    if which == "bad_revenue":
        return _("The expected revenue must be a number from 0 to 1,000,000,000,000.")
    if which == "bad_type_value":
        return _("The type must be lead or opportunity.")
    if which == "bad_summary":
        return _("The activity needs a short summary (up to %s characters).", SUMMARY_MAX)
    if which == "bad_assignee":
        return _("This can only be assigned to an active internal user.")
    if which == "bad_date":
        return _("The deadline must be a date like 2026-11-30.")
    if which == "bad_type":
        return _("This activity type isn't available in this Odoo.")
    return which


def _revenue(ctx, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= REVENUE_MAX:
        raise ToolError(_text(ctx, "bad_revenue"))
    return value


def _assignee(ctx, user_id):
    """The visible, active, internal user ``user_id`` (assignable as salesperson or activity owner)."""
    user = visible_records(ctx, "res.users", [user_id])
    if user.share or not user.active:
        raise ToolError(_text(ctx, "bad_assignee"))
    return user


def _ids(ids):
    return [int(i) for i in ids]


def _noteable_models(env):
    """Declared models usable for notes and activities here, in a stable order."""
    installed = areas.installed_areas(env)
    config = env["gental.mcp.config"].get()
    return [m for m, area in NOTE_MODELS.items() if area in installed and config[f"area_{area}_enabled"]]


def _target(ctx, args):
    """The declared-model record named by ``model`` and ``record_id``, visible to the acting user."""
    model = args.get("model")
    if model not in _noteable_models(ctx.env):
        raise ToolError(_text(ctx, "bad_model"))
    if model == "account.move":
        from .invoicing import customer_invoice  # Invoicing is installed: the model is offered

        return customer_invoice(ctx, args["record_id"])  # never a vendor bill or journal entry
    return visible_records(ctx, model, [args["record_id"]])


def _stage(stage):
    return {"id": stage.id, "name": stage.name}


def _lead_data(lead):
    return {
        "id": lead.id,
        "name": lead.name,
        "type": lead.type,
        "stage": _stage(lead.stage_id) if lead.stage_id else None,
        "expected_revenue": lead.expected_revenue,
        "partner_id": lead.partner_id.id or None,
        "user_id": lead.user_id.id or None,
        "tag_ids": lead.tag_ids.ids,
    }


@register
class CreateLead(CuratedTool):
    name = "create_lead"
    area = "crm"
    kind = "write"
    label = _lt("Create a lead")
    description = _lt(
        "Create a CRM lead or opportunity. Give the contact's details, or partner_id of an existing contact "
        "(find it with find_contact). Safe to retry: the same action key returns the lead already created."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 200, "title": _lt("Title"),
                         "description": _lt("Lead title.")},
                "contact_name": {"type": "string", "maxLength": 200, "title": _lt("Contact name")},
                "phone": {"type": "string", "maxLength": 50, "title": _lt("Phone")},
                "email": {"type": "string", "maxLength": 200, "title": _lt("Email")},
                "partner_id": id_schema(_lt("Existing contact id."), title=_lt("Customer")),
                "expected_revenue": {"type": "number", "minimum": 0, "maximum": REVENUE_MAX,
                                     "title": _lt("Expected revenue")},
                "type": enum(("lead", "opportunity"), _labels(ctx), title=_lt("Type"),
                             description=_lt("Default: lead.")),
                "description": {"type": "string", "maxLength": NOTE_MAX, "title": _lt("Details")},
            },
            "required": ["name"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"crm.lead": ["name", "contact_name", "phone", "email_from", "partner_id", "expected_revenue",
                             "type", "description", "stage_id", "user_id", "tag_ids"]}

    def run(self, args, ctx):
        kind = args.get("type", "lead")
        if kind not in ("lead", "opportunity"):
            raise ToolError(_text(ctx, "bad_type_value"))
        values = {"name": args["name"], "type": kind}
        for arg, field in (("contact_name", "contact_name"), ("phone", "phone"), ("email", "email_from")):
            if arg in args:
                values[field] = args[arg]
        if "expected_revenue" in args:
            values["expected_revenue"] = _revenue(ctx, args["expected_revenue"])
        if args.get("description"):
            values["description"] = html_text(args["description"])
        if "partner_id" in args:
            values["partner_id"] = visible_records(ctx, "res.partner", [args["partner_id"]]).id
        try:
            lead, _created = once_per_key(ctx, "crm.lead", lambda: ctx.env["crm.lead"].create(values))
        except curated.ODOO_ERRORS as error:
            raise curated.odoo_refusal(ctx, error) from None
        which = "opportunity_created" if lead.type == "opportunity" else "lead_created"
        return ToolResult(_text(ctx, which, name=lead.name), structured=_lead_data(lead), record=("crm.lead", lead.id))


@register
class UpdateLead(CuratedTool):
    name = "update_lead"
    area = "crm"
    kind = "write"
    idempotent = True
    label = _lt("Update a lead")
    description = _lt(
        "Change a lead or opportunity: move it to a stage, assign a salesperson, set the expected revenue or "
        "replace its tags. Use pipeline_summary to get the stage ids. Only the fields you pass change."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "lead_id": id_schema(title=_lt("Lead")),
                "stage_id": id_schema(_lt("Stage id from pipeline_summary."), title=_lt("Stage")),
                "user_id": id_schema(_lt("Salesperson (user id)."), title=_lt("Salesperson")),
                "expected_revenue": {"type": "number", "minimum": 0, "maximum": REVENUE_MAX,
                                     "title": _lt("Expected revenue")},
                "tag_ids": {"type": "array", "items": id_schema(), "maxItems": 50, "title": _lt("Tags"),
                            "description": _lt("Replaces the lead's tags.")},
            },
            "required": ["lead_id"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {
            "crm.lead": ["name", "type", "stage_id", "user_id", "expected_revenue", "tag_ids", "partner_id", "team_id",
                         "company_id"],
            "crm.stage": ["name"],
            "crm.tag": ["name"],
        }

    def run(self, args, ctx):
        lead = visible_records(ctx, "crm.lead", [args["lead_id"]])
        values = {}
        if "stage_id" in args:
            values["stage_id"] = visible_records(ctx, "crm.stage", [args["stage_id"]]).id
        if "user_id" in args:
            values["user_id"] = _assignee(ctx, args["user_id"]).id
        if "expected_revenue" in args:
            values["expected_revenue"] = _revenue(ctx, args["expected_revenue"])
        if "tag_ids" in args:
            values["tag_ids"] = [(6, 0, visible_records(ctx, "crm.tag", _ids(args["tag_ids"])).ids)]
        if not values:
            raise ToolError(_text(ctx, "nothing"))
        before = (lead.team_id, lead.company_id)
        try:
            with ctx.env.cr.savepoint():
                lead.write(values)
                # The team and company are recomputed outside the user's rights (e.g. from the new
                # salesperson): never leave the lead in a team or company the user can't see.
                for old, new in zip(before, (lead.team_id, lead.company_id)):
                    if new and new != old:
                        visible_records(ctx, new._name, [new.id])
        except curated.ODOO_ERRORS as error:
            raise curated.odoo_refusal(ctx, error) from None
        return ToolResult(_text(ctx, "lead_updated", name=lead.name), structured=_lead_data(lead),
                          record=("crm.lead", lead.id))


@register
class LogNote(CuratedTool):
    name = "log_note"
    area = "contacts"  # offered whenever any app owning one of the declared models is installed
    kind = "write"
    label = _lt("Log a note")
    description = _lt(
        "Add an internal note to a lead, quotation or order, contact or invoice. Notes are internal: "
        "nothing is emailed to the customer or to followers."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "model": enum(_noteable_models(ctx.env), _labels(ctx), title=_lt("Record type")),
                "record_id": id_schema(title=_lt("Record")),
                "body": {"type": "string", "minLength": 1, "maxLength": NOTE_MAX, "title": _lt("Note"),
                         "description": _lt("Plain text.")},
            },
            "required": ["model", "record_id", "body"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"mail.message": ["body", "message_type", "subtype_id"], "account.move": ["move_type"]}

    def run(self, args, ctx):
        record = _target(ctx, args)
        body = args["body"].strip()
        if not body:
            raise ToolError(_text(ctx, "empty_note"))
        html = html_text(body)
        try:
            message, _created = once_per_key(
                ctx, "mail.message",
                lambda: record.message_post(body=html, message_type="comment", subtype_xmlid="mail.mt_note"),
            )
        except curated.ODOO_ERRORS as error:
            raise curated.odoo_refusal(ctx, error) from None
        label = _labels(ctx)[record._name]
        return ToolResult(
            _text(ctx, "note_added", label=label),
            structured={"id": message.id, "model": record._name, "record_id": record.id},
            record=(record._name, record.id),
        )


@register
class ScheduleActivity(CuratedTool):
    name = "schedule_activity"
    area = "contacts"
    kind = "write"
    label = _lt("Schedule an activity")
    description = _lt(
        "Schedule a call, to-do or meeting on a lead, quotation or order, contact or invoice. It goes to "
        "you unless user_id (an internal user) is given."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "model": enum(_noteable_models(ctx.env), _labels(ctx), title=_lt("Record type")),
                "record_id": id_schema(title=_lt("Record")),
                "activity_type": enum(ACTIVITY_TYPES, _labels(ctx), title=_lt("Activity type")),
                "summary": {"type": "string", "minLength": 1, "maxLength": SUMMARY_MAX, "title": _lt("Summary")},
                "date_deadline": {"type": "string", "format": "date", "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                                  "title": _lt("Due date")},
                "user_id": id_schema(_lt("Assignee; default: you."), title=_lt("Assigned to")),
            },
            "required": ["model", "record_id", "activity_type", "summary", "date_deadline"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"mail.activity": ["res_model", "res_id", "activity_type_id", "summary", "date_deadline", "user_id"],
                "account.move": ["move_type"]}

    def run(self, args, ctx):
        record = _target(ctx, args)
        summary = args["summary"].strip()
        if not summary or len(summary) > SUMMARY_MAX:
            raise ToolError(_text(ctx, "bad_summary"))
        try:
            if not DATE_RE.fullmatch(args["date_deadline"]):
                raise ValueError
            deadline = date.fromisoformat(args["date_deadline"])
        except ValueError:
            raise ToolError(_text(ctx, "bad_date")) from None
        if args["activity_type"] not in ACTIVITY_TYPES or not ctx.env.ref(args["activity_type"], raise_if_not_found=False):
            raise ToolError(_text(ctx, "bad_type"))
        assignee = _assignee(ctx, args.get("user_id", ctx.env.uid))
        try:
            activity, _created = once_per_key(
                ctx, "mail.activity",
                lambda: record.activity_schedule(
                    args["activity_type"], date_deadline=deadline, summary=summary, user_id=assignee.id),
            )
        except curated.ODOO_ERRORS as error:
            raise curated.odoo_refusal(ctx, error) from None
        return ToolResult(
            _text(ctx, "activity_scheduled", summary=summary, date=deadline.isoformat()),
            structured={"id": activity.id, "model": record._name, "record_id": record.id,
                        "activity_type": args["activity_type"], "summary": summary,
                        "date_deadline": deadline.isoformat(), "user_id": assignee.id},
            record=(record._name, record.id),
        )


@register
class PipelineSummary(CuratedTool):
    name = "pipeline_summary"
    area = "crm"
    kind = "read"
    idempotent = True
    label = _lt("Pipeline summary")
    description = _lt(
        "Open opportunities per stage: count and expected revenue, with the stage ids that update_lead takes. "
        "Optionally for one salesperson (user_id) or sales team (team_id)."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "user_id": id_schema(_lt("Only this salesperson's."), title=_lt("Salesperson")),
                "team_id": id_schema(_lt("Only this sales team's."), title=_lt("Sales team")),
            },
            "additionalProperties": False,
        }

    def required_fields(self):
        return {
            "crm.lead": ["type", "stage_id", "expected_revenue", "user_id", "team_id", "company_id", "active"],
            "crm.stage": ["name", "sequence", "is_won", ("team_ids", "team_id")],  # renamed in 19.0
            "res.company": ["currency_id"],
        }

    def run(self, args, ctx):
        env = ctx.env
        # Open = not won (lost leads are archived); stage-less opportunities count too.
        domain = [("type", "=", "opportunity"), "|", ("stage_id", "=", False), ("stage_id.is_won", "=", False)]
        if "user_id" in args:
            domain.append(("user_id", "=", visible_records(ctx, "res.users", [args["user_id"]]).id))
        if "team_id" in args:
            domain.append(("team_id", "=", visible_records(ctx, "crm.team", [args["team_id"]]).id))
        own = env.company.currency_id

        def currency_of(company):
            return (company.currency_id or own).name

        by_stage = {}
        groups = env["crm.lead"]._read_group(domain, ["stage_id", "company_id"], ["__count", "expected_revenue:sum"])
        for stage, company, count, revenue in groups:
            slot = by_stage.setdefault(stage.id or None, {"count": 0, "revenue": {}})
            slot["count"] += count
            name = currency_of(company)
            slot["revenue"][name] = slot["revenue"].get(name, 0.0) + (revenue or 0.0)
        # Stages that apply to teams the user can see (no team restriction, or one of their visible teams).
        teams = env["crm.team"].search([])
        team_field = compat.crm_stage_team_field(env)
        stage_rows = env["crm.stage"].search([
            ("is_won", "=", False), "|", (team_field, "=", False), (team_field, "in", teams.ids)])
        stages = [{"stage_id": s.id, "name": s.name} for s in stage_rows]
        if None in by_stage:
            stages.append({"stage_id": None, "name": _text(ctx, "no_stage")})
        totals = {}
        for row in stages:
            slot = by_stage.get(row["stage_id"], {"count": 0, "revenue": {}})
            row["count"] = slot["count"]
            row["expected_revenue"] = _money_list(slot["revenue"])
            for name, amount in slot["revenue"].items():
                totals[name] = totals.get(name, 0.0) + amount
        count = sum(r["count"] for r in stages)
        money = _money_list(totals)
        shown = ", ".join(f"{m['amount']:,.2f} {m['currency']}" for m in money) or f"0.00 {own.name}"
        return ToolResult(_text(ctx, "pipeline", count=count, revenue=shown), structured={
            "stages": stages, "count": count, "expected_revenue": money,
        })


def _money_list(by_currency):
    return [{"currency": name, "amount": amount} for name, amount in sorted(by_currency.items())]
