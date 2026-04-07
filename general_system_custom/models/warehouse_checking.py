import logging
from collections import defaultdict
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class WarehouseCheckingSession(models.Model):
    _name = 'warehouse.checking.session'
    _description = 'Cross-Docking Session'

    name = fields.Char(string='Session', default='New Session', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Supplier', required=True)
    scan_sku = fields.Char(string='Scan SKU / QR Code') 
    
    line_ids = fields.One2many('warehouse.checking.line', 'session_id', string='Lines')
    state = fields.Selection(
        selection=[('draft', 'In Progress'), ('done', 'Validated')], 
        default='draft', 
        string='Status'
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('warehouse.checking.session') or _('New')
        return super().create(vals_list)

    def action_open_dropship_import(self):
        return {
            'name': 'Import Dropship List',
            'type': 'ir.actions.act_window',
            'res_model': 'import.dropship.pallets',
            'view_mode': 'form',
            'target': 'new',
        }

    def action_validate(self):
        self._check_ready_to_validate()
        
        # 1. Process Incoming POs (Receipts)
        # This updates the 'Done' quantity on the Receipt based on your scans
        grouped_lines = self._group_lines_by_po()
        for po, lines in grouped_lines.items():
            self._process_po_group(po, lines)
        
        # 2. Update Outgoing Delivery Moves (Pallet Info)
        self._assign_pallets_to_outgoing()
        
        self.state = 'done'
        return {'type': 'ir.actions.act_window_close'}

    def _process_po_group(self, po, lines):
        picking = self._get_existing_picking(po)
        lines.write({'picking_id': picking.id})
        self._update_picking_moves(picking, lines)
        self._validate_picking(picking)

    def _update_picking_moves(self, picking, lines):
        """ 
        Updates moves based on session lines. 
        Unscanned products are explicitly set to 0.0 to force backorder creation.
        """
        # 1. Aggregate scanned quantities by (Product, PO Line)
        qty_map = defaultdict(float)
        for l in lines:
            key = (l.product_id.id, l.purchase_line_id.id)
            qty_map[key] += l.fulfill_qty

        if picking.state == 'draft':
            picking.action_confirm()

        # 2. Apply quantities to moves
        for move in picking.move_ids:
            key = (move.product_id.id, move.purchase_line_id.id)
            
            if key in qty_map:
                move.quantity = qty_map[key]
            else:
                # CRITICAL: Set unscanned items to 0.
                # This forces Odoo to recognize them as "missing" and offer a backorder.
                move.quantity = 0.0

    def _validate_picking(self, picking):
        """ Automatically confirms the backorder for remaining parts. """
        try:
            res = picking.button_validate()
            
            # If Odoo asks about backorders (Partial Receipt)
            if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
                wizard = self.env['stock.backorder.confirmation'].with_context(res['context']).create({
                    'pick_ids': [fields.Command.set(picking.ids)]
                })
                wizard.process() # This creates the new picking for the remaining parts
                
        except Exception as e:
            raise UserError(_("Failed to validate receipt %s: %s") % (picking.name, str(e)))

    def _assign_pallets_to_outgoing(self):
        """ Updates Delivery Moves based on the work done in this session. """
        for line in self.line_ids:
            if line.delivery_picking_id:
                self._update_outgoing_move_for_line(line)

    def _update_outgoing_move_for_line(self, line):
        """ Updates 'stock.move.pallet.info' for the target move. """
        moves = line.delivery_picking_id.move_ids.filtered(
            lambda m: m.product_id == line.product_id and m.state not in ['done', 'cancel']
        )
        
        qty_remaining = line.fulfill_qty
        
        for move in moves:
            if qty_remaining <= 0: break
            
            # Capacity: Demand - Scanned
            capacity = max(0, move.product_uom_qty - move.qty_scanned)
            if capacity <= 0: continue
            
            qty_to_assign = min(qty_remaining, capacity)
            
            # A. Update Header Progress
            move.qty_scanned += qty_to_assign
            
            # B. Update/Create Pallet Info Line
            if qty_to_assign > 0 and line.pallet_id:
                pallet_info = self.env['stock.move.pallet.info'].search([
                    ('move_id', '=', move.id),
                    ('pallet_id', '=', line.pallet_id.id)
                ], limit=1)
                
                if pallet_info:
                    pallet_info.quantity += qty_to_assign
                else:
                    self.env['stock.move.pallet.info'].create({
                        'move_id': move.id,
                        'pallet_id': line.pallet_id.id,
                        'quantity': qty_to_assign
                    })
            
            qty_remaining -= qty_to_assign

    def action_reset_draft(self):
        self.ensure_one()
        
        # 1. Reset Outgoing Deliveries
        pickings = self.line_ids.mapped('delivery_picking_id')
        for picking in pickings:
            if picking.state not in ['done', 'cancel']:
                picking.action_reset_outgoing_scanned()
        
        self._revert_outgoing_moves()
        self._reset_incoming_pickings()
        
        self.line_ids.write({'picking_id': False})
        self.state = 'draft'

    def _revert_outgoing_moves(self):
        """ Reverses qty_scanned and updates pallet info. """
        for line in self.line_ids:
            if not line.delivery_picking_id: 
                continue

            moves = line.delivery_picking_id.move_ids.filtered(
                lambda m: m.product_id == line.product_id and m.state not in ['done', 'cancel']
            )
            
            qty_to_revert = line.fulfill_qty
            for move in moves:
                if qty_to_revert <= 0: 
                    break
                
                # FIX: Initialize amount to ensure it exists even if move.qty_scanned is 0
                amount = 0.0
                if move.qty_scanned > 0:
                    amount = min(qty_to_revert, move.qty_scanned)
                    move.qty_scanned -= amount
                    qty_to_revert -= amount
                
                # Only attempt to reduce pallet info if we actually reverted a quantity
                if line.pallet_id and amount > 0:
                    pallet_infos = self.env['stock.move.pallet.info'].search([
                        ('move_id', '=', move.id),
                        ('pallet_id', '=', line.pallet_id.id)
                    ])
                   
                    qty_reduce_pallet = amount 
                    for info in pallet_infos:
                        if qty_reduce_pallet <= 0: 
                            break
                        if info.quantity > qty_reduce_pallet:
                            info.quantity -= qty_reduce_pallet
                            qty_reduce_pallet = 0
                        else:
                            qty_reduce_pallet -= info.quantity
                            info.unlink()
                            
    def _reset_incoming_pickings(self):
        """ 
        Reverts validated receipts. 
        If a product was fully fulfilled, it reverts that 'Done' picking to Draft.
        If a backorder exists, it merges quantities and cancels the partial picking.
        """
        # Get all pickings processed in this session
        pickings = self.line_ids.mapped('picking_id')
        
        for picking in pickings:
            if picking.state == 'done':
                picking.action_reset_draft_intelliwise()
                
                po = picking.purchase_id
                main_open_picking = po.picking_ids.filtered(
                    lambda p: p.state not in ['done', 'cancel'] and 
                              p.picking_type_code == 'incoming' and 
                              p.id != picking.id
                )
                
                if main_open_picking:
                    # Case A: We have a backorder. Move quantities from THIS picking back to it.
                    main_open_picking = main_open_picking[0]
                    _logger.info(f"Merging {picking.name} back into {main_open_picking.name}")
                    
                    for reset_move in picking.move_ids:
                        target_move = main_open_picking.move_ids.filtered(
                            lambda m: m.product_id == reset_move.product_id and 
                                      m.purchase_line_id == reset_move.purchase_line_id
                        )
                        if target_move:
                            target_move.product_uom_qty += reset_move.product_uom_qty
                        else:
                            # If for some reason the product isn't in the backorder, move the move itself
                            reset_move.picking_id = main_open_picking.id
                    
                    # Cancel this picking because its contents are now back in the main one
                    picking.action_cancel()
                else:
                    # Case B: This was the only picking (Full Fulfillment).
                    # It is now back in 'Draft' state thanks to action_reset_draft_intelliwise.
                    _logger.info(f"Picking {picking.name} reset to Draft for re-use.")
          
    def _check_ready_to_validate(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("You cannot validate an empty session."))
        for line in self.line_ids:
            if not line.pallet_id and line.fulfill_qty > 0:
                raise UserError(self.env._("Please select a Pallet for the product %s before validating.") % line.product_id.display_name)

    def _group_lines_by_po(self):
        lines_by_po = {}
        for line in self.line_ids:
            if line.purchase_order_id not in lines_by_po:
                lines_by_po[line.purchase_order_id] = self.env['warehouse.checking.line']
            lines_by_po[line.purchase_order_id] |= line
        return lines_by_po

    def _get_existing_picking(self, po):
        pickings = po.picking_ids.filtered(
            lambda p: p.state not in ['done', 'cancel'] and p.picking_type_code in ['incoming', 'dropship']
        )
        if not pickings:
             raise UserError(_("No open receipt found for PO %s. It might be already processed.") % po.name)
        return pickings[0]

    
    def action_search_sku(self, barcode=None):
        self.ensure_one()
        search_term = barcode or self.scan_sku

        if not search_term: 
            return
            
        product = self.env['product.product'].search([
            '|', 
            ('default_code', '=', search_term), 
            ('barcode', '=', search_term)
        ], limit=1)
        
        if not product:
            raise UserError(_("Product not found for Code/QR: %s") % self.scan_sku)
        
        existing_lines = self.line_ids.filtered(lambda l: l.product_id == product)
        
        existing_keys = set()
        for line in existing_lines:
            existing_keys.add((line.purchase_line_id.id, line.sale_line_id.id))

        po_lines = self.env['purchase.order.line'].search([
            ('partner_id', '=', self.partner_id.id),
            ('product_id', '=', product.id),
            ('order_id.state', '=', 'purchase'),
        ])

        if not po_lines and not existing_lines:
             raise UserError(_("No open Purchase Orders found for this Product."))

        wizard_lines_vals = []
        for pol in po_lines:
            so = pol.order_id.sale_order_id
            if so:
                for sol in so.order_line.filtered(lambda l: l.product_id == product):
                    
                    if (pol.id, sol.id) in existing_keys:
                        continue

                    open_qty = pol.qty_split - pol.qty_received
                    if open_qty > 0:
                        is_dropship = pol.order_id.picking_type_id.code == 'dropship'
                        pickings = so.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state not in ['cancel', 'done'])
                        
                        delivery_picking_id = pickings[0].id if pickings else False
                        
                        dest_note = ""
                        if is_dropship:
                            dest_note = so.partner_shipping_id.display_name or so.partner_id.display_name

                        wizard_lines_vals.append((0, 0, {
                            'product_id': product.id,
                            'purchase_order_id': pol.order_id.id,
                            'purchase_line_id': pol.id,
                            'sale_order_id': so.id,
                            'sale_line_id': sol.id,
                            'delivery_picking_id': delivery_picking_id,
                            'open_qty': open_qty,
                            'fulfill_qty': open_qty, 
                            'is_dropship': is_dropship,
                            'destination_note': dest_note,
                        }))

        if wizard_lines_vals:
            wizard = self.env['warehouse.checking.wizard'].create({
                'session_id': self.id,
                'product_id': product.id,
                'line_ids': wizard_lines_vals
            })
            action_result = {
                'name': 'Found New Lines - Select to Add',
                'type': 'ir.actions.act_window',
                'res_model': 'warehouse.checking.wizard',
                'view_mode': 'form',
                'res_id': wizard.id,
                'target': 'new',
                'views': [(False, 'form')],
            }
            return action_result
        
        elif existing_lines:
            view_ref = self.env.ref('general_system_custom.view_warehouse_checking_line_tree', raise_if_not_found=False)
            view_id = view_ref.id if view_ref else False
            return {
                'name': 'Edit Existing Lines',
                'type': 'ir.actions.act_window',
                'res_model': 'warehouse.checking.line',
                'view_mode': 'list',
                'domain': [('id', 'in', existing_lines.ids)],
                'target': 'new',
                'views': [(view_id, 'list')],
            }
        else:
             raise UserError(_("All available lines for this product are already in the session or completed."))


class WarehouseCheckingLine(models.Model):
    _name = 'warehouse.checking.line'
    _description = 'Cross-Docking Line'

    session_id = fields.Many2one('warehouse.checking.session', string='Session')
    picking_id = fields.Many2one('stock.picking', string="Receipt", readonly=True)
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    
    delivery_picking_id = fields.Many2one('stock.picking', string='Delivery N.', readonly=True)
    
    customer_id = fields.Many2one(related='sale_order_id.partner_id', string='Customer', store=True, readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='SO #', readonly=True)
    sale_line_id = fields.Many2one('sale.order.line', string='SO Line')
    customer_po = fields.Char("Customer PO Number", related='sale_order_id.customer_po')
    purchase_order_id = fields.Many2one('purchase.order', string='PO #', readonly=True)
    purchase_line_id = fields.Many2one('purchase.order.line', string='PO Line', readonly=True)
    open_qty = fields.Float(string='Open Qty', readonly=True)
    fulfill_qty = fields.Float(string='Fulfill Qty')
    pallet_id = fields.Many2one('warehouse.pallet', string='Pallet #')
    destination_note = fields.Char(string='Note', readonly=True)
    
    partner_invoice_id = fields.Many2one(
        related='sale_order_id.partner_invoice_id', 
        string='Billing Address', 
        readonly=True
    )
    partner_shipping_id = fields.Many2one(
        related='sale_order_id.partner_shipping_id', 
        string='Delivery Address', 
        readonly=True
    )
    is_address_mismatch = fields.Boolean(
        compute='_compute_address_mismatch', 
        string='Address Mismatch'
    )
    shipping_method = fields.Selection(
        related='sale_order_id.shipping_method', 
        string='Ship Method', 
        readonly=True
    )
    is_dropship = fields.Boolean(string='Is Dropship')


    @api.depends('partner_invoice_id', 'partner_shipping_id')
    def _compute_address_mismatch(self):
        for line in self:
            line.is_address_mismatch = line.partner_invoice_id != line.partner_shipping_id

    @api.constrains('fulfill_qty', 'open_qty')
    def _check_qty(self):
        for line in self:
            if line.fulfill_qty > line.open_qty:
                raise UserError(_("Fulfill Qty cannot be greater than Open Qty."))

    @api.constrains('fulfill_qty', 'pallet_id')
    def _check_pallet_editable(self):
        for line in self:
            if line.pallet_id and line.pallet_id.state not in ['open', 'dropship']:
                raise UserError(_("You cannot modify items in Pallet %s because it is %s. Please re-open the pallet first.") % (line.pallet_id.name, line.pallet_id.state))

    def unlink(self):
        for line in self:
            if line.pallet_id and line.pallet_id.state not in ['open', 'dropship']:
                raise UserError(_("You cannot delete items from Pallet %s because it is %s.") % (line.pallet_id.name, line.pallet_id.state))
        return super().unlink()

    def action_confirm_line(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window_close'}


class WarehouseCheckingWizard(models.TransientModel):
    _name = 'warehouse.checking.wizard'
    _description = 'Scan Result Wizard'

    session_id = fields.Many2one('warehouse.checking.session', required=True)
    product_id = fields.Many2one('product.product', string="Scanned Product", readonly=True)
    line_ids = fields.One2many('warehouse.checking.wizard.line', 'wizard_id', string='Found Lines')
    has_dropship_lines = fields.Boolean(
        compute='_compute_banner_flags'
    )
    has_mismatch_lines = fields.Boolean(
        compute='_compute_banner_flags'
    )

    @api.depends('line_ids.is_dropship', 'line_ids.is_address_mismatch')
    def _compute_banner_flags(self):
        for wizard in self:
            # Returns True if ANY line has is_dropship == True
            wizard.has_dropship_lines = any(wizard.line_ids.mapped('is_dropship'))
            # Returns True if ANY line has is_address_mismatch == True
            wizard.has_mismatch_lines = any(wizard.line_ids.mapped('is_address_mismatch'))
            
    def action_add_selected(self):
        self.ensure_one()
        real_lines_vals = []
        for line in self.line_ids.filtered(lambda l: l.is_selected):
            real_lines_vals.append({
                'session_id': self.session_id.id,
                'product_id': self.product_id.id,
                'purchase_order_id': line.purchase_order_id.id,
                'purchase_line_id': line.purchase_line_id.id,
                'sale_order_id': line.sale_order_id.id,
                'sale_line_id': line.sale_line_id.id,
                'delivery_picking_id': line.delivery_picking_id.id if line.delivery_picking_id else False,
                'open_qty': line.open_qty,
                'fulfill_qty': line.fulfill_qty,
                'pallet_id': line.pallet_id.id,
                'is_dropship': line.is_dropship,
                'destination_note': line.destination_note,
            })
        
        if real_lines_vals:
            self.env['warehouse.checking.line'].create(real_lines_vals)
        
        return {'type': 'ir.actions.act_window_close'}

class WarehouseCheckingWizardLine(models.TransientModel):
    _name = 'warehouse.checking.wizard.line'
    _description = 'Scan Result Line (Transient)'

    is_selected = fields.Boolean(string="Add", default=False)
    wizard_id = fields.Many2one('warehouse.checking.wizard') 
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    delivery_picking_id = fields.Many2one('stock.picking', string='Delivery N.', readonly=True)  
    customer_id = fields.Many2one(related='sale_order_id.partner_id', string='Customer', readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='SO #', readonly=True)
    sale_line_id = fields.Many2one('sale.order.line', string='SO Line')
    partner_shipping_id = fields.Many2one(
        related='sale_order_id.partner_shipping_id', 
        string='Delivery Address', 
        readonly=True
    )
    partner_invoice_id = fields.Many2one(
        related='sale_order_id.partner_invoice_id', 
        string='Billing Address', 
        readonly=True
    )
    is_address_mismatch = fields.Boolean(
        compute='_compute_address_mismatch', 
        string='Address Mismatch'
    )
    shipping_method = fields.Selection(
        related='sale_order_id.shipping_method', 
        string='Ship Method', 
        readonly=True
    )

    purchase_order_id = fields.Many2one('purchase.order', string='PO #', readonly=True)
    purchase_line_id = fields.Many2one('purchase.order.line', string='PO Line', readonly=True)
    open_qty = fields.Float(string='Open Qty', readonly=True)
    fulfill_qty = fields.Float(string='Fulfill Qty')
    pallet_id = fields.Many2one('warehouse.pallet', string='Pallet #')
    destination_note = fields.Char(string='Note', readonly=True)
    is_dropship = fields.Boolean(string='Is Dropship')

    @api.depends('partner_invoice_id', 'partner_shipping_id')
    def _compute_address_mismatch(self):
        for line in self:
            line.is_address_mismatch = line.partner_invoice_id != line.partner_shipping_id
