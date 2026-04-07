import base64
from io import BytesIO
from odoo import models, fields, api, _
from odoo.exceptions import UserError

try:
    import openpyxl
except ImportError:
    openpyxl = None

class DropshipImport(models.Model):
    _name = 'dropship.import'
    _description = 'Dropship Import List'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', default='New', copy=False, readonly=True)
    
    state = fields.Selection([
        ('draft', 'Draft'), 
        ('processed', 'Processed'), 
        ('done', 'Confirmed')
    ], default='draft', string='Status', tracking=True)

    customer_id = fields.Many2one('res.partner', string='Customer', required=True, help="Default Customer for lines with missing POs")
    supplier_doc_number = fields.Char(string='Supplier Document N.')
    
    file_data = fields.Binary('Excel File', required=True)
    file_name = fields.Char('File Name')

    line_ids = fields.One2many('dropship.import.line', 'import_id', string='Lines')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('dropship.import') or 'New'
        return super().create(vals_list)

    def action_process_file(self):
        """ Parse Excel and create Lines """
        self.ensure_one()
        if not openpyxl:
            raise UserError(_("The 'openpyxl' library is missing."))

        self.line_ids.unlink()

        try:
            wb = openpyxl.load_workbook(filename=BytesIO(base64.b64decode(self.file_data)), read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            
            if not rows: raise UserError("File is empty")

            headers = [str(h).lower().strip() if h else '' for h in rows[0]]
            try:
                sku_idx = headers.index('sku')
                qty_idx = headers.index('quantity')
                po_idx = headers.index('po number') if 'po number' in headers else -1
                plt_idx = headers.index('pallet number') if 'pallet number' in headers else -1
            except ValueError:
                raise UserError(_("Missing required columns: 'SKU', 'Quantity'."))

            lines_vals = []
            
            def get_cell(row, idx):
                return row[idx] if idx >= 0 and idx < len(row) else None

            for i, row in enumerate(rows[1:], start=2):
                sku_val = get_cell(row, sku_idx)
                if not sku_val: continue
                sku = str(sku_val).strip()

                qty_val = get_cell(row, qty_idx)
                qty = float(qty_val) if qty_val else 0.0
                if qty <= 0: continue

                po_name = str(get_cell(row, po_idx)).strip() if po_idx != -1 and get_cell(row, po_idx) else False
                pallet_name = str(get_cell(row, plt_idx)).strip() if plt_idx != -1 and get_cell(row, plt_idx) else False

                product = self.env['product.product'].search([('default_code', '=', sku)], limit=1)
                if not product:
                    raise UserError(_("Row %s: Product not found for SKU: %s") % (i, sku))

                # --- PO Logic ---
                if po_name:
                    # Explicit PO
                    po = self.env['purchase.order'].search([('name', '=', po_name)], limit=1)
                    if not po:
                        raise UserError(_("Row %s: PO '%s' not found.") % (i, po_name))
                    
                    po_line = po.order_line.filtered(lambda l: l.product_id == product)
                    if not po_line:
                        raise UserError(_("Row %s: Product %s not found on PO %s") % (i, sku, po_name))
                    
                    lines_vals.append({
                        'import_id': self.id,
                        'product_id': product.id,
                        'qty': qty,
                        'po_id': po.id,
                        'po_line_id': po_line[0].id,
                        'pallet_raw': pallet_name,
                        'customer_id': po.sale_order_id.partner_id.id,
                    })
                else:
                    # Auto Split Logic
                    if not self.customer_id:
                        raise UserError(_("Row %s: PO Number is blank. Select a Customer header.") % i)
                    
                    # Find relevant Dropship POs
                    domain = [
                        ('order_id.sale_order_id.partner_id', '=', self.customer_id.id),
                        ('product_id', '=', product.id),
                        ('order_id.state', '=', 'purchase'),
                        ('order_id.picking_type_id.code', '=', 'dropship')
                    ]
                    po_lines = self.env['purchase.order.line'].search(domain, order='date_order asc')
                    
                    qty_remaining = qty
                    for pol in po_lines:
                        if qty_remaining <= 0: break
                        open_qty = pol.product_qty - pol.qty_received
                        if open_qty <= 0: continue
                        
                        take_qty = min(qty_remaining, open_qty)
                        lines_vals.append({
                            'import_id': self.id,
                            'product_id': product.id,
                            'qty': take_qty,
                            'po_id': pol.order_id.id,
                            'po_line_id': pol.id,
                            'pallet_raw': pallet_name,
                            'customer_id': self.customer_id.id,
                        })
                        qty_remaining -= take_qty
                    
                    if qty_remaining > 0:
                        # Overflow line (allows manual PO selection later)
                        lines_vals.append({
                            'import_id': self.id,
                            'product_id': product.id,
                            'qty': qty_remaining,
                            'po_id': False,
                            'po_line_id': False,
                            'pallet_raw': pallet_name,
                            'customer_id': self.customer_id.id,
                        })

            self.env['dropship.import.line'].create(lines_vals)
            self.state = 'processed'

        except UserError:
            raise
        except Exception as e:
            raise UserError(_("Error processing file: %s") % str(e))

    def action_confirm(self):
        """ Confirm lines and create Pallets """
        self.ensure_one()
        if self.state != 'processed': return

        pallet_cache = {} # (name, customer) -> record
        new_pallets_cache = {} # customer -> record

        for line in self.line_ids:
            if not line.po_id:
                raise UserError(_("Line for %s has no PO selected. Please select a PO.") % line.product_id.name)

            target_pallet = False
            if line.pallet_raw:
                key = (line.pallet_raw, line.customer_id.id)
                if key in pallet_cache:
                    target_pallet = pallet_cache[key]
                else: 
                    target_pallet = self.env['warehouse.pallet'].create({
                        'name': 'New',
                        'partner_id': line.customer_id.id,
                        'pallet_type': 'virtual_pallet',
                        'state': 'dropship',
                        'shipment_method': 'courier'
                    })
                    pallet_cache[key] = target_pallet
            else:
                if line.customer_id.id in new_pallets_cache:
                    target_pallet = new_pallets_cache[line.customer_id.id]
                else:
                    target_pallet = self.env['warehouse.pallet'].create({
                        'name': 'New',
                        'partner_id': line.customer_id.id,
                        'pallet_type': 'virtual_pallet',
                        'state': 'dropship',
                        'shipment_method': 'courier'
                    })
                    new_pallets_cache[line.customer_id.id] = target_pallet

            # 2. Content Line
            so = line.po_id.sale_order_id
            picking = line.po_id.picking_ids.filtered(lambda p: p.picking_type_code == 'dropship')
            
            self.env['warehouse.checking.line'].create({
                'pallet_id': target_pallet.id,
                'product_id': line.product_id.id,
                'purchase_order_id': line.po_id.id,
                'purchase_line_id': line.po_line_id.id,
                'sale_order_id': so.id if so else False,
                'fulfill_qty': line.qty,
                'open_qty': line.qty,
                'is_dropship': True,
                'delivery_picking_id': picking[0].id if picking else False,
            })

        self.state = 'done'


class DropshipImportLine(models.Model):
    _name = 'dropship.import.line'
    _description = 'Dropship Import Line'

    import_id = fields.Many2one('dropship.import', ondelete='cascade')
    
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    qty = fields.Float(string='Qty')
    
    # Customer for this specific line (defaults to PO customer or Header customer)
    customer_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    
    # -- Editable PO Fields --
    po_id = fields.Many2one('purchase.order', string='PO', domain="[('id', 'in', available_po_ids)]")
    
    po_line_id = fields.Many2one(
        'purchase.order.line', 
        string='PO Line', 
        compute='_compute_po_line_id', 
        store=True, 
        readonly=False
    )
    
    available_po_ids = fields.Many2many('purchase.order', compute='_compute_available_po_ids')
    
    pallet_raw = fields.Char(string='Pallet (File)')

    @api.depends('product_id', 'customer_id')
    def _compute_available_po_ids(self):
        for line in self:
            if not line.product_id or not line.customer_id:
                line.available_po_ids = False
                continue
            
            # Find POs for this Product + Customer
            # Matching logic: PO linked to SO for this customer OR dropship address
            domain = [
                ('state', '=', 'purchase'),
                ('order_line.product_id', '=', line.product_id.id),
                ('picking_type_id.code', '=', 'dropship'), # Only Dropship POs
                ('sale_order_id.partner_id', '=', line.customer_id.id)
            ]
            line.available_po_ids = self.env['purchase.order'].search(domain)

    @api.depends('po_id', 'product_id')
    def _compute_po_line_id(self):
        for line in self:
            if line.po_id and line.product_id:
                # Find the specific line on this PO
                pol = line.po_id.order_line.filtered(lambda l: l.product_id == line.product_id)
                if pol:
                    line.po_line_id = pol[0]
            # Note: We don't force False else to avoid overwriting if set manually by script initially
            # but for manual UI changes, it will update.
            