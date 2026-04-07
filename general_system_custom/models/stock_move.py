from odoo import models, fields, api
import time
import logging

_logger = logging.getLogger(__name__)


class StockMovePalletInfo(models.Model):
    _name = 'stock.move.pallet.info'
    _description = 'Stock Move Pallet Info'

    move_id = fields.Many2one('stock.move', string="Stock Move", required=True, ondelete='cascade')
    pallet_id = fields.Many2one('warehouse.pallet', string='Pallet')
    quantity = fields.Float(string='Quantity')
    product_id = fields.Many2one(related='move_id.product_id', string='Product', readonly=True)
    customer_id = fields.Many2one(
        related='move_id.picking_id.partner_id',
        string='Customer',
        store=True,
        readonly=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.pallet_id and rec.quantity > 0:
                rec._sync_warehouse_checking_line()
        return records

    def write(self, vals):
        res = super().write(vals)
        for rec in self:
            if 'pallet_id' in vals or 'quantity' in vals:
                rec._sync_warehouse_checking_line()
        return res

    def unlink(self):
        for rec in self:
            if rec.pallet_id:
                domain = [
                    ('pallet_id', '=', rec.pallet_id.id),
                    ('product_id', '=', rec.product_id.id),
                    ('delivery_picking_id', '=', rec.move_id.picking_id.id)
                ]
                existing_lines = self.env['warehouse.checking.line'].search(domain)
                lines_to_unlink = existing_lines.filtered(lambda l: not l.session_id)
                lines_to_unlink.unlink()

        return super().unlink()

    def _sync_warehouse_checking_line(self):
        """ Creates or updates the related Warehouse Checking Line for this pallet """
        self.ensure_one()
        if not self.pallet_id:
            return

        CheckingLine = self.env['warehouse.checking.line']
        move = self.move_id

        domain = [
            ('pallet_id', '=', self.pallet_id.id),
            ('product_id', '=', self.product_id.id),
            ('delivery_picking_id', '=', move.picking_id.id),
        ]

        # If there's a specific Sale Order line, link to it precisely
        if move.sale_line_id:
            domain.append(('sale_line_id', '=', move.sale_line_id.id))

        existing_line = CheckingLine.search(domain, limit=1)

        if existing_line:
            if self.quantity <= 0:
                existing_line.unlink()
            else:
                existing_line.write({
                    'fulfill_qty': self.quantity,
                    'open_qty': self.quantity,
                })
        elif self.quantity > 0:
            CheckingLine.create({
                'pallet_id': self.pallet_id.id,
                'product_id': self.product_id.id,
                'delivery_picking_id': move.picking_id.id,
                'sale_order_id': move.sale_line_id.order_id.id if move.sale_line_id else False,
                'sale_line_id': move.sale_line_id.id if move.sale_line_id else False,
                'fulfill_qty': self.quantity,
                'open_qty': self.quantity,
            })


class StockMove(models.Model):
    _inherit = 'stock.move'

    reserved_qty_custom = fields.Float(string="Custom Reserved Qty")

    pallet_info_ids = fields.One2many('stock.move.pallet.info', 'move_id', string="Pallet Distribution")

    qty_scanned = fields.Float(
        string="Scanned (PO)",
        help="Quantity fulfilled from Incoming PO (Warehouse Checking)",
        copy=False
    )
    qty_from_stock = fields.Float(
        string="Pick from Stock",
        compute='_compute_qty_from_stock',
        store=True,
        help="Quantity to pick from local stock (Demand - Scanned)"
    )

    @api.depends('product_uom_qty', 'qty_scanned','reserved_qty_custom')
    def _compute_qty_from_stock(self):
        for move in self:
            move.qty_from_stock = max(0.0, move.quantity - move.qty_scanned)

    def action_view_pallet_infos(self):
        """ Opens the Custom Pallet Info list for this move """
        self.ensure_one()
        return {
            'name': 'Pallet Distribution',
            'type': 'ir.actions.act_window',
            'res_model': 'stock.move.pallet.info',
            'view_mode': 'list,form',
            'domain': [('move_id', '=', self.id)],
            'context': {'default_move_id': self.id},
            'target': 'new',
        }

    def _action_assign(self, force_qty=False):
        t0 = time.time()

        moves_with_so_line = self.filtered(lambda m: m.sale_line_id)
        if not moves_with_so_line:
            return super()._action_assign(force_qty=force_qty)

        # 1. Build override map
        no_reserve_ids = []
        custom_qty_updates = {}

        for move in moves_with_so_line:
            sl = move.sale_line_id
            if not sl.reserve_qty:
                if move.product_uom_qty != 0.0:
                    no_reserve_ids.append(move.id)
            else:
                target = move.reserved_qty_custom
                if move.product_uom_qty != target:
                    custom_qty_updates[move.id] = target

        original_qtys = {
            move.id: move.product_uom_qty
            for move in moves_with_so_line
            if move.id in no_reserve_ids or move.id in custom_qty_updates
        }

        affected_ids = list(original_qtys.keys())

        t1 = time.time()
        _logger.info(f"STOCK_PERF: Step 1 (Build maps) took {t1-t0:.4f}s")

        # 2. Raw SQL writes
        cr = self.env.cr

        if no_reserve_ids:
            cr.execute(
                "UPDATE stock_move SET product_uom_qty = 0.0 WHERE id = ANY(%s)",
                (no_reserve_ids,)
            )

        qty_to_ids = {}
        for move_id, qty in custom_qty_updates.items():
            qty_to_ids.setdefault(qty, []).append(move_id)

        for qty, ids in qty_to_ids.items():
            cr.execute(
                "UPDATE stock_move SET product_uom_qty = %s WHERE id = ANY(%s)",
                (qty, ids)
            )

        # Targeted invalidation — only affected records, only relevant fields
        self.env['stock.move'].browse(affected_ids).invalidate_recordset(
            fnames=['product_uom_qty', 'product_qty']
        )

        t2 = time.time()
        _logger.info(f"STOCK_PERF: Step 2 (Raw SQL update) took {t2-t1:.4f}s")

        # 3. Core Odoo reservation logic
        res = super()._action_assign(force_qty=force_qty)

        t3 = time.time()
        _logger.info(f"STOCK_PERF: Step 3 (Super/Core Assign) took {t3-t2:.4f}s")

        # 4. Raw SQL restore
        restore_by_qty = {}
        for move_id, orig_qty in original_qtys.items():
            restore_by_qty.setdefault(orig_qty, []).append(move_id)

        for orig_qty, ids in restore_by_qty.items():
            cr.execute(
                "UPDATE stock_move SET product_uom_qty = %s WHERE id = ANY(%s)",
                (orig_qty, ids)
            )

        # Targeted invalidation again after restore
        self.env['stock.move'].browse(affected_ids).invalidate_recordset(
            fnames=['product_uom_qty', 'product_qty']
        )

        t4 = time.time()
        _logger.info(f"STOCK_PERF: Step 4 (Raw SQL restore) took {t4-t3:.4f}s")
        _logger.info(f"STOCK_PERF: TOTAL took {t4-t0:.4f}s")

        return res
