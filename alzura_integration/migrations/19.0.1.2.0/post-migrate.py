"""Repair Alzura order lines repriced by the BAF catalog lookup.

Until _baf_skip_repricing was honoured, _compute_baf_price could re-fire on an
imported Alzura line and overwrite the marketplace price with a BAF
retail-minus-discount price (stamping baf_applied_column_key /
baf_applied_discount_pct), and flatten the shipping / payment / alzura_charge
lines to 0.00 because their service product has no list price.

Two things are cleaned up here:

- The BAF stamps are dropped: no catalog discount applies to an Alzura line.
- price_subtotal / price_total and the order amounts are recomputed, since
  observed orders carry subtotals frozen at the BAF figure while price_unit
  already holds the correct Alzura value.

price_unit itself is NOT rewritten. On some orders it is already correct, and
technical_price_unit is core bookkeeping that cannot be trusted as a general
backup, so lines whose two values disagree are only logged for review.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    SaleOrder = env["sale.order"]

    # Orders stamped with any Alzura source, plus early imports that predate
    # the stamp but are still recognisable by their charge line.
    domain = [("so_source", "in", SaleOrder._alzura_source_ids().ids)]
    charge = env["product.product"].search(
        [("default_code", "=", "ALZURA-CHARGE")], limit=1
    )
    if charge:
        domain = [
            "|",
            ("order_line.product_id", "=", charge.id),
        ] + domain

    orders = SaleOrder.search(domain)
    lines = orders.order_line.filtered(lambda l: not l.display_type)
    if not lines:
        return

    fields_ = env["sale.order.line"]._fields
    stamps = {
        "baf_applied_column_key": None,
        "baf_applied_discount_pct": 0.0,
    }
    stamps = {name: val for name, val in stamps.items() if name in fields_}
    if stamps:
        # Written in SQL: these are stored computed fields with no inverse.
        assignments = ", ".join("%s = %%s" % name for name in stamps)
        cr.execute(
            "UPDATE sale_order_line SET %s WHERE id IN %%s" % assignments,
            list(stamps.values()) + [tuple(lines.ids)],
        )
        lines.invalidate_recordset(list(stamps))

    suspect = lines.filtered(
        lambda l: l.technical_price_unit
        and abs(l.technical_price_unit - l.price_unit) > 0.005
    )
    for line in suspect:
        _logger.warning(
            "Alzura repair: %s line %s (%s) has price_unit %s but "
            "technical_price_unit %s - review against the Alzura payload.",
            line.order_id.name,
            line.id,
            line.name,
            line.price_unit,
            line.technical_price_unit,
        )

    lines.modified(["price_unit", "product_uom_qty", "discount", "tax_ids"])
    env.flush_all()
    _logger.info(
        "Alzura repair: recomputed %s line(s) across %s order(s); "
        "%s line(s) flagged for price review.",
        len(lines),
        len(orders),
        len(suspect),
    )
