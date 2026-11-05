"""Invoicing curated tools (area ``invoicing``, needs the Invoicing app).

Only customer invoices and credit notes (``out_invoice`` / ``out_refund``) are ever read or sent; vendor
bills and journal entries are "not found". Everything runs as the acting user. Amounts are Odoo's own
fields, in the invoice currency (balances: in the company currency). The verified-customer tools
(``my_invoices``, ``my_balance``, ``my_invoice_document``) resolve the customer from Gental's signed identity
only and show posted invoices of that commercial partner and its contacts; no certain match gives no data.
"""
import re
from datetime import date

from odoo import _, fields
from odoo.exceptions import AccessError

from odoo.tools.translate import _lt

from .. import compat
from ..core.tools import ToolError, ToolOutcomeUnknown, ToolResult
from ..services import curated, identity
from ..services.action_keys import once_per_key
from ..services.curated import CuratedTool, id_schema, register, visible_records
from ..services.outbox import DocumentTool
from .common import enum, lang_context

CUSTOMER_TYPES = ("out_invoice", "out_refund")
STATES = ("draft", "posted", "cancel")
PAYMENT_STATES = ("not_paid", "partial", "paid", "in_payment", "reversed")
OPEN_PAYMENT_STATES = ("not_paid", "partial")
LIST_MAX, MY_MAX = 50, 10
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_SCHEMA = {"type": "string", "format": "date", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}
INVOICE_FIELDS = ["name", "move_type", "state", "payment_state", "partner_id", "commercial_partner_id",
                  "invoice_partner_display_name", "invoice_date", "invoice_date_due", "amount_total",
                  "amount_residual", "amount_residual_signed", "currency_id", "company_id"]
# The customer portal link (``portal`` is installed with Invoicing).
PORTAL_FIELDS = ["access_url", "access_token"]
# Balances are in the company currency.
BALANCE_FIELDS = {"res.partner": ["credit", "commercial_partner_id"], "res.company": ["currency_id", "root_id"]}


def _labels(ctx):
    """Localized titles of the enum values and of the move types."""
    context = lang_context(ctx)  # noqa: F841 - read by ``_``
    return {
        "draft": _("Draft"),
        "posted": _("Posted"),
        "cancel": _("Cancelled"),
        "not_paid": _("Not paid"),
        "partial": _("Partially paid"),
        "paid": _("Paid"),
        "in_payment": _("In payment"),
        "reversed": _("Reversed"),
        "out_invoice": _("Invoice"),
        "out_refund": _("Credit note"),
    }


def _text(ctx, which, **values):
    context = lang_context(ctx)  # noqa: F841 - read by ``_``
    if which == "listed":
        return _("%(count)s invoices found.", **values)
    if which == "none":
        return _("No invoices found.")
    if which == "details":
        return _("%(name)s: total %(total)s, still due %(due)s, due date %(date)s.", **values)
    if which == "balance":
        return _("Total receivable %(receivable)s, of which %(overdue)s is overdue.", **values)
    if which == "no_match":
        return _("I couldn't match your account with a customer in Odoo, so I can't show anything.")
    if which == "my_none":
        return _("You have no invoices.")
    if which == "sent":
        return _("%(name)s was emailed to %(email)s.", **values)
    if which == "already_sent":
        return _("%(name)s was already emailed for this request.", **values)
    if which == "need_ref":
        return _("Give the invoice id or its number.")
    if which == "ambiguous":
        return _("Several invoices have this number. Use the invoice id.")
    if which == "bad_value":
        return _("The value of “%(name)s” isn't allowed.", **values)
    if which == "bad_date":
        return _("Dates must look like 2026-11-30.")
    if which == "bad_limit":
        return _("The limit must be from 1 to %s.", values["maximum"])
    if which == "not_posted":
        return _("Only posted invoices can be sent. Post it in Odoo first.")
    if which == "no_email":
        return _("The invoice's customer has no email address in Odoo.")
    if which == "no_balance_rights":
        return _("You need Invoicing rights in Odoo to see customer balances.")
    if which == "balance_unavailable":
        return _("I can't check the balance right now. The team can help you with it.")
    if which == "unknown":
        return _("The invoice may have been emailed already. Check its chatter in Odoo before sending again.")
    return which


def _choice(ctx, args, name, allowed):
    """``args[name]`` checked against ``allowed`` (the schema validator does not check ``oneOf``)."""
    value = args.get(name)
    if value is not None and value not in allowed:
        raise ToolError(_text(ctx, "bad_value", name=name))
    return value


def _date(ctx, args, name):
    value = args.get(name)
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not DATE_RE.fullmatch(value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(_text(ctx, "bad_date")) from None


def _limit(ctx, args, default, maximum):
    value = args.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ToolError(_text(ctx, "bad_limit", maximum=maximum))
    return value


def _money(amount, currency):
    return f"{amount:,.2f} {currency.name}"


def _today(env):
    return fields.Date.context_today(env.user)


def _customer_domain():
    return [("move_type", "in", CUSTOMER_TYPES)]


def _is_overdue(move, today):
    return (move.move_type == "out_invoice" and move.state == "posted" and move.payment_state in OPEN_PAYMENT_STATES
            and bool(move.invoice_date_due) and move.invoice_date_due < today)


def _iso(value):
    return value.isoformat() if value else None


def _row(ctx, move, today):
    labels = _labels(ctx)
    return {
        "id": move.id,
        "number": move.name,
        "type": move.move_type,
        "type_label": labels[move.move_type],
        "partner_id": move.partner_id.id or None,
        "partner_name": move.invoice_partner_display_name or "",
        "invoice_date": _iso(move.invoice_date),
        "invoice_date_due": _iso(move.invoice_date_due),
        "state": move.state,
        "state_label": labels.get(move.state, move.state),
        "payment_state": move.payment_state,
        "payment_state_label": labels.get(move.payment_state, move.payment_state),
        "amount_total": move.amount_total,
        "amount_residual": move.amount_residual,
        "currency": move.currency_id.name,
        "overdue": _is_overdue(move, today),
    }


def _invoices_result(ctx, moves, empty_key="none", with_portal_url=False):
    """``with_portal_url`` adds the customer's portal link to each posted invoice still open (🔒 tools only)."""
    today = _today(ctx.env)
    rows = []
    for move in moves:
        row = _row(ctx, move, today)
        url = _portal_url(move) if with_portal_url else None
        if url:
            row["portal_url"] = url
        rows.append(row)
    text = _text(ctx, "listed", count=len(rows)) if rows else _text(ctx, empty_key)
    if rows:
        text += "\n" + "\n".join(
            f"- {r['number']} ({r['invoice_date'] or '-'}): {r['amount_total']:,.2f} {r['currency']}, "
            f"{r['payment_state_label']}" for r in rows)
    return ToolResult(text, structured={"invoices": rows})


def customer_invoice(ctx, invoice_id):
    """The customer invoice or credit note ``invoice_id``, visible to the acting user; anything else
    (another move type, a record the user can't see) is "not found"."""
    try:
        move = visible_records(ctx, "account.move", [invoice_id])
    except AccessError:
        raise ToolError(curated.message("not_visible", ctx.locale)) from None
    if move.move_type not in CUSTOMER_TYPES:
        raise ToolError(curated.message("not_visible", ctx.locale))
    return move


def portal_available(env):
    """Whether Odoo's customer portal (``portal`` module) is installed here."""
    return "portal.mixin" in env


def _portal_url(move):
    """The customer's portal link to view and pay the posted, still open ``move`` (Odoo's share URL with its
    access token; it doesn't expire)."""
    if (not portal_available(move.env) or move.state != "posted" or move.payment_state not in OPEN_PAYMENT_STATES
            or not hasattr(move, "_get_share_url")):
        return None
    try:
        return move.get_base_url() + move._get_share_url()
    except AccessError:
        return None


def _balance(ctx, partner, no_rights="no_balance_rights"):
    """Receivable (Odoo's ``credit``) and overdue totals of the commercial ``partner``, company currency.
    ``no_rights`` is the ``_text`` key used when the acting user can't read balances."""
    env = ctx.env
    try:
        compat.check_field_read(env, "res.partner", ["credit"])  # 17.0/18.0 give 0 instead of refusing
        receivable = partner.credit
    except AccessError:
        raise ToolError(_text(ctx, no_rights)) from None
    overdue = env["account.move"].search([
        ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
        ("commercial_partner_id", "=", partner.id), ("payment_state", "in", OPEN_PAYMENT_STATES),
        ("invoice_date_due", "<", _today(env)), ("company_id", "child_of", env.company.root_id.id),
    ])
    currency = env.company.currency_id
    overdue_total = currency.round(sum(overdue.mapped("amount_residual_signed")))
    receivable = currency.round(receivable)
    data = {
        "partner_id": partner.id,
        "receivable": receivable,
        "overdue": overdue_total,
        "overdue_count": len(overdue),
        "currency": currency.name,
    }
    text = _text(ctx, "balance", receivable=_money(receivable, currency), overdue=_money(overdue_total, currency))
    return ToolResult(text, structured=data)


def _matched_customer(ctx):
    """The verified chatting customer's commercial partner, or None (no or several matches)."""
    who = identity.require(ctx)
    return identity.match_partner(ctx.env, who.phone)


@register
class GetInvoiceDocument(DocumentTool):
    name = "get_invoice_document"
    area = "invoicing"
    model = "account.move"
    id_arg = "invoice_id"
    id_description = _lt("Id of the customer invoice or credit note.")
    id_title = _lt("Invoice")
    report_xmlid = "account.account_invoices"
    # Same invoice without the payments block, and not limited to accounting groups (sales users).
    fallback_report_xmlid = "account.account_invoices_without_payment"
    label = _lt("Invoice PDF")
    description = _lt(
        "Makes the PDF of a customer invoice or credit note and returns a download link that expires. "
        "Give the customer the link exactly as returned."
    )

    def run(self, args, ctx):
        customer_invoice(ctx, args[self.id_arg])  # never a vendor bill or journal entry
        return super().run(args, ctx)


@register
class MyInvoiceDocument(GetInvoiceDocument):
    name = "my_invoice_document"
    id_description = _lt("Id of one of the customer's own invoices or credit notes (from my_invoices).")
    requires_identity = True
    label = _lt("My invoice PDF")
    description = _lt(
        "Makes the PDF of one of the chatting customer's own posted invoices or credit notes and returns a "
        "download link that expires. Works only for clients that send Gental's verified customer identity; "
        "another customer's invoice is not found. Give the customer the link exactly as returned."
    )

    def required_fields(self):
        return {"account.move": ["move_type", "state", "commercial_partner_id"]}

    def find_record(self, args, ctx):
        customer = _matched_customer(ctx)
        move = ctx.env["account.move"].search(
            _customer_domain() + [("id", "=", args[self.id_arg]), ("state", "=", "posted"),
                                  ("commercial_partner_id", "=", customer.id)], limit=1) if customer else None
        if not move:
            raise ToolError(curated.message("not_visible", ctx.locale))
        return move

    def run(self, args, ctx):
        return DocumentTool.run(self, args, ctx)


@register
class ListInvoices(CuratedTool):
    name = "list_invoices"
    area = "invoicing"
    kind = "read"
    label = _lt("List invoices")
    description = _lt(
        "List customer invoices and credit notes, newest first. Filter by customer (partner_id, includes "
        "its contacts), state, payment state, overdue only, or invoice date range. Amounts are in each "
        "invoice's currency."
    )

    def input_schema(self, ctx):
        labels = _labels(ctx)
        return {
            "type": "object",
            "properties": {
                "partner_id": id_schema(_lt("Customer id (find_contact)."), title=_lt("Customer")),
                "state": enum(STATES, labels, title=_lt("Status")),
                "payment_state": enum(PAYMENT_STATES, labels, title=_lt("Payment status")),
                "overdue_only": {"type": "boolean", "title": _lt("Overdue only"),
                                 "description": _lt("Only posted, unpaid invoices (not credit notes) past their due "
                                                    "date.")},
                "date_from": dict(DATE_SCHEMA, title=_lt("From date"), description=_lt("Invoice date from (inclusive).")),
                "date_to": dict(DATE_SCHEMA, title=_lt("To date"), description=_lt("Invoice date to (inclusive).")),
                "limit": {"type": "integer", "minimum": 1, "maximum": LIST_MAX, "title": _lt("How many"),
                          "description": _lt("Default 20.")},
            },
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"account.move": INVOICE_FIELDS, "res.partner": ["commercial_partner_id"]}

    def run(self, args, ctx):
        domain = _customer_domain()
        if "partner_id" in args:
            partner = visible_records(ctx, "res.partner", [args["partner_id"]])
            domain.append(("commercial_partner_id", "=", partner.commercial_partner_id.id))
        state = _choice(ctx, args, "state", STATES)
        if state:
            domain.append(("state", "=", state))
        payment_state = _choice(ctx, args, "payment_state", PAYMENT_STATES)
        if payment_state:
            domain.append(("payment_state", "=", payment_state))
        if args.get("overdue_only"):
            domain += [("move_type", "=", "out_invoice"), ("state", "=", "posted"),
                       ("payment_state", "in", OPEN_PAYMENT_STATES),
                       ("invoice_date_due", "<", _today(ctx.env))]
        date_from, date_to = _date(ctx, args, "date_from"), _date(ctx, args, "date_to")
        if date_from:
            domain.append(("invoice_date", ">=", date_from))
        if date_to:
            domain.append(("invoice_date", "<=", date_to))
        limit = _limit(ctx, args, 20, LIST_MAX)
        moves = ctx.env["account.move"].search(domain, order="invoice_date desc, id desc", limit=limit)
        return _invoices_result(ctx, moves)


@register
class InvoiceDetails(CuratedTool):
    name = "invoice_details"
    area = "invoicing"
    kind = "read"
    label = _lt("Invoice details")
    description = _lt(
        "One customer invoice or credit note by id or number: lines, totals, amount still due, payment "
        "state, due date and, for posted invoices not fully paid, portal_url: a non-expiring link the "
        "customer can open to view the invoice (and pay online when online payment is set up in Odoo)."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "invoice_id": id_schema(title=_lt("Invoice")),
                "number": {"type": "string", "minLength": 1, "maxLength": 100, "title": _lt("Invoice number"),
                           "description": _lt("Invoice number, e.g. INV/2026/00012.")},
            },
            "additionalProperties": False,
        }

    def required_fields(self):
        return {
            "account.move": INVOICE_FIELDS + ["amount_untaxed", "amount_tax", "invoice_line_ids"] + PORTAL_FIELDS,
            "account.move.line": ["name", "product_id", "quantity", "price_unit", "discount", "price_subtotal",
                                  "price_total", "display_type"],
        }

    def _find(self, args, ctx):
        if "invoice_id" in args:
            return customer_invoice(ctx, args["invoice_id"])
        number = (args.get("number") or "").strip()
        if not number:
            raise ToolError(_text(ctx, "need_ref"))
        moves = ctx.env["account.move"].search(_customer_domain() + [("name", "=", number)], limit=2)
        if not moves:
            raise ToolError(curated.message("not_visible", ctx.locale))
        if len(moves) > 1:
            raise ToolError(_text(ctx, "ambiguous"))
        return moves

    def run(self, args, ctx):
        move = self._find(args, ctx)
        data = _row(ctx, move, _today(ctx.env))
        data.update({
            "amount_untaxed": move.amount_untaxed,
            "amount_tax": move.amount_tax,
            "lines": [{
                "description": line.name or "",
                "product_id": line.product_id.id or None,
                "quantity": line.quantity,
                "price_unit": line.price_unit,
                "discount": line.discount,
                "price_subtotal": line.price_subtotal,
                "price_total": line.price_total,
            } for line in move.invoice_line_ids.filtered(lambda line: line.display_type == "product")],
        })
        url = _portal_url(move)
        if url:
            data["portal_url"] = url
        text = _text(ctx, "details", name=move.name, total=_money(move.amount_total, move.currency_id),
                     due=_money(move.amount_residual, move.currency_id), date=_iso(move.invoice_date_due) or "-")
        if url:
            text += "\n" + url
        return ToolResult(text, structured=data, record=("account.move", move.id))


@register
class CustomerBalance(CuratedTool):
    name = "customer_balance"
    area = "invoicing"
    kind = "read"
    label = _lt("Customer balance")
    description = _lt(
        "What a customer owes: total receivable and the overdue part, in the company currency. "
        "partner_id may be a contact; its company's balance is returned."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {"partner_id": id_schema(_lt("Customer id (find_contact)."), title=_lt("Customer"))},
            "required": ["partner_id"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"account.move": INVOICE_FIELDS, **BALANCE_FIELDS}

    def field_access_refusal(self, ctx):
        return _text(ctx, "no_balance_rights")

    def run(self, args, ctx):
        partner = visible_records(ctx, "res.partner", [args["partner_id"]]).commercial_partner_id
        partner = visible_records(ctx, "res.partner", [partner.id])
        return _balance(ctx, partner)


@register
class MyInvoices(CuratedTool):
    name = "my_invoices"
    area = "invoicing"
    kind = "read"
    requires_identity = True
    label = _lt("My invoices")
    description = _lt(
        "The chatting customer's own posted invoices and credit notes, newest first. Invoices not fully paid "
        "carry portal_url: a non-expiring link the customer can open to view the invoice (and pay online when "
        "online payment is set up in Odoo). Works only for clients that send Gental's verified customer "
        "identity; the customer is never chosen by argument."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "unpaid_only": {"type": "boolean", "title": _lt("Unpaid only"),
                                "description": _lt("Only invoices not fully paid yet.")},
                "limit": {"type": "integer", "minimum": 1, "maximum": MY_MAX, "title": _lt("How many"),
                          "description": _lt("Default 5.")},
            },
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"account.move": INVOICE_FIELDS + PORTAL_FIELDS, "res.partner": ["commercial_partner_id"]}

    def run(self, args, ctx):
        limit = _limit(ctx, args, 5, MY_MAX)
        customer = _matched_customer(ctx)
        if customer is None:
            return ToolResult(_text(ctx, "no_match"), structured={"invoices": []})
        domain = _customer_domain() + [("state", "=", "posted"), ("commercial_partner_id", "=", customer.id)]
        if args.get("unpaid_only"):
            domain.append(("payment_state", "in", OPEN_PAYMENT_STATES))
        moves = ctx.env["account.move"].search(domain, order="invoice_date desc, id desc", limit=limit)
        return _invoices_result(ctx, moves, empty_key="my_none", with_portal_url=True)


@register
class MyBalance(CuratedTool):
    name = "my_balance"
    area = "invoicing"
    kind = "read"
    requires_identity = True
    label = _lt("My balance")
    description = _lt(
        "What the chatting customer owes: total receivable and the overdue part, in the company currency. "
        "Works only for clients that send Gental's verified customer identity."
    )

    def input_schema(self, ctx):
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def required_fields(self):
        return {"account.move": INVOICE_FIELDS, **BALANCE_FIELDS}

    def field_access_refusal(self, ctx):
        return _text(ctx, "balance_unavailable")

    def run(self, args, ctx):
        customer = _matched_customer(ctx)
        if customer is None:
            return ToolResult(_text(ctx, "no_match"), structured={})
        return _balance(ctx, customer, no_rights="balance_unavailable")


@register
class SendInvoice(CuratedTool):
    name = "send_invoice"
    area = "invoicing"
    kind = "write"
    label = _lt("Email an invoice")
    description = _lt(
        "Email a posted customer invoice or credit note, with its PDF, through Odoo's own invoice sending. "
        "The email is addressed only to the invoice's customer; the invoice's followers get Odoo's usual "
        "notification. Safe to retry: the same action key never emails twice."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {"invoice_id": id_schema(title=_lt("Invoice"))},
            "required": ["invoice_id"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {
            "account.move": INVOICE_FIELDS + ["is_move_sent", "message_ids"],
            # 18+; 17.0 sends with ``account.move.send`` (``_new_send_wizard``), covered by the send tests there.
            "account.move.send.wizard": ["move_id", "sending_methods", "mail_partner_ids", "alerts"],
            "mail.message": ["model", "res_id", "message_type", "subtype_id"],
            "res.partner": ["email"],
        }

    def run(self, args, ctx):
        move = customer_invoice(ctx, args["invoice_id"])
        recipient = move.partner_id

        def send():  # only runs when the action key hasn't sent this invoice yet
            if move.state != "posted":
                raise ToolError(_text(ctx, "not_posted"))
            if not recipient.email:
                raise ToolError(_text(ctx, "no_email"))
            return self._send(ctx, move, recipient)

        message, created = once_per_key(ctx, "mail.message", send, key_model=f"account.move.send:{move.id}")
        text = _text(ctx, "sent" if created else "already_sent", name=move.name, email=recipient.email)
        return ToolResult(text, structured={
            "invoice_id": move.id, "number": move.name, "message_id": message.id, "email": recipient.email,
        }, record=("account.move", move.id))

    @staticmethod
    def _send(ctx, move, recipient):
        """Send ``move`` by email with Odoo's send wizard as the acting user; returns the chatter message.

        The mail is queued (sent by Odoo's mail cron after commit), so a failed call rolls it back. Errors
        Odoo raises before sending are refusals; a failure once sending started is ``outcome unknown``."""
        env = ctx.env(context=dict(ctx.env.context, active_model="account.move", active_ids=move.ids,
                                   mail_notify_force_send=False))
        try:
            wizard = SendInvoice._new_send_wizard(env, move, recipient)
            if wizard.mail_partner_ids != recipient:  # never anyone else (e.g. template defaults)
                wizard.mail_partner_ids = recipient
            if "alerts" in wizard._fields and wizard.alerts:  # 18+
                wizard._raise_danger_alerts(wizard.alerts)
        except curated.ODOO_ERRORS as error:
            raise curated.odoo_refusal(ctx, error) from None
        last = max(move.message_ids.ids or [0])
        comment = env.ref("mail.mt_comment")
        try:
            wizard.action_send_and_print()
            message = env["mail.message"].search([
                ("model", "=", "account.move"), ("res_id", "=", move.id), ("id", ">", last),
                ("message_type", "=", "comment"), ("subtype_id", "=", comment.id),
            ], order="id desc", limit=1)
        except Exception as error:  # noqa: BLE001 - once sending started, a failure can't prove nothing went out
            if ctx.is_retryable is not None and ctx.is_retryable(error):
                raise  # the host replays the whole request (the savepoint undoes the queued mail)
            raise ToolOutcomeUnknown(_text(ctx, "unknown")) from None
        if not message:
            raise ToolOutcomeUnknown(_text(ctx, "unknown"))
        return message

    @staticmethod
    def _new_send_wizard(env, move, recipient):
        """Odoo's send wizard for ``move``, email only, addressed to ``recipient``."""
        Wizard = env[compat.invoice_send_model(env)]
        if "move_id" in Wizard._fields:  # 18+: account.move.send.wizard, one invoice
            return Wizard.create({
                "move_id": move.id,
                "sending_methods": ["email"],
                "mail_partner_ids": [(6, 0, recipient.ids)],
            })
        # 17.0: account.move.send takes the invoices and its template from the caller (as the Send button does).
        # The optional send checkboxes (download, post, ...) are off; localisation e-invoicing (e.g. ZATCA) still
        # runs as with Odoo's Send button, as it does on 18+.
        template = env.ref(move._get_mail_template(), raise_if_not_found=False)
        return Wizard.create({
            **Wizard._get_wizard_vals_restrict_to({"checkbox_send_mail": True}),
            "move_ids": [(6, 0, move.ids)],
            "mail_template_id": template.id if template else False,
            "mail_partner_ids": [(6, 0, recipient.ids)],
        })
