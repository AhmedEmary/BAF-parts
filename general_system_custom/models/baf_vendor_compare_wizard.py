from odoo import _, models, fields, api
from odoo.exceptions import UserError


class BafVendorPriceCompare(models.TransientModel):
    _name = 'baf.vendor.price.compare'
    _description = 'BAF Vendor Price Comparison'

    sale_line_id = fields.Many2one('sale.order.line', string='SO Line', required=True, readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    brand_id = fields.Many2one('product.brand', string='Brand', readonly=True)
    qty_to_purchase = fields.Float(string='Qty to Buy', readonly=True)

    selected_vendor_id = fields.Many2one(
        'res.partner',
        string='Vendor for this line',
        domain="[('id', 'in', selectable_vendor_ids)]",
        help="Initially the auto-picked cheapest vendor. Change to override.",
    )
    selectable_vendor_ids = fields.Many2many(
        'res.partner',
        relation='baf_vendor_compare_selectable_rel',
        column1='wizard_id', column2='partner_id',
        string='Selectable Vendors',
    )

    reason = fields.Text(string='Why this vendor?', readonly=True)
    line_ids = fields.One2many('baf.vendor.price.compare.line', 'wizard_id', string='Vendor Prices')

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        line_id = self.env.context.get('default_sale_line_id') or self.env.context.get('active_id')
        if not line_id:
            return vals
        sale_line = self.env['sale.order.line'].browse(line_id)
        if not sale_line.exists() or not sale_line.product_id:
            return vals

        best = sale_line.product_id.baf_get_best_vendor()

        line_vals = []
        for cand in best['candidates']:
            line_vals.append((0, 0, {
                'vendor_id': cand['vendor'].id,
                'method': cand['method'] or False,
                'column_key': cand['column_key'] or '',
                'discount_pct': cand['discount_pct'] or 0.0,
                'sb_surcharge': cand['sb_surcharge'] or 0.0,
                'price': cand['price'] if cand['price'] is not None else 0.0,
                'priceable': cand['price'] is not None,
                'is_winner': cand['is_winner'],
                'note': cand['note'] or '',
            }))

        vals.update({
            'sale_line_id': sale_line.id,
            'product_id': sale_line.product_id.id,
            'brand_id': sale_line.product_id.brand.id if sale_line.product_id.brand else False,
            'qty_to_purchase': sale_line.qty_to_purchase,
            'reason': best['reason'],
            'selected_vendor_id': best['vendor'].id if best['vendor'] else sale_line.purchase_vendor_id.id,
            'line_ids': line_vals,
            'selectable_vendor_ids': [(6, 0, [c['vendor'].id for c in best['candidates']])],
        })
        return vals

    def action_apply(self):
        self.ensure_one()
        if not self.selected_vendor_id:
            raise UserError(_("Pick a vendor before applying."))
        self.sale_line_id.purchase_vendor_id = self.selected_vendor_id
        return {'type': 'ir.actions.act_window_close'}


class BafVendorPriceCompareLine(models.TransientModel):
    _name = 'baf.vendor.price.compare.line'
    _description = 'BAF Vendor Price Comparison Line'
    _order = 'is_winner desc, priceable desc, price asc'

    wizard_id = fields.Many2one('baf.vendor.price.compare', required=True, ondelete='cascade')
    vendor_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    method = fields.Selection(
        selection=[
            ('matrix', 'Matrix Table'),
            ('codes',  'Discount Codes'),
            ('direct', 'Direct Prices'),
        ],
        string='Pricing Method',
        readonly=True,
    )
    column_key = fields.Char(string='Table Column', readonly=True)
    discount_pct = fields.Float(string='Discount %', readonly=True, digits=(6, 4))
    sb_surcharge = fields.Float(string='SB Surcharge %', readonly=True, digits=(6, 4))
    price = fields.Float(string='Net Price', readonly=True, digits='Product Price')
    priceable = fields.Boolean(string='Has Price', readonly=True)
    is_winner = fields.Boolean(string='Selected', readonly=True)
    note = fields.Char(string='Note', readonly=True)