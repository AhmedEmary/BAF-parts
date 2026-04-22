from odoo import models, fields, api


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    retail_price = fields.Monetary(
        string='Retail',
        currency_field='currency_id',
        compute='_compute_baf_price',
        store=True,
        readonly=False,
        precompute=True,
    )
    surcharge = fields.Monetary(
        string='Surcharge',
        currency_field='currency_id',
        compute='_compute_baf_price',
        store=True,
        readonly=True,
        precompute=True,
    )
    price_unit = fields.Float(
        string="Unit Price",
        compute='_compute_baf_price',
        digits='Product Price',
        store=True,
        readonly=False,
        precompute=True,
    )

    @api.depends('product_id', 'order_id.partner_id', 'product_uom_qty')
    def _compute_baf_price(self):
        for line in self:
            if not line.product_id:
                line.retail_price = 0.0
                line.surcharge = 0.0
                continue

            product = line.product_id.sudo()
            partner = (
                getattr(line, 'order_partner_id', False)
                or line.order_id.partner_id
                or self.env.user.partner_id
            )

            line.retail_price = product.list_price
            line.surcharge = product.surcharge or 0.0

            if line.state in ['cancel'] or line.qty_invoiced > 0:
                continue

            line.price_unit = product.baf_get_sales_price(partner=partner.sudo()._origin)
            line.discount = 0.0

    def _compute_price_unit(self):
        standard = self.filtered(
            lambda l: not l.product_id or l.state in ['cancel'] or l.qty_invoiced > 0
        )
        if standard:
            super(SaleOrderLine, standard)._compute_price_unit()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'price_unit' in vals and vals.get('order_id'):
                order = self.env['sale.order'].sudo().browse(vals['order_id'])
                if order.website_id:
                    vals.pop('price_unit')
        return super().create(vals_list)

    def write(self, vals):
        if 'price_unit' in vals and any(line.order_id.website_id for line in self):
            vals.pop('price_unit')
        return super().write(vals)

    def _get_cart_display_price(self):
        self.ensure_one()
        return self.price_unit * self.product_uom_qty

    def _should_show_strikethrough_price(self):
        if self.order_id.website_id:
            return False
        return super()._should_show_strikethrough_price()
