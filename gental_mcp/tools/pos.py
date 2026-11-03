"""Point of Sale tools: ``pos_sales_summary`` (aggregates only) and ``find_pos_order`` (exact receipt).

Reads run as the acting user. The summary never returns individual customers or orders; the receipt
lookup matches ``pos_reference`` or ``name`` exactly (no wildcards) and returns no customer data.
"""
from datetime import date, datetime, time, timedelta

import pytz
from odoo.tools.translate import _lt

from ..core.tools import ToolError, ToolResult
from ..services import curated
from .common import selection_labels, text as _t

MAX_DAYS = 92
TOP_PRODUCTS = 5
MAX_MATCHES = 5
SOLD_STATES = ("paid", "done", "invoiced")

BAD_RANGE = _lt("date_from must not be after date_to.")
TOO_LONG = _lt("The range can be at most 92 days.")
BAD_DATE = _lt("Dates must look like 2026-11-02.")
NO_SALES = _lt("No point of sale orders in this range.")
NO_RECEIPT = _lt("No point of sale order has this receipt.")
SHOP_LINE = _lt("%(shop)s: %(count)s orders, total %(total)s %(currency)s")
TOP_LINE = _lt("Top products: %(products)s")
ORDER_LINE = _lt("%(ref)s: %(state)s, total %(total)s %(currency)s")


def _day(ctx, value, default):
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(_t(ctx, BAD_DATE))


def _user_tz(ctx):
    return pytz.timezone(ctx.env.user.tz or "UTC")


def _utc_bounds(ctx, first, last):
    """UTC naive datetimes [start, end) covering ``first``..``last`` in the acting user's timezone."""
    tz = _user_tz(ctx)

    def to_utc(day):
        return tz.localize(datetime.combine(day, time.min)).astimezone(pytz.utc).replace(tzinfo=None)

    return to_utc(first), to_utc(last + timedelta(days=1))


@curated.register
class PosSalesSummary(curated.CuratedTool):
    name = "pos_sales_summary"
    area = "pos"
    kind = "read"
    title_en = "Point of sale summary"
    title_ar = "ملخص نقاط البيع"
    description = (
        "Point of sale totals per shop (POS config) for a date range, at most 92 days, today by default: order "
        "count, sales total and the top 5 products by quantity. Aggregates only, no customer details."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "date_from": {"type": "string", "format": "date", "title": "From (YYYY-MM-DD)"},
                "date_to": {"type": "string", "format": "date", "title": "To (YYYY-MM-DD)"},
                "config_id": curated.id_schema(title="Shop (POS config) id"),
            },
        }

    def required_fields(self):
        return {
            "pos.order": ["config_id", "state", "date_order", "amount_total"],
            "pos.order.line": ["order_id", "product_id", "qty"],
            "pos.config": ["name", "currency_id"],
            "product.product": ["name"],
        }

    def run(self, args, ctx):
        today = datetime.now(_user_tz(ctx)).date()
        first = _day(ctx, args.get("date_from"), today)
        last = _day(ctx, args.get("date_to"), today)
        if first > last:
            raise ToolError(_t(ctx, BAD_RANGE))
        if (last - first).days + 1 > MAX_DAYS:
            raise ToolError(_t(ctx, TOO_LONG))
        start, end = _utc_bounds(ctx, first, last)
        states = [s for s in SOLD_STATES if s in dict(ctx.env["pos.order"]._fields["state"].selection)]
        domain = [("state", "in", states), ("date_order", ">=", start), ("date_order", "<", end)]
        if args.get("config_id"):
            curated.visible_records(ctx, "pos.config", [args["config_id"]])
            domain.append(("config_id", "=", args["config_id"]))
        groups = ctx.env["pos.order"]._read_group(domain, ["config_id"], ["__count", "amount_total:sum"])
        shops, lines = [], []
        for config, count, total in groups:
            top = ctx.env["pos.order.line"]._read_group(
                [("order_id." + d[0], d[1], d[2]) for d in domain] + [("order_id.config_id", "=", config.id)],
                ["product_id"], ["qty:sum"], order="qty:sum desc", limit=TOP_PRODUCTS,
            )
            products = [{"id": p.id, "name": p.name, "qty": qty} for p, qty in top]
            currency = config.currency_id.name
            total = config.currency_id.round(total)
            shops.append({
                "config_id": config.id, "shop": config.name, "order_count": count,
                "amount_total": total, "currency": currency, "top_products": products,
            })
            lines.append(_t(ctx, SHOP_LINE, shop=config.name, count=count, total=total, currency=currency))
            if products:
                lines.append(_t(ctx, TOP_LINE, products=", ".join(f"{p['name']} ({p['qty']:g})" for p in products)))
        text = "\n".join(lines) if lines else _t(ctx, NO_SALES)
        return ToolResult(text, structured={
            "date_from": first.isoformat(), "date_to": last.isoformat(), "shops": shops, "lines": lines,
        })


@curated.register
class FindPosOrder(curated.CuratedTool):
    name = "find_pos_order"
    area = "pos"
    kind = "read"
    title_en = "Find a point of sale order"
    title_ar = "البحث عن طلب نقطة بيع"
    description = (
        "Finds a point of sale order by its exact receipt reference (as printed on the receipt, or the order "
        "name) and returns its lines, total, payment methods and state. No wildcard search."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["receipt"],
            "properties": {
                "receipt": {"type": "string", "minLength": 1, "maxLength": 64, "title": "Receipt reference"},
            },
        }

    def required_fields(self):
        return {
            "pos.order": ["name", "pos_reference", "state", "date_order", "amount_total", "lines", "payment_ids",
                          "config_id"],
            "pos.order.line": ["full_product_name", "qty", "price_unit", "price_subtotal_incl"],
            "pos.payment": ["payment_method_id"],
            "pos.payment.method": ["name"],
            "pos.config": ["currency_id"],
        }

    def run(self, args, ctx):
        receipt = args["receipt"].strip()
        orders = ctx.env["pos.order"].search(
            ["|", ("pos_reference", "=", receipt), ("name", "=", receipt)], order="id", limit=MAX_MATCHES
        ) if receipt else ctx.env["pos.order"]
        if not orders:
            return ToolResult(_t(ctx, NO_RECEIPT), structured={"orders": [], "lines": []})
        out, lines = [], []
        for order in orders:
            labels = selection_labels(ctx, order, "state")
            currency = order.config_id.currency_id.name
            methods = []
            for payment in order.payment_ids:
                if payment.payment_method_id.name not in methods:
                    methods.append(payment.payment_method_id.name)
            ref = order.pos_reference or order.name
            out.append({
                "reference": ref,
                "name": order.name,
                "state": order.state,
                "state_label": labels.get(order.state, order.state),
                "date": order.date_order.strftime("%Y-%m-%dT%H:%M:%SZ") if order.date_order else None,
                "amount_total": order.amount_total,
                "currency": currency,
                "payment_methods": methods,
                "lines": [{
                    "product": line.full_product_name, "qty": line.qty,
                    "price_unit": line.price_unit, "total": line.price_subtotal_incl,
                } for line in order.lines],
            })
            lines.append(_t(ctx, ORDER_LINE, ref=ref, state=labels.get(order.state, order.state),
                            total=order.amount_total, currency=currency))
        return ToolResult("\n".join(lines), structured={"orders": out, "lines": lines})
