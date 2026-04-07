import base64
import logging
from io import BytesIO

from odoo import models, fields, api, _
from odoo.exceptions import UserError

try:
    import openpyxl
except ImportError:
    openpyxl = None

_logger = logging.getLogger(__name__)


class DeliveryListExcelLine(models.Model):
    """Raw Excel data per SKU, used for validation and onchange lookups."""
    _name = 'delivery.list.excel.line'
    _description = 'Delivery List Excel Line'

    import_id = fields.Many2one(
        'delivery.list.import',
        string='Import',
        required=True,
        ondelete='cascade',
    )
    product_id = fields.Many2one('product.product', string='Product', required=True)
    sku = fields.Char(string='SKU', required=True)
    qty_received = fields.Float(string='Qty Received', required=True)
    price_supplier = fields.Float(string='Price (Supplier)')
    sequence = fields.Integer(string='Sequence', help='SKU appearance order in Excel')


class DeliveryListImport(models.Model):
    """
    Inbound Reconciliation — persistent, full-page feature.

    Imports supplier delivery documents and auto-splits received
    quantities across open Purchase Orders using FIFO logic.
    """
    _name = 'delivery.list.import'
    _description = 'Delivery List Import'

    name = fields.Char(string='Reference', default='New', copy=False, readonly=True)

    supplier_id = fields.Many2one(
        'res.partner',
        string='Supplier',
        required=True,
        help="Select the supplier who sent this delivery",
    )

    supplier_doc_number = fields.Char(
        string='Supplier Document N.',
        help="Supplier's delivery note or invoice reference number",
    )

    file_data = fields.Binary(
        string='Excel File',
        help="Upload Excel with columns: SKU, Qty, Price",
    )
    file_name = fields.Char(string='File Name')

    line_ids = fields.One2many(
        'delivery.list.line',
        'import_id',
        string='Split Lines',
    )

    excel_line_ids = fields.One2many(
        'delivery.list.excel.line',
        'import_id',
        string='Excel Lines',
    )

    state = fields.Selection(
        [('draft', 'Draft'), ('processed', 'Processed'), ('confirmed', 'Confirmed')],
        default='draft',
        string='Status',
    )

    # Summary stats
    total_received = fields.Integer(
        string='Total Items Received',
        compute='_compute_summary',
        store=True,
    )
    total_po_lines = fields.Integer(
        string='PO Lines Matched',
        compute='_compute_summary',
        store=True,
    )
    has_price_variance = fields.Boolean(
        string='Price Variance Detected',
        compute='_compute_summary',
        store=True,
    )
    has_under_allocation = fields.Boolean(
        string='Under-Allocation Detected',
        compute='_compute_summary',
        store=True,
    )
    has_over_allocation = fields.Boolean(
        string='Over-Allocation Detected',
        compute='_compute_summary',
        store=True,
    )

    @api.depends(
        'line_ids',
        'line_ids.qty_split',
        'line_ids.price_variance',
        'line_ids.is_under_allocation',
        'line_ids.is_over_allocation',
    )
    def _compute_summary(self):
        for rec in self:
            rec.total_received = sum(rec.line_ids.mapped('qty_split'))
            rec.total_po_lines = len(rec.line_ids)
            rec.has_price_variance = any(rec.line_ids.mapped('price_variance'))
            rec.has_under_allocation = any(rec.line_ids.mapped('is_under_allocation'))
            rec.has_over_allocation = any(rec.line_ids.mapped('is_over_allocation'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('delivery.list.import') or 'New'
        return super().create(vals_list)

    def action_process_file(self):
        """Parse Excel file and create split lines using FIFO logic."""
        self.ensure_one()

        if not openpyxl:
            raise UserError(_("The 'openpyxl' library is required for Excel import."))

        if not self.file_data:
            raise UserError(_("Please upload an Excel file before processing."))

        wb = openpyxl.load_workbook(
            filename=BytesIO(base64.b64decode(self.file_data)),
            read_only=True,
        )
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))

        if not rows:
            raise UserError(_("The Excel file is empty."))

        header = [str(cell).lower().strip() if cell else '' for cell in rows[0]]

        try:
            sku_idx = header.index('sku')
            qty_idx = header.index('qty')
            price_idx = header.index('price')
        except ValueError:
            raise UserError(_(
                "Excel file must contain columns: 'SKU', 'Qty', 'Price'\n"
                f"Found: {header}"
            ))

        # Clear existing lines
        self.line_ids.unlink()
        self.excel_line_ids.unlink()

        # Aggregate duplicate SKUs from Excel
        sku_data = {}  # {sku: {'qty': float, 'price': float, 'product': record}}
        sku_sequence = {}  # {sku: int} first appearance order

        max_idx = max(sku_idx, qty_idx, price_idx)

        for i, row in enumerate(rows[1:], start=2):
            if not row or len(row) <= max_idx:
                continue
            if not row[sku_idx]:
                continue

            sku = str(row[sku_idx]).strip()
            qty = float(row[qty_idx]) if row[qty_idx] else 0.0
            price = float(row[price_idx]) if row[price_idx] else 0.0

            if qty <= 0:
                continue

            if sku not in sku_data:
                product = self.env['product.product'].search([
                    ('default_code', '=', sku),
                ], limit=1)
                if not product:
                    raise UserError(_(
                        f"Row {i}: Product not found for SKU '{sku}'\n"
                        f"Please create the product first or check the SKU."
                    ))
                sku_data[sku] = {'qty': 0.0, 'price': price, 'product': product}
                sku_sequence[sku] = len(sku_sequence) + 1

            sku_data[sku]['qty'] += qty

        if not sku_data:
            raise UserError(_("No valid data found in the Excel file."))

        # Create excel lines (raw data for validation)
        excel_lines_vals = []
        for sku, data in sku_data.items():
            excel_lines_vals.append({
                'import_id': self.id,
                'product_id': data['product'].id,
                'sku': sku,
                'qty_received': data['qty'],
                'price_supplier': data['price'],
                'sequence': sku_sequence[sku],
            })
        self.env['delivery.list.excel.line'].create(excel_lines_vals)

        # Create split lines using FIFO
        lines_to_create = []

        for sku, data in sku_data.items():
            product = data['product']
            qty_received = data['qty']
            price_supplier = data['price']
            excel_seq = sku_sequence[sku]

            all_po_lines = self.env['purchase.order.line'].search([
                ('partner_id', '=', self.supplier_id.id),
                ('product_id', '=', product.id),
                ('order_id.state', '=', 'purchase'),
            ])

            sorted_po_lines = all_po_lines.sorted(
                key=lambda pol: pol.order_id.date_order
            )
            allocatable_po_lines = sorted_po_lines.filtered(lambda pol: pol.qty_open > 0)

            if not all_po_lines:
                raise UserError(_(
                    f"No Purchase Orders found for:\n"
                    f"Supplier: {self.supplier_id.name}\n"
                    f"Product: {sku} - {product.name}\n\n"
                    f"Please create a PO first."
                ))

            if not allocatable_po_lines:
                raise UserError(_(
                    f"No open quantity available for:\n"
                    f"Supplier: {self.supplier_id.name}\n"
                    f"Product: {sku} - {product.name}\n\n"
                    f"All POs for this product are fully received or allocated.\n"
                    f"Please create a new PO or verify existing PO quantities."
                ))

            remaining_qty = qty_received
            allocated_by_po_line = {}
            last_allocated_po_line = None

            for po_line in allocatable_po_lines:
                if remaining_qty <= 0:
                    break
                open_qty = po_line.qty_open
                take_qty = min(remaining_qty, open_qty)
                allocated_by_po_line[po_line.id] = take_qty
                last_allocated_po_line = po_line
                remaining_qty -= take_qty

            if remaining_qty > 0 and last_allocated_po_line:
                allocated_by_po_line[last_allocated_po_line.id] += remaining_qty

            for po_line in sorted_po_lines:
                assigned_qty = allocated_by_po_line.get(po_line.id, 0.0)
                if assigned_qty <= 0:
                    continue

                lines_to_create.append({
                    'import_id': self.id,
                    'product_id': product.id,
                    'sku': sku,
                    'qty_split': assigned_qty,
                    'price_supplier': price_supplier,
                    'po_line_id': po_line.id,
                    'po_id': po_line.order_id.id,
                    'price_po': po_line.price_unit,
                    'excel_sequence': excel_seq,
                })

        if lines_to_create:
            self.env['delivery.list.line'].create(lines_to_create)

        self.state = 'processed'

    def action_confirm_delivery(self):
        """Confirm and update PO receipts with allocated quantities."""
        self.ensure_one()

        if not self.line_ids:
            raise UserError(_("No lines to confirm. Please process the delivery first."))

        # Validation: for each product, sum(qty_split) must equal excel qty_received
        product_totals = {}
        for line in self.line_ids:
            pid = line.product_id.id
            if pid not in product_totals:
                product_totals[pid] = {
                    'sku': line.sku,
                    'product_name': line.product_id.display_name,
                    'total_split': 0.0,
                }
            product_totals[pid]['total_split'] += line.qty_split

        mismatches = []
        for excel_line in self.excel_line_ids:
            pid = excel_line.product_id.id
            pt = product_totals.get(pid)
            total_split = pt['total_split'] if pt else 0.0
            if abs(total_split - excel_line.qty_received) > 0.01:
                mismatches.append(
                    f"  {excel_line.sku}: allocated {total_split}, expected {excel_line.qty_received}"
                )

        if mismatches:
            raise UserError(_(
                "Cannot confirm: split quantities do not match received quantities.\n\n%s"
            ) % '\n'.join(mismatches))

        # Group allocations by PO line
        po_line_allocations = {}
        for line in self.line_ids:
            po_line_id = line.po_line_id.id
            if po_line_id not in po_line_allocations:
                po_line_allocations[po_line_id] = 0.0
            po_line_allocations[po_line_id] += line.qty_split

        for po_line_id, allocated_qty in po_line_allocations.items():
            po_line = self.env['purchase.order.line'].browse(po_line_id)
            po_line.write({'qty_split': po_line.qty_split + allocated_qty})
            _logger.info(
                f"Allocated {allocated_qty} to PO line {po_line.order_id.name} - "
                f"{po_line.product_id.default_code}"
            )

        self.write({'state': 'confirmed'})

        allocated_lines = len(po_line_allocations)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Delivery Confirmed'),
                'message': _('Successfully allocated quantities to %s PO line(s).', allocated_lines),
                'type': 'success',
                'sticky': False,
                'next': {
                    'type': 'ir.actions.act_window',
                    'res_model': 'delivery.list.import',
                    'res_id': self.id,
                    'views': [(False, 'form')],
                    'view_mode': 'form',
                    'target': 'current',
                },
            },
        }

    def action_reset_to_draft(self):
        """Reset from Processed to Draft. Clears lines but keeps the file."""
        self.ensure_one()

        if self.state != 'processed':
            raise UserError(_("Can only reset to draft from 'Processed' status."))

        # Clear the split lines and excel lines
        self.line_ids.unlink()
        self.excel_line_ids.unlink()

        self.write({'state': 'draft'})

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Reset to Draft'),
                'message': _('The import has been reset to draft. You can re-process or upload a different file.'),
                'type': 'info',
                'sticky': False,
                'next': {
                    'type': 'ir.actions.act_window',
                    'res_model': 'delivery.list.import',
                    'res_id': self.id,
                    'views': [(False, 'form')],
                    'view_mode': 'form',
                    'target': 'current',
                },
            },
        }

    def action_reset_to_processed(self):
        """Reset from Confirmed to Processed. Removes allocated quantities from PO lines."""
        self.ensure_one()

        if self.state != 'confirmed':
            raise UserError(_("Can only reset to processed from 'Confirmed' status."))

        # Group allocations by PO line (same logic as confirmation)
        po_line_allocations = {}
        for line in self.line_ids:
            po_line_id = line.po_line_id.id
            if po_line_id not in po_line_allocations:
                po_line_allocations[po_line_id] = 0.0
            po_line_allocations[po_line_id] += line.qty_split

        # Subtract the allocated quantities from PO lines
        for po_line_id, allocated_qty in po_line_allocations.items():
            po_line = self.env['purchase.order.line'].browse(po_line_id)
            new_qty_split = po_line.qty_split - allocated_qty

            # Safety check: don't go negative
            if new_qty_split < 0:
                raise UserError(_(
                    "Cannot reset: PO line %s would have negative allocated quantity. "
                    "This may indicate the quantities have already been used downstream."
                ) % po_line.order_id.name)

            po_line.write({'qty_split': new_qty_split})
            _logger.info(
                f"Removed {allocated_qty} from PO line {po_line.order_id.name} - "
                f"{po_line.product_id.default_code}"
            )

        self.write({'state': 'processed'})

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Reset to Processed'),
                'message': _('The import has been reset to processed. Allocated quantities have been removed from PO lines.'),
                'type': 'warning',
                'sticky': False,
                'next': {
                    'type': 'ir.actions.act_window',
                    'res_model': 'delivery.list.import',
                    'res_id': self.id,
                    'views': [(False, 'form')],
                    'view_mode': 'form',
                    'target': 'current',
                },
            },
        }


class DeliveryListLine(models.Model):
    """
    Individual split line showing how received quantity
    is distributed across Purchase Orders.
    """
    _name = 'delivery.list.line'
    _description = 'Delivery List Split Line'
    _order = 'excel_sequence asc, po_date asc, id asc'

    import_id = fields.Many2one(
        'delivery.list.import',
        string='Import',
        required=True,
        ondelete='cascade',
    )

    # Related fields from parent import (for view domains and visibility)
    state = fields.Selection(
        related='import_id.state',
        string='Import State',
        store=True,
        readonly=True,
    )
    supplier_id = fields.Many2one(
        'res.partner',
        related='import_id.supplier_id',
        string='Supplier',
        store=True,
        readonly=True,
    )
    available_product_ids = fields.Many2many(
        'product.product',
        compute='_compute_available_product_ids',
        string='Available Products',
    )
    available_po_ids = fields.Many2many(
        'purchase.order',
        compute='_compute_available_po_ids',
        string='Available POs',
    )

    # Product info
    product_id = fields.Many2one('product.product', string='Product', required=True)
    sku = fields.Char(
        string='SKU',
        compute='_compute_sku',
        store=True,
        readonly=False,
    )

    # Quantities
    qty_split = fields.Float(
        string='Qty (Split)',
        required=True,
        help="Quantity assigned to this PO from the total received",
    )
    qty_received_po = fields.Float(
        string='Received Qty',
        related='po_line_id.qty_received',
        store=False,
        help="Quantity already received in the warehouse for this PO line (live value)",
    )
    open_qty_po = fields.Float(
        string='Open Qty PO',
        related='po_line_id.qty_open',
        store=False,
        help="Remaining quantity needed on this PO before delivery (live value)",
    )

    # Prices
    price_supplier = fields.Monetary(
        string='Price (Supplier)',
        currency_field='currency_id',
        compute='_compute_price_supplier',
        store=True,
        readonly=False,
        help="Price from supplier's invoice (auto-filled from Excel, editable)",
    )
    price_po = fields.Monetary(
        string='Price PO',
        currency_field='currency_id',
        compute='_compute_price_po',
        store=True,
        readonly=False,
        help="Price on our Purchase Order (auto-filled from PO line)",
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='po_id.currency_id',
        string='Currency',
    )

    # Links
    po_line_id = fields.Many2one(
        'purchase.order.line',
        string='PO Line',
        compute='_compute_po_line_id',
        store=True,
        readonly=False,
    )
    po_id = fields.Many2one(
        'purchase.order',
        string='PO Number',
    )
    so_id = fields.Many2one(
        'sale.order',
        related='po_id.sale_order_id',
        string='SO Number',
        store=True,
    )
    customer_id = fields.Many2one(
        'res.partner',
        related='so_id.partner_id',
        string='Customer',
        store=True,
    )

    # Alerts
    price_variance = fields.Boolean(
        string='Price Variance',
        compute='_compute_price_variance',
        store=True,
        help="Supplier price differs from PO price",
    )
    price_difference = fields.Monetary(
        string='Price Diff',
        compute='_compute_price_variance',
        store=True,
        currency_field='currency_id',
    )

    is_under_allocation = fields.Boolean(
        string='Under-Allocation',
        compute='_compute_allocation_flags',
        store=True,
        help="Split qty is less than PO open qty (yellow)",
    )
    is_over_allocation = fields.Boolean(
        string='Over-Allocation',
        compute='_compute_allocation_flags',
        store=True,
        help="Split qty exceeds PO open qty (red)",
    )

    # Ordering
    excel_sequence = fields.Integer(
        string='Excel Sequence',
        help="SKU appearance order from Excel file",
    )
    po_date = fields.Datetime(
        related='po_id.date_order',
        string='PO Date',
        store=True,
    )

    @api.depends('import_id.excel_line_ids.product_id', 'po_id')
    def _compute_available_product_ids(self):
        """Products selectable on this line: from Excel, narrowed by selected PO."""
        for line in self:
            if not line.import_id or not line.import_id.excel_line_ids:
                line.available_product_ids = False
                continue

            excel_products = line.import_id.excel_line_ids.mapped('product_id')

            if line.po_id:
                po_products = line.po_id.order_line.mapped('product_id')
                line.available_product_ids = excel_products & po_products
            else:
                line.available_product_ids = excel_products

    @api.depends('import_id.supplier_id', 'import_id.excel_line_ids.product_id', 'product_id')
    def _compute_available_po_ids(self):
        """POs selectable on this line: from supplier, narrowed by selected product from Excel."""
        for line in self:
            if not line.import_id or not line.import_id.supplier_id:
                line.available_po_ids = False
                continue

            # Base: POs from this supplier in purchase state
            domain = [
                ('partner_id', '=', line.import_id.supplier_id.id),
                ('state', '=', 'purchase'),
            ]

            if line.product_id:
                # Narrow to POs that have the selected product in their lines
                domain += [('order_line.product_id', '=', line.product_id.id)]

            else:
                # No product selected: narrow to POs that have ANY Excel product
                excel_product_ids = line.import_id.excel_line_ids.mapped('product_id').ids
                if excel_product_ids:
                    domain += [('order_line.product_id', 'in', excel_product_ids)]

            line.available_po_ids = self.env['purchase.order'].search(domain)

    @api.depends('qty_split', 'open_qty_po')
    def _compute_allocation_flags(self):
        for line in self:
            line.is_under_allocation = (line.qty_split + 0.01) < line.open_qty_po
            line.is_over_allocation = (line.qty_split - 0.01) > line.open_qty_po

    @api.depends('price_supplier', 'price_po')
    def _compute_price_variance(self):
        for line in self:
            if line.price_supplier and line.price_po:
                diff = abs(line.price_supplier - line.price_po)
                line.price_variance = diff > 0.01
                line.price_difference = line.price_supplier - line.price_po
            else:
                line.price_variance = False
                line.price_difference = 0.0

    @api.depends('product_id', 'import_id.excel_line_ids.product_id')
    def _compute_sku(self):
        for line in self:
            if line.product_id and line.import_id:
                excel_line = line.import_id.excel_line_ids.filtered(
                    lambda l: l.product_id == line.product_id
                )[:1]
                line.sku = excel_line.sku if excel_line else line.product_id.default_code or ''
            else:
                line.sku = line.product_id.default_code or '' if line.product_id else ''

    @api.depends('product_id', 'import_id.excel_line_ids.product_id')
    def _compute_price_supplier(self):
        for line in self:
            if line.product_id and line.import_id:
                excel_line = line.import_id.excel_line_ids.filtered(
                    lambda l: l.product_id == line.product_id
                )[:1]
                line.price_supplier = excel_line.price_supplier if excel_line else 0.0
            else:
                line.price_supplier = 0.0

    @api.depends('po_id', 'product_id')
    def _compute_po_line_id(self):
        for line in self:
            if line.po_id and line.product_id:
                po_line = self.env['purchase.order.line'].search([
                    ('order_id', '=', line.po_id.id),
                    ('product_id', '=', line.product_id.id),
                ], limit=1)
                line.po_line_id = po_line or False
            else:
                line.po_line_id = False

    @api.depends('po_line_id')
    def _compute_price_po(self):
        for line in self:
            line.price_po = line.po_line_id.price_unit if line.po_line_id else 0.0

