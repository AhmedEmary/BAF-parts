from odoo import models, fields, api


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    retail_price = fields.Monetary(string='Retail', currency_field='currency_id', compute='_compute_custom_discounts', store=True, readonly=False, precompute=True)
    disc_code_1 = fields.Float(string='Discount 1 %', digits='Discount', compute='_compute_custom_discounts', store=True, readonly=True, precompute=True)
    disc_code_2 = fields.Float(string='Discount 2 %', digits='Discount', compute='_compute_custom_discounts', store=True, readonly=True, precompute=True)
    surcharge = fields.Monetary(string='Surcharge', currency_field='currency_id', compute='_compute_custom_discounts', store=True, readonly=True, precompute=True)

    price_unit = fields.Float(
        string="Unit Price",
        compute='_compute_custom_discounts',
        digits='Product Price',
        store=True,
        readonly=False,
        precompute=True,
    )

    @api.depends('product_id', 'order_id.partner_id', 'product_uom_qty')
    def _compute_custom_discounts(self):
        for line in self:
            if not line.product_id:
                line.retail_price = 0.0
                line.disc_code_1 = 0.0
                line.disc_code_2 = 0.0
                line.surcharge = 0.0
                continue

            product_sudo = line.product_id.sudo()
            partner = (
                getattr(line, 'order_partner_id', False)
                or line.order_id.partner_id
                or self.env.user.partner_id
            )
            partner_sudo = partner.sudo()._origin

            def get_percentage(disc_code_record):
                if not disc_code_record:
                    return 0.0
                if partner_sudo:
                    valid_ids = [partner_sudo.id]
                    if partner_sudo.commercial_partner_id:
                        valid_ids.append(partner_sudo.commercial_partner_id.id)
                    specific = disc_code_record.sudo().value_ids.filtered(
                        lambda v: v.partner_id.id in valid_ids
                    )
                    if specific:
                        return specific[0].percentage
                fallback = disc_code_record.sudo().value_ids.filtered(lambda v: not v.partner_id)
                return fallback[0].percentage if fallback else 0.0

            retail = product_sudo.list_price
            d1 = get_percentage(product_sudo.disc_code_1)
            d2 = get_percentage(product_sudo.disc_code_2)
            surge = product_sudo.surcharge or 0.0

            line.retail_price = retail
            line.disc_code_1 = d1
            line.disc_code_2 = d2
            line.surcharge = surge

            if line.state in ['cancel'] or line.qty_invoiced > 0:
                continue

            line.price_unit = retail * (1 - d1 / 100.0) * (1 - d2 / 100.0) + surge
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
