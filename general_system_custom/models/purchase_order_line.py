from odoo import models, fields, api

class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    retail_price = fields.Monetary(string='Retail', currency_field='currency_id')

    disc_code_1 = fields.Float(string='Discount 1 %', digits='Discount')
    disc_code_2 = fields.Float(string='Discount 2 %', digits='Discount')

    surcharge = fields.Monetary(string='Surcharge', currency_field='currency_id')

    # Part 3: Inbound Reconciliation - Track allocated quantities
    qty_split = fields.Float(
        string='Qty Allocated',
        default=0.0,
        help="Quantity allocated through delivery imports (reserved for this PO)"
    )

    qty_open = fields.Float(
        string='Open Qty',
        compute='_compute_qty_open',
        store=True,
        help="Remaining quantity that can be allocated: Ordered - Received - Allocated"
    )

    @api.depends('product_qty', 'qty_received', 'qty_split')
    def _compute_qty_open(self):
        """
        Open Qty = Ordered - max(Received, Already Allocated)
        This prevents double-assignment in subsequent imports
        """
        for line in self:
            spoken_for = max(line.qty_received, line.qty_split)
            line.qty_open = max(0.0, line.product_qty - spoken_for)

    @api.onchange('product_id')
    def _onchange_product_id_custom(self):
        if self.product_id:
            vendor = self.order_id.partner_id
            
            def get_percentage(disc_code_record):
                if not disc_code_record or not vendor:
                    return 0.0
                # Look for a value line for this specific vendor
                value_line = disc_code_record.value_ids.filtered(lambda v: v.partner_id == vendor)
                if value_line:
                    return value_line[0].percentage
                return 0.0

            # Apply Logic
            self.disc_code_1 = get_percentage(self.product_id.disc_code_1)
            self.disc_code_2 = get_percentage(self.product_id.disc_code_2)
            self.surcharge = self.product_id.surcharge

            sale_price_found = False
            if self.order_id.sale_order_id:
                so_line = self.order_id.sale_order_id.order_line.filtered(
                    lambda l: l.product_id == self.product_id
                )
                if so_line:
                    self.retail_price = so_line[0].price_unit
                    sale_price_found = True
            
            if not sale_price_found:
                self.retail_price = self.price_unit

            self._recompute_final_price()

    @api.onchange('retail_price', 'disc_code_1', 'disc_code_2', 'surcharge')
    def _recompute_final_price(self):
        """
        Calculates: ((Retail * (1-D1%)) * (1-D2%)) + Surcharge
        """
        for line in self:
            # Use the Float values directly
            d1_pct = line.disc_code_1 or 0.0
            d2_pct = line.disc_code_2 or 0.0

            price_after_d1 = line.retail_price * (1 - (d1_pct / 100.0))
            
            price_after_d2 = price_after_d1 * (1 - (d2_pct / 100.0))
            
            final_price = price_after_d2 + line.surcharge

            line.price_unit = final_price 
    
    def write(self, vals):
        res = super(PurchaseOrderLine, self).write(vals)
        if any(field in vals for field in ['retail_price', 'disc_code_1', 'disc_code_2', 'surcharge']):
            self._recompute_final_price()
            
        return res
