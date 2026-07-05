from odoo import models, fields, api


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    retail_price = fields.Monetary(string='Retail', currency_field='currency_id')
    surcharge = fields.Monetary(string='Surcharge', currency_field='currency_id')

    # ── BAF pricing breakdown (snapshot of how price_unit was derived) ───────
    baf_discount_code = fields.Char(
        string='Discount Code',
        help="Discount code that was used to look up this line's purchase price. "
             "Snapshotted from the product when the line was created.",
    )
    baf_discount_pct = fields.Float(
        string='Discount %',
        digits=(6, 4),
        help="Discount percentage applied from the BAF discount table.",
    )
    baf_column_key = fields.Char(
        string='Column Key',
        help="Full discount-table column key used (e.g. SUP1_BMW_T12).",
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
            line.baf_discount_code = line.product_id.baf_discount_code or False
            details = line.product_id.baf_get_purchase_price_details(
                line.order_id.partner_id
            )
            if details:
                line.price_unit = details['price']
                line.baf_discount_pct = details['discount_pct']
                line.baf_column_key = details['column_key']
            else:
                # Vendor gives no discount for this product -> full retail (UPE).
                line.price_unit = line.product_id.list_price
                line.baf_discount_pct = 0.0
                line.baf_column_key = False

    @api.onchange('product_id')
    def _onchange_product_id_custom(self):
        if not self.product_id:
            return

        self.surcharge = self.product_id.surcharge
        self.baf_discount_code = self.product_id.baf_discount_code or False

        details = self.product_id.baf_get_purchase_price_details(
            self.order_id.partner_id
        )

        # Retail is always the product's list price — never the sale order
        # price_unit, since the SO line may carry a customer-specific discount
        # that must not flow into the purchase cost calculation.
        self.retail_price = self.product_id.list_price

        if details:
            self.price_unit = details['price']
            self.baf_discount_pct = details['discount_pct']
            self.baf_column_key = details['column_key']
        else:
            # No BAF discount for this vendor -> full retail (UPE).
            self.price_unit = self.product_id.list_price
            self.baf_discount_pct = 0.0
            self.baf_column_key = False

    def write(self, vals):
        res = super().write(vals)
        if 'retail_price' in vals or 'surcharge' in vals:
            for line in self:
                details = line.product_id.baf_get_purchase_price_details(
                    line.order_id.partner_id
                )
                if details:
                    line.price_unit = details['price']
                    line.baf_discount_pct = details['discount_pct']
                    line.baf_column_key = details['column_key']
        return res
