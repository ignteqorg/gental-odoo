"""Inventory tools: ``stock_availability``, ``delivery_status``, ``my_deliveries`` (identity-scoped) and
``get_delivery_document``.

All reads run as the acting user. Deliveries are linked to an order by the picking's ``origin`` (the
order reference), so the tools work without the Sales app. ``my_deliveries`` never takes a phone or a
customer: the customer is whoever Gental verified (``identity``), and no match means no data.
"""
from odoo.exceptions import AccessError
from odoo.tools.translate import _lt

from ..core.tools import ToolError, ToolResult
from ..services import areas, curated, identity
from ..services.outbox import DocumentTool
from .common import selection_labels, text as _t

MAX_PRODUCTS = 5
MAX_WAREHOUSES = 20
MAX_DELIVERIES = 10

NEED_PRODUCT = _lt("Give a product_id or a query.")
NO_PRODUCT = _lt("No product matches.")
NO_DELIVERIES = _lt("No deliveries found.")
NO_ORDER = _lt("No order with this reference.")
STOCK_LINE = _lt("%(product)s at %(warehouse)s: on hand %(on_hand)s, forecast %(forecast)s, free %(free)s")
DELIVERY_LINE = _lt("%(name)s: %(state)s, scheduled %(scheduled)s")
DELIVERY_LINE_DONE = _lt("%(name)s: %(state)s, scheduled %(scheduled)s, delivered %(done)s")
DELIVERY_TRACKING = _lt(", tracking %(tracking)s")
DELIVERY_ORDER = _lt(", order %(order)s")
# What ``_delivery_rows`` reads; the tracking reference comes with the shipping connectors (``stock_delivery``).
PICKING_FIELDS = ["name", "state", "scheduled_date", "date_done", "origin", "picking_type_code",
                  curated.AppField("carrier_tracking_ref", ("stock_delivery", "delivery"))]


def _stamp(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _state_label(ctx, picking):
    return selection_labels(ctx, picking, "state").get(picking.state, picking.state)


def _delivery_rows(ctx, pickings, with_order=False):
    tracking = "carrier_tracking_ref" in pickings._fields
    rows, lines = [], []
    for picking in pickings:
        row = {
            "name": picking.name,
            "state": picking.state,
            "state_label": _state_label(ctx, picking),
            "scheduled_date": _stamp(picking.scheduled_date),
            "done_date": _stamp(picking.date_done),
        }
        values = {"name": row["name"], "state": row["state_label"], "scheduled": row["scheduled_date"]}
        if row["done_date"]:
            line = _t(ctx, DELIVERY_LINE_DONE, done=row["done_date"], **values)
        else:
            line = _t(ctx, DELIVERY_LINE, **values)
        if tracking and picking.carrier_tracking_ref:
            row["tracking_ref"] = picking.carrier_tracking_ref
            line += _t(ctx, DELIVERY_TRACKING, tracking=row["tracking_ref"])
        if with_order and picking.origin:
            row["order_ref"] = picking.origin
            line += _t(ctx, DELIVERY_ORDER, order=row["order_ref"])
        rows.append(row)
        lines.append(line)
    return rows, lines


@curated.register
class StockAvailability(curated.CuratedTool):
    name = "stock_availability"
    area = "inventory"
    kind = "read"
    label = _lt("Stock availability")
    description = _lt(
        "Stock of a product per warehouse: on hand, forecast (virtual) and free quantity. Give a product_id, or a "
        "query matching the product name or internal reference; optionally limit to one warehouse_id."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "product_id": curated.id_schema(title=_lt("Product")),
                "query": {"type": "string", "minLength": 1, "maxLength": 100, "title": _lt("Product name or reference")},
                "warehouse_id": curated.id_schema(title=_lt("Warehouse")),
            },
        }

    def required_fields(self):
        return {
            "product.product": ["name", "default_code", "qty_available", "virtual_available", "free_qty"],
            "stock.warehouse": ["name"],
        }

    def run(self, args, ctx):
        Product = ctx.env["product.product"]
        if args.get("product_id"):
            products = curated.visible_records(ctx, "product.product", [args["product_id"]])
        elif (args.get("query") or "").strip():
            query = args["query"].strip()
            products = Product.search(["|", ("name", "ilike", query), ("default_code", "ilike", query)], limit=MAX_PRODUCTS)
        else:
            raise ToolError(_t(ctx, NEED_PRODUCT))
        if not products:
            return ToolResult(_t(ctx, NO_PRODUCT), structured={"products": [], "lines": []})
        if args.get("warehouse_id"):
            warehouses = curated.visible_records(ctx, "stock.warehouse", [args["warehouse_id"]])
        else:
            warehouses = ctx.env["stock.warehouse"].search([], limit=MAX_WAREHOUSES)
        out, lines = [], []
        for product in products:
            per_warehouse = []
            for warehouse in warehouses:
                # ``warehouse_id`` from 19.0, ``warehouse`` before: the stock quantities read the one they know.
                scoped = product.with_context(warehouse_id=warehouse.id, warehouse=warehouse.id)
                row = {
                    "warehouse_id": warehouse.id,
                    "warehouse": warehouse.name,
                    "qty_available": scoped.qty_available,
                    "virtual_available": scoped.virtual_available,
                    "free_qty": scoped.free_qty,
                }
                per_warehouse.append(row)
                lines.append(_t(
                    ctx, STOCK_LINE, product=product.name, warehouse=warehouse.name,
                    on_hand=row["qty_available"], forecast=row["virtual_available"], free=row["free_qty"],
                ))
            out.append({"id": product.id, "name": product.name, "reference": product.default_code or None,
                        "warehouses": per_warehouse})
        return ToolResult("\n".join(lines), structured={"products": out, "lines": lines})


@curated.register
class DeliveryStatus(curated.CuratedTool):
    name = "delivery_status"
    area = "inventory"
    kind = "read"
    label = _lt("Delivery status")
    description = _lt(
        "Outgoing deliveries (pickings) of an order, by the order reference: status, scheduled and done dates, "
        "and the carrier tracking reference when the delivery app is installed."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["order_ref"],
            "properties": {
                "order_ref": {"type": "string", "minLength": 1, "maxLength": 64, "title": _lt("Order reference")},
            },
        }

    def required_fields(self):
        return {"stock.picking": PICKING_FIELDS + ["company_id", curated.AppField("sale_id", ("sale_stock",))],
                "sale.order": ["name", "company_id"]}

    def run(self, args, ctx):
        ref = args["order_ref"].strip()
        domain = [("origin", "=", ref), ("picking_type_code", "=", "outgoing")]
        if "sales" in areas.installed_areas(ctx.env):
            # Resolve the order as the acting user first: an order they can't see is "not found", and only
            # that order's own deliveries (same company) are returned.
            try:
                order = ctx.env["sale.order"].search([("name", "=", ref)], limit=1)
            except AccessError:
                order = None
            if not order:
                raise ToolError(_t(ctx, NO_ORDER))
            domain.append(("company_id", "=", order.company_id.id))
            if "sale_id" in ctx.env["stock.picking"]._fields:
                domain = [("sale_id", "=", order.id), ("picking_type_code", "=", "outgoing")]
        pickings = ctx.env["stock.picking"].search(domain, order="scheduled_date, id", limit=MAX_DELIVERIES)
        if not pickings:
            return ToolResult(_t(ctx, NO_DELIVERIES), structured={"order_ref": ref, "deliveries": [], "lines": []})
        rows, lines = _delivery_rows(ctx, pickings)
        return ToolResult("\n".join(lines), structured={"order_ref": ref, "deliveries": rows, "lines": lines})


@curated.register
class MyDeliveries(curated.CuratedTool):
    name = "my_deliveries"
    area = "inventory"
    kind = "read"
    requires_identity = True
    label = _lt("My deliveries")
    description = _lt(
        "The verified customer's own outgoing deliveries, newest first. Works only for a verified customer; "
        "takes no phone or customer argument."
    )

    def input_schema(self, ctx):
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_DELIVERIES, "default": 5, "title": _lt("How many")},
            },
        }

    def required_fields(self):
        return {"stock.picking": PICKING_FIELDS + ["partner_id"]}

    def run(self, args, ctx):
        who = identity.require(ctx)
        limit = min(int(args.get("limit") or 5), MAX_DELIVERIES)
        partner = identity.match_partner(ctx.env, who.phone)
        pickings = ctx.env["stock.picking"]
        if partner:
            pickings = pickings.search(
                [("partner_id", "child_of", partner.id), ("picking_type_code", "=", "outgoing"),
                 ("state", "not in", ["draft", "cancel"])],
                order="scheduled_date desc, id desc", limit=limit,
            )
        if not pickings:
            return ToolResult(_t(ctx, NO_DELIVERIES), structured={"deliveries": [], "lines": []})
        rows, lines = _delivery_rows(ctx, pickings, with_order=True)
        return ToolResult("\n".join(lines), structured={"deliveries": rows, "lines": lines})


@curated.register
class GetDeliveryDocument(DocumentTool):
    name = "get_delivery_document"
    area = "inventory"
    model = "stock.picking"
    id_arg = "picking_id"
    id_description = _lt("Id of the delivery (transfer).")
    id_title = _lt("Delivery")
    report_xmlid = "stock.action_report_delivery"
    label = _lt("Delivery slip PDF")
    description = _lt(
        "Makes the delivery slip PDF of a delivery and returns a download link that expires. "
        "Give the customer the link exactly as returned."
    )
