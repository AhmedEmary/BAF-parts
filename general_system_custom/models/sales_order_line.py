from odoo import models, fields, api
import logging
from odoo.exceptions import UserError
_logger = logging.getLogger(__name__)


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    stock_quantity = fields.Float(string='Stock Available', compute='_compute_stock_quantity', store=True)
    reserve_qty = fields.Boolean(string='Occupy Stock', default=False, copy=False)
    reserved_qty = fields.Float(string='Reserved Qty', default=0.0, store=True, copy=False)
    percentage_reserved = fields.Float(string='Percentage Completed', compute='_compute_percentage_reserved', store=True, aggregator="avg")
    qty_to_purchase = fields.Float(string='Qty to Purchase', compute='_compute_qty_to_purchase', store=True)
    purchase_vendor_id = fields.Many2one('res.partner', string='PO Vendor', compute='_compute_purchase_vendor_id', store=True, readonly=False)
    brand_id = fields.Many2one('product.brand', related='product_id.brand', store=True, string="Brand", readonly=True)
    purchased_qty = fields.Float(string='Purchased Qty', compute='_compute_purchased_qty', store=True)
    unshipped_qty = fields.Float(string='Unshipped Qty', compute='_compute_unshipped_qty', store=True)

    @api.depends('product_uom_qty', 'qty_invoiced')
    def _compute_unshipped_qty(self):
        for line in self:
            line.unshipped_qty = line.product_uom_qty - line.qty_invoiced

    @api.depends('product_id')
    def _compute_purchase_vendor_id(self):
        for line in self:
            if line.product_id and line.product_id.seller_ids:
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

    def action_create_purchase_order(self):
        lines_to_process = self.filtered(lambda l: l.qty_to_purchase > 0)

        if not lines_to_process:
            raise UserError("Selected lines have no shortage to purchase.")

        lines_without_vendor = lines_to_process.filtered(lambda l: not l.purchase_vendor_id)
        if lines_without_vendor:
            product_names = ", ".join(lines_without_vendor.mapped('product_id.name'))
            raise UserError(f"Please select a Vendor for the following products before creating a PO:\n{product_names}")

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
                # Use BAF purchase price engine
                supplier_code = getattr(vendor, 'baf_supplier_code', 'SUP1') or 'SUP1'
                baf_price = line.product_id.baf_get_purchase_price(supplier_code=supplier_code)
                final_cost = baf_price if baf_price is not None else line.price_unit

                pol_vals_list.append({
                    'order_id': po.id,
                    'product_id': line.product_id.id,
                    'name': line.name,
                    'product_qty': line.qty_to_purchase,
                    'product_uom_id': line.product_uom_id.id,
                    'retail_price': line.price_unit,
                    'price_unit': final_cost,
                    'surcharge': line.product_id.surcharge or 0.0,
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
