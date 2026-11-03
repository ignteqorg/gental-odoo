"""Sales curated tools (area ``sales``, needs the Sales app)."""
from odoo import Command
from odoo.tools import float_compare, plaintext2html
from odoo.tools.translate import _lt

from ..core import meta as gmeta
from ..core.tools import ToolError
from ..services import curated, identity, quote, result_meta
from ..services.action_keys import once_per_key
from ..services.outbox import DocumentTool
from .common import (
    html_text, iso, like_escape, localized_env, money, odoo_errors, quantity, result, selection_label, text,
)

MAX_PRODUCTS = 20
MAX_LINES = 50
MAX_MY_ORDERS = 10
MAX_QTY = 1_000_000
PRICELIST_MANAGER_GROUP = "sales_team.group_sale_manager"

PRODUCTS_FOUND = _lt("Products found: %s")
NO_PRODUCTS = _lt("No product matches this search.")
NO_LINES = _lt("Add at least one product line.")
PRICELIST_NOT_ALLOWED = _lt("Only the customer's own price list can be used here. Leave the price list out, or ask "
                            "a sales manager.")
NOT_SOLD = _lt("This product can't be sold: %s")
STANDARD_PRICES = _lt("Prices are on the standard price list: a customer's own prices are shown only to that "
                      "customer once verified, or to a sales manager.")
LABEL_UNTAXED = _lt("Untaxed amount")
LABEL_TAX = _lt("Taxes")
LABEL_TOTAL = _lt("Total")
PREVIEW_HINT = _lt("Show the customer these lines and the total. When they agree, create the quotation with "
                   "the same customer, lines and price list.")
OTHER_QUOTE = _lt("This quote was made for another customer, other lines or another price list. "
                  "Preview the quotation again.")
PRICES_CHANGED = _lt("Prices changed since the preview. Preview the quotation again and show the customer "
                     "the new total.")
QUOTATION_CREATED = _lt("Quotation %(name)s created. Total: %(total)s")
ORDER_STATE = _lt("Order %(name)s: %(state)s")
CANCELLED_CANT_CONFIRM = _lt("Order %s is cancelled, so it can't be confirmed.")
CANCEL_NEEDS_STAFF = _lt("Odoo needs a person to confirm cancelling order %s, so nothing was changed. "
                         "Staff should cancel it in Odoo.")
REASON_CANCEL_WIZARD = _lt("Cancelling this order needs a confirmation in Odoo.")
ORDER_NOT_FOUND = _lt("No order with this reference.")
MY_ORDERS = _lt("Your orders: %s")
NO_MY_ORDERS = _lt("You have no orders yet.")
NO_ACCOUNT_MATCH = _lt("We couldn't match this number to a customer account, so there are no orders to show.")


class _NeedsWizard(Exception):
    """Unwinds the cancel savepoint when Odoo asks for a confirmation wizard instead of cancelling."""


_id = curated.id_schema


def _lines_schema():
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_LINES,
        "description": "Products and quantities, at least one line.",
        "items": {
            "type": "object",
            "properties": {
                "product_id": _id("Product id from search_products."),
                "qty": {"type": "number", "minimum": 1, "maximum": MAX_QTY},
            },
            "required": ["product_id", "qty"],
            "additionalProperties": False,
        },
    }


def _product_env(ctx):
    """Localized env whose product names leave out the internal code (returned separately)."""
    env = localized_env(ctx)
    return env(context=dict(env.context, display_default_code=False))


def _priced(ctx, args):
    """``(partner, Pricing, price_lines(...))`` for the quotation arguments, every id checked as the acting user."""
    lines = args["lines"]
    if not lines:
        raise ToolError(text(ctx, NO_LINES))
    partner = curated.visible_records(ctx, "res.partner", [args["partner_id"]])
    pricing = _pricing(ctx, partner, args.get("pricelist_id"))
    products = curated.visible_records(ctx, "product.product", [line["product_id"] for line in lines])
    not_sold = products.filtered(lambda p: not p.sale_ok)
    if not_sold:
        raise ToolError(text(ctx, NOT_SOLD, ", ".join(not_sold.mapped("display_name"))))
    return partner, pricing, quote.price_lines(ctx.env, partner, lines, pricing.pricelist or None)


# How a price list was chosen (R20 + R30): "chosen" by a sales manager, the "customer"'s own (a sales manager, or
# the verified customer themselves), or the "standard" one (anyone else: a public agent must never reveal another
# customer's negotiated prices by trying partner ids).
CHOSEN, CUSTOMER, STANDARD = "chosen", "customer", "standard"


class Pricing:
    def __init__(self, pricelist, basis):
        self.pricelist, self.basis = pricelist, basis

    @property
    def pricelist_id(self):
        return self.pricelist.id if self.pricelist else None


def _own_pricelist(ctx, partner):
    return partner.with_company(ctx.env.company).property_product_pricelist


def _standard_pricelist(ctx):
    """The price list a customer without one of their own gets (Odoo's default, as for the public partner)."""
    public = ctx.env.ref("base.public_partner", raise_if_not_found=False)
    pricelist = _own_pricelist(ctx, public) if public else ctx.env["product.pricelist"]
    return pricelist or ctx.env["product.pricelist"].search(
        [("company_id", "in", [ctx.env.company.id, False])], limit=1)


def _verified_customer(ctx, partner) -> bool:
    """Whether ``partner`` belongs to the customer this call's verified identity matched (never a guess)."""
    who = identity.current(ctx)
    matched = identity.match_partner(ctx.env, who.phone) if who else None
    return bool(matched and partner.commercial_partner_id == matched)


def _pricing(ctx, partner, pricelist_id) -> Pricing:
    """The price list rule. A sales manager may choose any visible price list and gets the customer's own by
    default. Anyone else gets a customer's own price list (and may name it) only when the call's verified
    identity is that customer; otherwise the standard price list, and a ``pricelist_id`` is refused."""
    manager = ctx.env.user.has_group(PRICELIST_MANAGER_GROUP)
    if pricelist_id:
        pricelist = curated.visible_records(ctx, "product.pricelist", [pricelist_id])
        if manager:
            return Pricing(pricelist, CHOSEN)
        if partner and pricelist == _own_pricelist(ctx, partner) and _verified_customer(ctx, partner):
            return Pricing(pricelist, CUSTOMER)
        raise ToolError(text(ctx, PRICELIST_NOT_ALLOWED))
    if partner is None:
        return Pricing(None, STANDARD)  # search_products without a customer: the sales price
    if manager or _verified_customer(ctx, partner):
        return Pricing(_own_pricelist(ctx, partner), CUSTOMER)
    return Pricing(_standard_pricelist(ctx), STANDARD)


def _quote_payload(ctx, args, pricing, priced) -> dict:
    """What the signed quote binds: the request (customer, lines), the price list used and why, Odoo's prices
    and the total."""
    return {
        "partner_id": args["partner_id"],
        "pricelist_id": pricing.pricelist_id,
        "pricing": pricing.basis,
        "company_id": ctx.env.company.id,
        "client_id": ctx.client_id,
        "lines": [
            {"product_id": asked["product_id"], "qty": asked["qty"], "price_unit": line["price_unit"],
             "discount": line["discount"], "taxes": line["taxes"]}
            for asked, line in zip(args["lines"], priced["lines"])
        ],
        "total": priced["amount_total"],
        "currency": priced["currency"],
    }


def _same_request(payload: dict, ctx, args, pricing) -> bool:
    """Same customer, lines, company and client, priced on the same price list under the same rule (a quote made
    for the verified customer can't be created without them, nor the other way round)."""
    asked = [(line["product_id"], line["qty"]) for line in args["lines"]]
    quoted = [(line.get("product_id"), line.get("qty")) for line in payload.get("lines") or []]
    return (
        payload.get("partner_id") == args["partner_id"]
        and payload.get("pricelist_id") == pricing.pricelist_id
        and payload.get("pricing") == pricing.basis
        and payload.get("company_id") == ctx.env.company.id
        and payload.get("client_id") == ctx.client_id
        and quoted == asked
    )


def _currency(env, code):
    return env["res.currency"].with_context(active_test=False).search([("name", "=", code)], limit=1)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same_prices(currency, priced: dict, payload: dict) -> bool:
    """Whether Odoo's prices (``price_lines`` / ``quote.summarise`` shape) are those the quote signed: currency,
    total and, line by line, unit price, discount and taxes."""
    quoted_lines = payload.get("lines") or []
    if (
        payload.get("currency") != currency.name
        or not _number(payload.get("total"))
        or currency.compare_amounts(priced["amount_total"], payload["total"]) != 0
        or len(quoted_lines) != len(priced["lines"])
    ):
        return False
    for line, quoted in zip(priced["lines"], quoted_lines):
        if not (_number(quoted.get("price_unit")) and _number(quoted.get("discount"))):
            return False
        if (
            currency.compare_amounts(line["price_unit"], quoted["price_unit"]) != 0
            or float_compare(line["discount"], quoted["discount"], precision_digits=4) != 0
            or line["taxes"] != quoted.get("taxes")
        ):
            return False
    return True


def _order_state(ctx, order) -> dict:
    order = order.with_env(localized_env(ctx))
    return {"id": order.id, "name": order.name, "state": order.state, "state_label": selection_label(order, "state")}


@curated.register
class SearchProducts(curated.CuratedTool):
    name = "search_products"
    area = "sales"
    kind = "read"
    title_en = "Search products"
    title_ar = "البحث عن منتجات"
    description = (
        "Finds products that can be sold, by name or internal code. Returns id, name, code, price and, when "
        "Inventory is installed, the quantity on hand. With partner_id, the price is on that customer's own price "
        "list only for the verified customer (or a sales manager), else on the standard price list; without it, "
        "the sales price. price_list in the result says which (customer, standard or chosen). pricelist_id is "
        "accepted only as the verified customer's own price list, or from a sales manager."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PRODUCTS, "default": 10},
                "partner_id": _id("Optional customer id (find_contact): their own prices if they are the verified "
                                  "customer."),
                "pricelist_id": _id("Optional price list id: the verified customer's own, unless you are a sales "
                                    "manager."),
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {
            "product.product": ["name", "default_code", "sale_ok", "lst_price", "currency_id",
                                curated.AppField("qty_available", ("stock",))],
            "product.pricelist": ["currency_id"],
            "res.partner": ["property_product_pricelist"],
        }

    def run(self, args, ctx):
        env = _product_env(ctx)
        query = args["query"].strip()
        partner = curated.visible_records(ctx, "res.partner", [args["partner_id"]]) if args.get("partner_id") else None
        pricing = _pricing(ctx, partner, args.get("pricelist_id"))
        pricelist = pricing.pricelist.with_env(env) if pricing.pricelist else None
        domain = [("sale_ok", "=", True), "|", ("name", "ilike", query), ("default_code", "ilike", query)]
        with odoo_errors(ctx):
            products = env["product.product"].search(domain, limit=args.get("limit") or 10)
            stock = "qty_available" in products._fields
            rows = []
            for product in products:
                if pricelist:
                    price, currency = pricelist._get_product_price(product, 1.0), pricelist.currency_id
                else:
                    price, currency = product.lst_price, product.currency_id
                row = {"id": product.id, "name": product.display_name, "code": product.default_code or "",
                       "price": currency.round(price), "currency": currency.name}
                if stock:
                    row["qty_available"] = product.qty_available
                rows.append(row)
        if not rows:
            return result(text(ctx, NO_PRODUCTS), {"products": [], "price_list": pricing.basis})
        listing = "\n".join(f"{r['name']} (#{r['id']}) · {r['price']} {r['currency']}" for r in rows)
        summary = text(ctx, PRODUCTS_FOUND, "\n" + listing)
        if partner and pricing.basis == STANDARD:
            summary += "\n" + text(ctx, STANDARD_PRICES)
        return result(summary, {"products": rows, "price_list": pricing.basis})


@curated.register
class PreviewQuotation(curated.CuratedTool):
    name = "preview_quotation"
    area = "sales"
    kind = "read"
    title_en = "Preview a quotation"
    title_ar = "معاينة عرض سعر"
    description = (
        "Prices a quotation without saving anything: Odoo's prices, discounts, taxes and total for the customer "
        "and lines. Always preview before create_quotation and show the customer the lines and total. The "
        "customer's own price list applies only when they are the verified customer (or for a sales manager); "
        "otherwise the standard price list (price_list says which). Returns lines (label and value), total and a "
        "signed quote that create_quotation checks (valid 15 minutes)."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "partner_id": _id("Customer id from find_contact."),
                "lines": _lines_schema(),
                "pricelist_id": _id("Optional price list id: the verified customer's own, or any for a sales "
                                    "manager."),
            },
            "required": ["partner_id", "lines"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return _ORDER_FIELDS

    def run(self, args, ctx):
        with odoo_errors(ctx):
            _partner, pricing, priced = _priced(ctx, args)
            names = {
                p.id: p.display_name
                for p in _product_env(ctx)["product.product"].browse(
                    [line["product_id"] for line in priced["lines"]]
                )
            }
            currency = _currency(ctx.env, priced["currency"])
        token = quote.sign(ctx.env["gental.mcp.config"].quote_secret(), _quote_payload(ctx, args, pricing, priced),
                           quote.now())
        lines = [
            {"label": f"{names.get(line['product_id'], line['name'])} × {quantity(line['qty'])}",
             "value": money(line["subtotal"], currency)}
            for line in priced["lines"]
        ]
        lines += [
            {"label": text(ctx, LABEL_UNTAXED), "value": money(priced["amount_untaxed"], currency)},
            {"label": text(ctx, LABEL_TAX), "value": money(priced["amount_tax"], currency)},
            {"label": text(ctx, LABEL_TOTAL), "value": money(priced["amount_total"], currency)},
        ]
        summary = "\n".join(f"{line['label']}: {line['value']}" for line in lines) + "\n" + text(ctx, PREVIEW_HINT)
        if pricing.basis == STANDARD:
            summary += "\n" + text(ctx, STANDARD_PRICES)
        return result(summary, {"lines": lines, "total": priced["amount_total"], "quote": token,
                                "price_list": pricing.basis})


@curated.register
class CreateQuotation(curated.CuratedTool):
    name = "create_quotation"
    area = "sales"
    kind = "write"
    title_en = "Create a quotation"
    title_ar = "إنشاء عرض سعر"
    description = (
        "Creates a draft quotation for the customer with these lines, priced by Odoo (prices are never taken "
        "from the request). Send the same customer, lines and price list as the preview_quotation the customer "
        "agreed to: when its signed quote is sent, the quotation is refused if anything or the total changed. "
        "Returns the quotation id, number and total."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "partner_id": _id("Customer id."),
                "lines": _lines_schema(),
                "pricelist_id": _id("Optional price list id, the same as in the preview."),
                "note": {"type": "string", "maxLength": 2000, "description": "Optional note on the quotation."},
            },
            "required": ["partner_id", "lines"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return _ORDER_FIELDS

    def run(self, args, ctx):
        token = ctx.meta.get(gmeta.PREVIEW_QUOTE)

        def create():
            _partner, pricing, priced = _priced(ctx, args)
            if token is not None:
                payload = quote.verify(ctx.env["gental.mcp.config"].quote_secret(), token, quote.now(), ctx.locale)
                if not _same_request(payload, ctx, args, pricing):
                    raise ToolError(text(ctx, OTHER_QUOTE))
                if not _same_prices(_currency(ctx.env, priced["currency"]), priced, payload):
                    raise ToolError(text(ctx, PRICES_CHANGED))
            values = {
                "partner_id": args["partner_id"],
                "order_line": [
                    Command.create({"product_id": line["product_id"], "product_uom_qty": line["qty"]})
                    for line in args["lines"]
                ],
            }
            if pricing.pricelist:  # always explicit: Odoo would otherwise apply the customer's own list
                values["pricelist_id"] = pricing.pricelist.id
            if (args.get("note") or "").strip():
                values["note"] = plaintext2html(args["note"].strip())
            order = ctx.env["sale.order"].create(values)
            # Re-priced by Odoo, both before writing and on the saved order: any change refuses the call
            # (the savepoint drops the order).
            if token is not None and not _same_prices(order.currency_id, quote.summarise(order), payload):
                raise ToolError(text(ctx, PRICES_CHANGED))
            return order

        # A retried action (same Gental action key, kept in the module's action-key table, never on the
        # customer-visible order) returns the quotation it already created.
        with odoo_errors(ctx):
            order, _created = once_per_key(ctx, "sale.order", create)
            total = money(order.amount_total, order.currency_id)
            data = {"id": order.id, "name": order.name, "total": order.amount_total,
                    "currency": order.currency_id.name, "state": order.state}
        return result(text(ctx, QUOTATION_CREATED, name=order.name, total=total), data, record=("sale.order", order.id))


@curated.register
class ConfirmOrder(curated.CuratedTool):
    name = "confirm_order"
    area = "sales"
    kind = "write"
    title_en = "Confirm an order"
    title_ar = "تأكيد طلب"
    description = (
        "Confirms a draft or sent quotation, turning it into a sales order. Confirming an order that is already "
        "confirmed changes nothing and returns its state."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {"order_id": _id("Quotation id.")},
            "required": ["order_id"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"sale.order": ["name", "state"]}

    def run(self, args, ctx):
        order = curated.visible_records(ctx, "sale.order", [args["order_id"]])
        with odoo_errors(ctx):
            if order.state == "cancel":
                raise ToolError(text(ctx, CANCELLED_CANT_CONFIRM, order.name))
            if order.state in ("draft", "sent"):
                order.action_confirm()
            data = _order_state(ctx, order)
        return result(text(ctx, ORDER_STATE, name=data["name"], state=data["state_label"]), data,
                      record=("sale.order", order.id))


@curated.register
class CancelOrder(curated.CuratedTool):
    name = "cancel_order"
    area = "sales"
    kind = "destructive"
    title_en = "Cancel an order"
    title_ar = "إلغاء طلب"
    description = (
        "Cancels a quotation or sales order (and its draft invoices). An optional reason is logged on the order. "
        "When Odoo needs a person to confirm the cancellation, nothing changes and staff are flagged."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {
                "order_id": _id("Order id."),
                "reason": {"type": "string", "maxLength": 1000, "description": "Optional reason, logged on the order."},
            },
            "required": ["order_id"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"sale.order": ["name", "state"]}

    def run(self, args, ctx):
        order = curated.visible_records(ctx, "sale.order", [args["order_id"]])
        with odoo_errors(ctx):
            if order.state != "cancel":
                try:
                    with ctx.env.cr.savepoint():
                        # 17.0/18.0 return the cancel wizard for any confirmed order unless this key is set; the
                        # wizard only offers to email the customer, which this tool never does (19.0 cancels).
                        outcome = order.with_context(disable_cancel_warning=True).action_cancel()
                        if isinstance(outcome, dict):  # a wizard: Odoo wants a person to decide
                            raise _NeedsWizard()
                except _NeedsWizard:
                    order.invalidate_recordset()
                    res = result(text(ctx, CANCEL_NEEDS_STAFF, order.name), _order_state(ctx, order),
                                 record=("sale.order", order.id))
                    return result_meta.staff_review(res, text(ctx, REASON_CANCEL_WIZARD))
                reason = (args.get("reason") or "").strip()
                if reason:
                    order.message_post(body=html_text(reason), subtype_xmlid="mail.mt_note")
            data = _order_state(ctx, order)
        return result(text(ctx, ORDER_STATE, name=data["name"], state=data["state_label"]), data,
                      record=("sale.order", order.id))


def _order_summary(ctx, order) -> dict:
    order = order.with_env(localized_env(ctx))
    return {
        "id": order.id,
        "name": order.name,
        "state": order.state,
        "state_label": selection_label(order, "state"),
        "date_order": iso(order.date_order),
        "amount_total": order.amount_total,
        "currency": order.currency_id.name,
    }


@curated.register
class OrderStatus(curated.CuratedTool):
    name = "order_status"
    area = "sales"
    kind = "read"
    title_en = "Order status"
    title_ar = "حالة الطلب"
    description = (
        "Looks up a quotation or sales order by its number (e.g. S00042) or the customer's reference. Returns "
        "its state, dates, invoice status and, when Inventory is installed, delivery status."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {"order_ref": {"type": "string", "minLength": 1, "maxLength": 100,
                                         "description": "Order number or customer reference."}},
            "required": ["order_ref"],
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"sale.order": ["name", "client_order_ref", "state", "date_order", "validity_date",
                               "commitment_date", "invoice_status", "amount_total", "currency_id",
                               curated.AppField("delivery_status", ("sale_stock",))]}

    def run(self, args, ctx):
        ref = like_escape(args["order_ref"].strip())
        Orders = ctx.env["sale.order"]
        with odoo_errors(ctx):
            order = Orders.search([("name", "=ilike", ref)], limit=1) or Orders.search(
                [("client_order_ref", "=ilike", ref)], order="date_order desc, id desc", limit=1
            )
            if not order:
                raise ToolError(text(ctx, ORDER_NOT_FOUND))
            data = _order_summary(ctx, order)
            order = order.with_env(localized_env(ctx))
            data.update({
                "validity_date": iso(order.validity_date),
                "commitment_date": iso(order.commitment_date),
                "invoice_status": order.invoice_status or "",
                "invoice_status_label": selection_label(order, "invoice_status"),
            })
            if "delivery_status" in order._fields:
                data["delivery_status"] = order.delivery_status or ""
                data["delivery_status_label"] = selection_label(order, "delivery_status")
        return result(text(ctx, ORDER_STATE, name=data["name"], state=data["state_label"]), data,
                      record=("sale.order", order.id))


@curated.register
class MyOrders(curated.CuratedTool):
    name = "my_orders"
    area = "sales"
    kind = "read"
    requires_identity = True
    title_en = "My orders"
    title_ar = "طلباتي"
    description = (
        "The chatting customer's own quotations and orders, newest first. Works only for clients that send "
        "Gental's verified customer identity; the customer is never chosen by argument."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_MY_ORDERS, "default": 5}},
            "additionalProperties": False,
        }

    def required_fields(self):
        return {"sale.order": ["name", "partner_id", "state", "date_order", "amount_total", "currency_id"]}

    def run(self, args, ctx):
        customer = identity.require(ctx)
        with odoo_errors(ctx):
            partner = identity.match_partner(ctx.env, customer.phone)
            if not partner:
                return result(text(ctx, NO_ACCOUNT_MATCH), {"orders": []})
            orders = ctx.env["sale.order"].search(
                [("partner_id", "child_of", partner.id)], order="date_order desc, id desc", limit=args.get("limit") or 5
            )
            rows = [_order_summary(ctx, o) for o in orders]
        if not rows:
            return result(text(ctx, NO_MY_ORDERS), {"orders": []})
        listing = "\n".join(f"{r['name']} · {r['state_label']} · {r['amount_total']} {r['currency']}" for r in rows)
        return result(text(ctx, MY_ORDERS, "\n" + listing), {"orders": rows})


@curated.register
class GetOrderDocument(DocumentTool):
    name = "get_order_document"
    area = "sales"
    model = "sale.order"
    id_arg = "order_id"
    id_description = "Id of the quotation or sales order."
    report_xmlid = "sale.action_report_saleorder"
    title_en = "Quotation or order PDF"
    title_ar = "ملف عرض السعر أو الطلب"
    description = (
        "Makes the PDF of a quotation or sales order and returns a download link that expires. "
        "Give the customer the link exactly as returned."
    )


_ORDER_FIELDS = {
    "res.partner": ["name", "property_product_pricelist"],
    "product.product": ["sale_ok", "display_name"],
    "sale.order": ["partner_id", "pricelist_id", "order_line", "note", "name", "state",
                   "amount_untaxed", "amount_tax", "amount_total", "currency_id"],
    "sale.order.line": ["product_id", "product_uom_qty", "price_unit", "discount", "price_subtotal",
                        "display_type", ("tax_ids", "tax_id")],  # renamed in 19.0
}


@curated.register
class MyOrderDocument(DocumentTool):
    name = "my_order_document"
    area = "sales"
    model = "sale.order"
    id_arg = "order_id"
    id_description = "Id of one of the customer's own quotations or orders (from my_orders)."
    report_xmlid = "sale.action_report_saleorder"
    requires_identity = True
    title_en = "My quotation or order PDF"
    title_ar = "ملف عرض سعري أو طلبي"
    description = (
        "Makes the PDF of one of the chatting customer's own quotations or orders and returns a download link "
        "that expires. Works only for clients that send Gental's verified customer identity; another "
        "customer's order is not found. Give the customer the link exactly as returned."
    )

    def required_fields(self):
        return {"sale.order": ["partner_id"]}

    def find_record(self, args, ctx):
        partner = identity.match_partner(ctx.env, identity.require(ctx).phone)
        order = ctx.env["sale.order"].search(
            [("id", "=", args[self.id_arg]), ("partner_id", "child_of", partner.id)], limit=1) if partner else None
        if not order:
            raise ToolError(curated.message("not_visible", ctx.locale))
        return order
