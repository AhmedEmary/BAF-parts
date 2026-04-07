from odoo import models, fields, api

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    retail_price = fields.Monetary(string='Retail', currency_field='currency_id', compute='_compute_custom_discounts', store=True, readonly=False, precompute=True)
    disc_code_1 = fields.Float(string='Discount 1 %', digits='Discount', compute='_compute_custom_discounts', store=True, readonly=False, precompute=True)
    disc_code_2 = fields.Float(string='Discount 2 %', digits='Discount', compute='_compute_custom_discounts', store=True, readonly=False, precompute=True)
    surcharge = fields.Monetary(string='Surcharge', currency_field='currency_id', compute='_compute_custom_discounts', store=True, readonly=False, precompute=True)

    @api.depends('product_id', 'order_id.partner_id')
    def _compute_custom_discounts(self):
        """ Fetch the breakdown data when the product or customer changes """
        for line in self:
            if not line.product_id:
                line.retail_price = 0.0
                line.disc_code_1 = 0.0
                line.disc_code_2 = 0.0
                line.surcharge = 0.0
                continue
            
            # 1. Elevate privileges so the logged-in Website User can read the discount tables
            product_sudo = line.product_id.sudo()
            order_sudo = line.order_id.sudo()
            
            # 2. Extract partner safely. Fallback to website user if cart is brand new.
            partner = order_sudo.partner_id._origin if order_sudo else False
            if not partner:
                partner = self.env.user.partner_id.sudo()
            
            def get_percentage(disc_code_record):
                if not disc_code_record:
                    return 0.0
                
                if partner:
                    valid_partner_ids = [partner.id]
                    if partner.commercial_partner_id:
                        valid_partner_ids.append(partner.commercial_partner_id.id)
                        
                    specific_line = disc_code_record.sudo().value_ids.filtered(lambda v: v.partner_id.id in valid_partner_ids)
                    if specific_line:
                        return specific_line[0].percentage

                # Step B: Fallback for all other users & unregistered visitors.
                # It looks for a line where the partner_id is NOT set (blank).
                fallback_line = disc_code_record.sudo().value_ids.filtered(lambda v: not v.partner_id)
                if fallback_line:
                    return fallback_line[0].percentage
                    
                # Step C: No specific discount and no fallback found
                return 0.0

            line.disc_code_1 = get_percentage(product_sudo.disc_code_1)
            line.disc_code_2 = get_percentage(product_sudo.disc_code_2)
            line.surcharge = product_sudo.surcharge or 0.0
            line.retail_price = product_sudo.list_price

    @api.depends('retail_price', 'disc_code_1', 'disc_code_2', 'surcharge', 'product_id')
    def _compute_price_unit(self):
        """ 
        Overrides Odoo's standard price calculation.
        Calculates: ((Retail * (1-D1%)) * (1-D2%)) + Surcharge
        """
        super()._compute_price_unit()
        
        for line in self:
            if not line.product_id:
                continue
                
            d1_pct = line.disc_code_1 or 0.0
            d2_pct = line.disc_code_2 or 0.0

            price_after_d1 = line.retail_price * (1 - (d1_pct / 100.0))
            price_after_d2 = price_after_d1 * (1 - (d2_pct / 100.0))
            final_price = price_after_d2 + line.surcharge

            line.price_unit = final_price
            
            line.discount = 0.0

    @api.model_create_multi
    def create(self, vals_list):
        """ Intercept when an item is added to the cart to block Odoo from forcing the Retail Price """
        for vals in vals_list:
            if 'price_unit' in vals and vals.get('order_id'):
                order = self.env['sale.order'].sudo().browse(vals['order_id'])
                if order.website_id:
                    vals.pop('price_unit')
        return super().create(vals_list)

    def write(self, vals):
        """ Intercept when an item quantity is updated in the cart """
        if 'price_unit' in vals:
            if any(line.order_id.website_id for line in self):
                vals.pop('price_unit')
        return super().write(vals)

    def _get_cart_display_price(self):
        """ Force the cart to use our exact subtotal without standard pricelist re-mapping """
        self.ensure_one()
        return self.price_unit * self.product_uom_qty

    def _should_show_strikethrough_price(self):
        """ Automatically hides the crossed-out retail price in the cart """
        if self.order_id.website_id:
            return False
        return super()._should_show_strikethrough_price()
