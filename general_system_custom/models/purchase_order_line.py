from odoo import models, fields, api


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    retail_price = fields.Monetary(string='Retail', currency_field='currency_id')
    surcharge = fields.Monetary(string='Surcharge', currency_field='currency_id')

    # ── BAF pricing breakdown (snapshot of how price_unit was derived) ───────
    # Computed + stored (editable) so they persist reliably through create and
    # order confirmation. Setting them as side-effects of the price_unit compute
    # did not persist (Odoo only flushes the field a compute owns).
    baf_discount_code = fields.Char(
        string='Discount Code',
        compute='_compute_baf_pricing_snapshot', store=True, readonly=False,
        help="Discount code that was used to look up this line's purchase price. "
             "Snapshotted from the product when the line was created.",
    )
    baf_discount_pct = fields.Float(
        string='Discount %',
        digits=(6, 4),
        compute='_compute_baf_pricing_snapshot', store=True, readonly=False,
        help="Discount percentage applied from the BAF discount table.",
    )
    baf_column_key = fields.Char(
        string='Column Key',
        compute='_compute_baf_pricing_snapshot', store=True, readonly=False,
        help="Discount-table column key used (e.g. BMW_T12).",
    )

    # Inbound reconciliation
    qty_split = fields.Float(
        string='Qty Allocated',
        default=0.0,
        help="Quantity allocated through delivery imports (reserved for this PO)",
    )
    qty_open = fields.Float(
        string='Open Qty',
        compute='_compute_qty_open',
        store=True,
        help="Remaining quantity that can be allocated: Ordered - Received - Allocated",
    )

    @api.depends('product_qty', 'qty_received', 'qty_split')
    def _compute_qty_open(self):
        for line in self:
            spoken_for = max(line.qty_received, line.qty_split)
            line.qty_open = max(0.0, line.product_qty - spoken_for)

    @api.depends('product_id', 'order_id.partner_id')
    def _compute_baf_pricing_snapshot(self):
        """Record how this line's purchase price was derived (code / % / column)
        for the vendor on the order. Own compute so the values persist."""
        for line in self:
            # price_unit is frozen once invoiced; freeze the snapshot with it,
            # or it would end up describing a price the line never charged.
            if line.invoice_lines:
                continue
            product = line.product_id
            if not product:
                line.baf_discount_code = False
                line.baf_discount_pct = 0.0
                line.baf_column_key = False
                continue
            details = product.baf_get_purchase_price_details(line.order_id.partner_id)
            line.baf_discount_code = product.baf_discount_code or False
            line.baf_discount_pct = details['discount_pct'] if details else 0.0
            line.baf_column_key = details['column_key'] if details else False

    @api.depends('product_qty', 'product_uom_id', 'company_id',
                 'order_id.partner_id', 'product_id')
    def _compute_price_unit_and_date_planned_and_name(self):
        # Odoo's core compute reprices the line from the vendor pricelist /
        # standard cost and would overwrite the BAF discounted price. Run it
        # first, then make the per-vendor BAF price authoritative for any line
        # the engine can price (matrix / codes / direct).
        super()._compute_price_unit_and_date_planned_and_name()
        for line in self:
            if not line.product_id or line.invoice_lines:
                continue
            details = line.product_id.baf_get_purchase_price_details(
                line.order_id.partner_id
            )
            # BAF price wins; no discount for this vendor -> full retail (UPE).
            line.price_unit = details['price'] if details else line.product_id.list_price

    @api.onchange('product_id')
    def _onchange_product_id_custom(self):
        if not self.product_id:
            return
        # Retail is always the product's list price — never the sale order
        # price_unit, since the SO line may carry a customer-specific discount
        # that must not flow into the purchase cost calculation.
        self.surcharge = self.product_id.surcharge
        self.retail_price = self.product_id.list_price
