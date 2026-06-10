from odoo import models, fields, api, _
from odoo.exceptions import UserError


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
    baf_applied_column_key = fields.Char(
        string='Applied Column Key',
        compute='_compute_baf_price',
        store=True,
        readonly=True,
        precompute=True,
        help="Discount table column used for this line's price lookup "
             "(e.g. BMW_T12_GR1, MINI_T39_MOTO, JLR_GR1).",
    )
    baf_applied_discount_pct = fields.Float(
        string='Applied Discount %',
        digits=(6, 4),
        compute='_compute_baf_price',
        store=True,
        readonly=True,
        precompute=True,
        help="Discount percentage applied to UPE for this line.",
    )

    @api.depends('product_id', 'order_id.partner_id', 'product_uom_qty')
    def _compute_baf_price(self):
        for line in self:
            if not line.product_id:
                line.retail_price = 0.0
                line.surcharge = 0.0
                line.baf_applied_column_key = ''
                line.baf_applied_discount_pct = 0.0
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

            details = product.baf_get_sales_price_details(partner=partner.sudo()._origin)
            line.price_unit = details['price']
            line.baf_applied_column_key = details['column_key']
            line.baf_applied_discount_pct = details['discount_pct']
            line.discount = 0.0

    def _compute_price_unit(self):
        standard = self.filtered(
            lambda l: not l.product_id or l.state in ['cancel'] or l.qty_invoiced > 0
        )
        if standard:
            super(SaleOrderLine, standard)._compute_price_unit()

    def _baf_assert_product_orderable(self, product):
        """Raise UserError when the product is NLA or has a replacement."""
        if not product:
            return
        template = product.product_tmpl_id if product._name == 'product.product' else product
        if not template._baf_is_order_blocked():
            return
        if template.replaced_by_id:
            raise UserError(_(
                "“%(name)s” has been replaced and can no longer be ordered. "
                "Please order the replacement “%(replacement)s” instead.",
                name=template.display_name,
                replacement=template.replaced_by_id.display_name,
            ))
        raise UserError(_(
            "“%(name)s” is no longer available and cannot be ordered.",
            name=template.display_name,
        ))

    @api.model_create_multi
    def create(self, vals_list):
        Product = self.env['product.product'].sudo()
        for vals in vals_list:
            product_id = vals.get('product_id')
            if product_id:
                self._baf_assert_product_orderable(Product.browse(product_id))
            if 'price_unit' in vals and vals.get('order_id'):
                order = self.env['sale.order'].sudo().browse(vals['order_id'])
                if order.website_id:
                    vals.pop('price_unit')
        return super().create(vals_list)

    def write(self, vals):
        if 'product_id' in vals and vals['product_id']:
            product = self.env['product.product'].sudo().browse(vals['product_id'])
            self._baf_assert_product_orderable(product)
        # Bumping the qty of a now-blocked line (product flagged NLA / replaced
        # after the line was added) must also be refused.
        if 'product_uom_qty' in vals:
            try:
                new_qty = float(vals['product_uom_qty'])
            except (TypeError, ValueError):
                new_qty = 0.0
            if new_qty > 0:
                for line in self:
                    if new_qty > line.product_uom_qty:
                        self._baf_assert_product_orderable(line.product_id)
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
