from odoo import models, fields, api
import logging
from odoo.exceptions import UserError
_logger = logging.getLogger(__name__)

# Costing gaps: the line has no usable cost and each state needs a different
# fix. Kept apart from 'legacy' (pre-existing lines, never costed) and 'none'
# (no product), which are hidden in the views but keep their stored margin.
BAF_COST_GAP_STATES = ('no_vendor', 'no_price', 'no_delivery_window')


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    sku_lookup = fields.Char(
        string='SKU',
        compute='_compute_sku_brand_lookup',
        store=True,
        readonly=False,
        help="Type the exact SKU to find and set the product",
    )
    brand_lookup = fields.Many2one(
        'product.brand',
        string='Search Brand',
        compute='_compute_sku_brand_lookup',
        store=True,
        readonly=False,
        help="Select brand first to filter SKU search (required when same SKU exists in multiple brands)",
    )

    stock_quantity = fields.Float(string='Stock Available', compute='_compute_stock_quantity', store=True)
    reserve_qty = fields.Boolean(string='Occupy Stock', default=False, copy=False)
    reserved_qty = fields.Float(string='Reserved Qty', default=0.0, store=True, copy=False)
    percentage_reserved = fields.Float(string='Percentage Completed', compute='_compute_percentage_reserved', store=True, aggregator="avg")
    qty_to_purchase = fields.Float(string='Qty to Purchase', compute='_compute_qty_to_purchase', store=True)
    purchase_vendor_id = fields.Many2one(
        'res.partner', string='Selected Vendor',
        compute='_compute_purchase_vendor_id', store=True, readonly=False,
    )
    baf_alt_vendor_id = fields.Many2one(
        'res.partner', string='Chosen Alternative Vendor', copy=False,
        help="Set when the customer picks an alternative direct vendor on the "
             "portal. Drives the Selected Vendor and the line price "
             "(direct + markup). Empty = default (best-price vendor).",
    )
    brand_id = fields.Many2one('product.brand', related='product_id.brand', store=True, string="Brand", readonly=True)
    purchased_qty = fields.Float(string='Purchased Qty', compute='_compute_purchased_qty', store=True)
    unshipped_qty = fields.Float(string='Unshipped Qty', compute='_compute_unshipped_qty', store=True)

    baf_cost_status = fields.Selection(
        selection=[
            ('ok', 'Costed'),
            ('no_vendor', 'No Eligible Vendor'),
            ('no_price', 'Vendor Cannot Price'),
            ('no_delivery_window', 'None In Delivery Window'),
            ('legacy', 'Not Costed'),
            ('none', 'N/A'),
        ],
        string='Cost Status', compute='_compute_purchase_price',
        store=True, readonly=True, precompute=False,
        groups='base.group_user',
        help="How this line's Cost was resolved. Each gap state needs a "
             "different fix: onboard a vendor for the brand, extend the "
             "vendor's discount table, or widen the customer's delivery window.",
    )
    # Read-only on purpose: the cost actually paid is the vendor's price, so a
    # hand-typed figure could only ever disagree with the purchase order.
    #
    # precompute=False everywhere in this chain, overriding sale_margin: the
    # engine needs purchase_vendor_id, which is a live (non-stored) compute,
    # so Odoo cannot precompute Cost anyway and warns on every registry load.
    # margin / margin_percent follow, since they now depend on baf_cost_status.
    purchase_price = fields.Float(
        compute='_compute_purchase_price', store=True, readonly=True,
        precompute=False,
    )
    margin = fields.Float(precompute=False)
    margin_percent = fields.Float(precompute=False)

    def _baf_skip_repricing(self):
        """True when this line's price is owned by an external system.

        Lines imported from a marketplace already carry the price the buyer
        was charged there, so re-deriving one from the BAF catalog would both
        overwrite that agreed price and break the order total. Modules that
        import such orders override this; the default is to reprice normally.
        """
        self.ensure_one()
        return False

    @api.onchange('sku_lookup', 'brand_lookup')
    def _onchange_sku_lookup(self):
        """When user types an exact SKU, find and set the product (filtered by brand if set)."""
        if not self.sku_lookup:
            return
        sku = self.sku_lookup.strip()
        domain = [('sku', '=', sku)]
        if self.brand_lookup:
            domain.append(('brand', '=', self.brand_lookup.id))
        products = self.env['product.product'].search(domain)
        if len(products) == 1:
            self.product_id = products
            self.product_template_id = products.product_tmpl_id
            self.brand_lookup = products.brand
        elif len(products) > 1:
            if not self.brand_lookup:
                brand_ids = products.mapped('brand').ids
                return {
                    'domain': {'brand_lookup': [('id', 'in', brand_ids)]},
                    'warning': {
                        'title': 'Multiple Brands Found',
                        'message': f'SKU "{sku}" exists in multiple brands. '
                                   f'Please select a Brand to choose the correct product.',
                    },
                }
            else:
                self.product_id = products[0]
                self.product_template_id = products[0].product_tmpl_id
        else:
            return {
                'warning': {
                    'title': 'SKU Not Found',
                    'message': f'No product found with exact SKU "{sku}".',
                },
            }

    @api.depends('product_id')
    def _compute_sku_brand_lookup(self):
        """Auto-fill SKU and Brand from the selected product."""
        for line in self:
            if line.product_id:
                line.sku_lookup = line.product_id.sku or ''
                line.brand_lookup = line.product_id.brand
            else:
                line.sku_lookup = ''
                line.brand_lookup = False

    @api.depends('product_uom_qty', 'qty_invoiced')
    def _compute_unshipped_qty(self):
        for line in self:
            line.unshipped_qty = line.product_uom_qty - line.qty_invoiced

    def _baf_costing_price(self, vendor):
        """Engine purchase price for `vendor`, or None when it cannot price
        this part. sudo() on both sides: a portal cart-add reaches res.partner
        and baf.discount.line, which a portal user cannot read."""
        self.ensure_one()
        details = self.product_id.sudo().baf_get_purchase_price_details(
            vendor.sudo())
        return details['price'] if details else None

    @api.model
    def _baf_gap_status(self, candidates):
        """Tell the two costing gaps apart from what baf_get_best_vendor
        already reports per candidate. The 'no_delivery_window' state is
        kept in the field selection for historical data but is no longer
        produced (the customer's delivery cap no longer filters candidates)."""
        if not candidates:
            return 'no_vendor'
        return 'no_price'

    def _baf_resolve_cost(self):
        """(price, status) from the pricing engine for this line's costing
        vendor: the Selected Vendor when set, otherwise the best vendor."""
        self.ensure_one()
        if not self.product_id:
            return 0.0, 'none'
        if self.purchase_vendor_id:
            price = self._baf_costing_price(self.purchase_vendor_id)
            if price is None:
                return 0.0, 'no_price'
            return price, 'ok'
        best = self.product_id.sudo().baf_get_best_vendor(
            customer=self.order_id.partner_id.sudo())
        if best['vendor']:
            return best['price'], 'ok'
        return 0.0, self._baf_gap_status(best['candidates'])

    @api.depends('product_id', 'purchase_vendor_id', 'order_id.partner_id')
    def _compute_purchase_price(self):
        """Cost is the price we expect to pay the costing vendor. Replaces
        sale_margin's standard_price lookup, which BAF imports never fill.

        product_uom_qty is deliberately absent: the engine prices per unit, so
        quantity cannot change Cost. Margin still follows quantity through
        sale_margin's own compute.
        """
        for line in self:
            line.purchase_price, line.baf_cost_status = line._baf_resolve_cost()

    @api.depends('price_subtotal', 'product_uom_qty', 'purchase_price',
                 'baf_cost_status')
    def _compute_margin(self):
        """A line with no resolvable cost reports no margin rather than the
        full subtotal. 'legacy' is deliberately not in the blanked set: those
        lines predate costing and keep the value they already have, so editing
        an old order never rewrites its margin. The views hide it instead.
        """
        gaps = self.filtered(
            lambda l: l.baf_cost_status in BAF_COST_GAP_STATES)
        super(SaleOrderLine, self - gaps)._compute_margin()
        for line in gaps:
            line.margin = 0.0
            line.margin_percent = 0.0

    @api.depends('product_id', 'baf_alt_vendor_id')
    def _compute_purchase_vendor_id(self):
        for line in self:
            # A customer-chosen alternative wins over the auto best-vendor.
            if line.baf_alt_vendor_id:
                line.purchase_vendor_id = line.baf_alt_vendor_id
                continue
            if not line.product_id:
                line.purchase_vendor_id = False
                continue
            best = line.product_id.baf_get_best_vendor(
                customer=line.order_id.partner_id)
            if best['vendor']:
                line.purchase_vendor_id = best['vendor']
            elif line.product_id.seller_ids:
                line.purchase_vendor_id = line.product_id.seller_ids[0].partner_id
            else:
                line.purchase_vendor_id = False

    @api.depends('order_id.purchase_ids.order_line.product_qty', 'order_id.purchase_ids.order_line.qty_received', 'order_id.purchase_ids.state', 'order_id.purchase_ids.receipt_status')
    def _compute_purchased_qty(self):
        for line in self:
            po_lines = self.env['purchase.order.line'].search([
                ('order_id.sale_order_id', '=', line.order_id.id),
                ('product_id', '=', line.product_id.id),
                ('order_id.state', 'in', ['draft', 'purchase', 'done']),
                ('order_id.receipt_status', '!=', 'full')
            ])
            line.purchased_qty = sum(max(0, pol.product_qty - pol.qty_received) for pol in po_lines)

    @api.depends('product_uom_qty', 'reserved_qty', 'reserve_qty', 'purchased_qty', 'order_id', 'state')
    def _compute_qty_to_purchase(self):
        for line in self:
            reserved = line.reserved_qty if line.reserve_qty else 0.0
            line.qty_to_purchase = max(0, line.product_uom_qty - reserved - line.purchased_qty)

    @api.depends('product_id')
    def _compute_stock_quantity(self):
        for line in self:
            # Assuming you want to get the available stock of the product
            if line.product_id:
                line.stock_quantity = line.product_id.qty_available
            else:
                line.stock_quantity = 0.0

    @api.depends('product_uom_qty', 'reserved_qty')
    def _compute_percentage_reserved(self):
        for line in self:
            if line.product_uom_qty > 0:
                line.percentage_reserved = (line.reserved_qty / line.product_uom_qty) * 100.0
            else:
                line.percentage_reserved = 0.0

    def action_open_vendor_compare(self):
        self.ensure_one()
        return {
            'name': 'Compare Vendor Prices',
            'type': 'ir.actions.act_window',
            'res_model': 'baf.vendor.price.compare',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_sale_line_id': self.id},
        }

    def action_create_purchase_order(self):
        lines_to_process = self.filtered(lambda l: l.qty_to_purchase > 0)

        if not lines_to_process:
            raise UserError("Selected lines have no shortage to purchase.")

        lines_without_vendor = lines_to_process.filtered(lambda l: not l.purchase_vendor_id)
        if lines_without_vendor:
            product_names = ", ".join(lines_without_vendor.mapped('product_id.name'))
            raise UserError(f"Please select a Vendor for the following products before creating a PO:\n{product_names}")

        unpriceable = [
            "%s - %s" % (line.product_id.display_name,
                         line.purchase_vendor_id.display_name)
            for line in lines_to_process
            if line.product_id.sudo().baf_get_purchase_price_details(
                line.purchase_vendor_id) is None
        ]
        if unpriceable:
            raise UserError(
                "These vendors have no price for the part. Extend the "
                "vendor's discount table before ordering:\n%s"
                % "\n".join(unpriceable))

        grouped_lines = {}
        for line in lines_to_process:
            vendor = line.purchase_vendor_id
            if vendor not in grouped_lines:
                grouped_lines[vendor] = []
            grouped_lines[vendor].append(line)

        ctx = {
            'tracking_disable': True,
            'mail_notrack': True,
            'mail_create_nolog': True,
            'mail_create_nosubscribe': True,
        }

        created_pos = self.env['purchase.order']
        PurchaseOrder = self.env['purchase.order'].with_context(ctx)
        PurchaseOrderLine = self.env['purchase.order.line'].with_context(ctx)

        for vendor, so_lines in grouped_lines.items():
            po = PurchaseOrder.create({
                'partner_id': vendor.id,
                'origin': so_lines[0].order_id.name,
                'company_id': so_lines[0].company_id.id,
                'date_order': fields.Datetime.now(),
                'sale_order_id': so_lines[0].order_id.id,
            })
            created_pos += po

            pol_vals_list = []
            for line in so_lines:
                # Guarded above: every line here is priceable by its vendor.
                details = line.product_id.baf_get_purchase_price_details(vendor)

                pol_vals_list.append({
                    'order_id': po.id,
                    'product_id': line.product_id.id,
                    'name': line.name,
                    'product_qty': line.qty_to_purchase,
                    'product_uom_id': line.product_uom_id.id,
                    'retail_price': line.product_id.list_price,
                    'price_unit': details['price'],
                    'surcharge': line.product_id.surcharge or 0.0,
                    'baf_discount_code': line.product_id.baf_discount_code or False,
                    'baf_discount_pct': details['discount_pct'],
                    'baf_column_key': details['column_key'],
                    'date_planned': fields.Datetime.now(),
                })

            if pol_vals_list:
                PurchaseOrderLine.create(pol_vals_list)

        if not created_pos:
            raise UserError("No Purchase Orders were created.")

        return {
            'name': 'Purchase Orders',
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_pos.ids)],
        }
