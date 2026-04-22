from odoo import models, fields, api


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    retail_price = fields.Monetary(string='Retail', currency_field='currency_id')
    surcharge = fields.Monetary(string='Surcharge', currency_field='currency_id')

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

    @api.onchange('product_id')
    def _onchange_product_id_custom(self):
        if not self.product_id:
            return

        self.surcharge = self.product_id.surcharge

        # Compute BAF purchase price if the product has a DE table route
        baf_price = self.product_id.baf_get_purchase_price(
            supplier_code=self._get_supplier_code()
        )

        sale_price_found = False
        if self.order_id.sale_order_id:
            so_line = self.order_id.sale_order_id.order_line.filtered(
                lambda l: l.product_id == self.product_id
            )
            if so_line:
                self.retail_price = so_line[0].price_unit
                sale_price_found = True

        if not sale_price_found:
            self.retail_price = self.product_id.list_price

        if baf_price is not None:
            self.price_unit = baf_price
        else:
            # EU direct — keep whatever the vendor pricelist resolved
            pass

    def _get_supplier_code(self):
        """
        Resolve the BAF supplier code ('SUP1', 'SUP2', 'SUP3') from the
        vendor on the purchase order. Extend this method to map vendor IDs
        to supplier codes as needed.
        """
        vendor = self.order_id.partner_id if self.order_id else False
        if not vendor:
            return 'SUP1'
        # Default mapping — customise by adding vendor-specific logic here
        return getattr(vendor, 'baf_supplier_code', 'SUP1') or 'SUP1'

    def write(self, vals):
        res = super().write(vals)
        if 'retail_price' in vals or 'surcharge' in vals:
            for line in self:
                baf_price = line.product_id.baf_get_purchase_price(
                    supplier_code=line._get_supplier_code()
                )
                if baf_price is not None:
                    line.price_unit = baf_price
        return res
