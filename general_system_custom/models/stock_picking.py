import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    pallet_ids = fields.Many2many(
        'warehouse.pallet', 
        string='Pallets', 
        compute='_compute_pallet_ids',
        store=True
    )
    
    pallet_count = fields.Integer(
        string='Pallet Count', 
        compute='_compute_pallet_count', 
        store=True
    )

    # Pull billing address from the Sales Order
    billing_address_id = fields.Many2one(
        related='sale_id.partner_invoice_id',
        string='Billing Address',
        readonly=True,
        help="The billing address from the related Sales Order."
    )

    # Pull delivery address from the Sales Order
    delivery_address_id = fields.Many2one(
        related='sale_id.partner_shipping_id',
        string='Delivery Address SO',
        readonly=True,
        help="The delivery address from the related Sales Order."
    )

    is_address_mismatch = fields.Boolean(
        compute='_compute_address_mismatch', 
        store=True
    )

    @api.depends('billing_address_id', 'delivery_address_id')
    def _compute_address_mismatch(self):
        for picking in self:
            # True if both exist and are different
            if picking.billing_address_id and picking.delivery_address_id:
                picking.is_address_mismatch = picking.billing_address_id != picking.delivery_address_id
            else:
                picking.is_address_mismatch = False
                
    @api.depends('move_ids.pallet_info_ids.pallet_id')
    def _compute_pallet_ids(self):
        for picking in self:
            picking.pallet_ids = picking.move_ids.mapped('pallet_info_ids.pallet_id')

    @api.depends('pallet_ids')
    def _compute_pallet_count(self):
        for picking in self:
            picking.pallet_count = len(picking.pallet_ids)

    def action_view_pallets(self):
        self.ensure_one()
        return {
            'name': 'Pallets',
            'type': 'ir.actions.act_window',
            'res_model': 'warehouse.pallet',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.pallet_ids.ids)],
        }

    def _action_done(self):
        """ 
        Triggered when a Picking (Receipt/Delivery) is validated and marked as Done.
        """
        res = super()._action_done()

        for picking in self:
            if picking.purchase_id and picking.purchase_id.sale_order_id:
                so = picking.purchase_id.sale_order_id
                _logger.info(f"RECEIPT VALIDATED: {picking.name} | Updating Reservations for SO: {so.name}")
                
                for move in picking.move_ids:
                    if move.state == 'done' and move.product_id:                        
                        so_lines = so.order_line.filtered(lambda l: l.product_id == move.product_id)
                        qty_from_receipt = move.quantity
                        
                        for line in so_lines:
                            if qty_from_receipt <= 0:
                                break
                            
                            qty_needed = max(0, line.product_uom_qty - line.reserved_qty)
                            if qty_needed > 0 and picking.picking_type_code == 'incoming':
                                to_add = min(qty_from_receipt, qty_needed)
                                line.reserve_qty = True
                                line.invalidate_recordset(['stock_quantity']) 
                                line.reserved_qty += to_add
                                qty_from_receipt -= to_add
        return res

    def action_reset_draft_intelliwise(self):
        """
        Reverses the 'Receive & Reserve' logic and resets the picking to draft.
        """
        for picking in self:
            if picking.purchase_id and picking.purchase_id.sale_order_id:
                so = picking.purchase_id.sale_order_id
                _logger.info(f"RESETTING RECEIPT: {picking.name} | Reverting Reservations for SO: {so.name}")
                
                for move in picking.move_ids:
                    if move.state == 'done' and move.product_id:
                        so_lines = so.order_line.filtered(lambda l: l.product_id == move.product_id)
                        
                        qty_to_remove = move.quantity
                        for line in so_lines:
                            if qty_to_remove <= 0:
                                break
                            
                            if line.reserved_qty > 0:
                                reduction = min(line.reserved_qty, qty_to_remove)
                                line.reserved_qty -= reduction
                                qty_to_remove -= reduction
                                
                                if line.reserved_qty <= 0:
                                    line.reserve_qty = False
                                    
                                line.invalidate_recordset(['stock_quantity'])

            if picking.state == 'done':
                for move in picking.move_ids:
                     if move.state == 'done':
                         self.env['stock.quant']._update_available_quantity(
                             move.product_id, 
                             move.location_dest_id, 
                             -move.quantity
                         )
                         move.write({
                             'state': 'draft',
                             'quantity': 0,
                             'picked': False,
                         })
                
                picking.write({'state': 'draft', 'is_locked': False})

        return True
    
    def action_reset_outgoing_scanned(self):
        """
        Custom reset for outgoing deliveries:
        1. Clears Done quantities (standard Odoo).
        2. Clears scanned totals (custom qty_scanned).
        3. Removes linked Pallet Info records.
        """
        for picking in self:
            if picking.state == 'done':
                 # Odoo 'Done' pickings are typically locked. 
                 # Usually, resetting involves cancelling and creating a new one 
                 # or simply not allowing reset of already shipped goods.
                 continue

            for move in picking.move_ids:
                # Clear custom tracking
                move.qty_scanned = 0.0
                # Clear standard Odoo execution quantity
                move.quantity = 0.0
                # Remove custom pallet distribution lines
                move.pallet_info_ids.unlink()
