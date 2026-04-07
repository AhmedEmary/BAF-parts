from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
import base64
from io import BytesIO
import openpyxl
import qrcode


class WarehousePallet(models.Model):
    _name = 'warehouse.pallet'
    _description = 'Shipment List & Outbound Management'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'ready_date desc, name desc'

    name = fields.Char(string='Pallet Number', required=True, copy=False, readonly=True, default=lambda self: _('New'))
    
    partner_id = fields.Many2one('res.partner', string='Customer', tracking=True, required=True)
    
    # Dates
    ready_date = fields.Date(string='Ready Date', default=fields.Date.context_today)
    pickup_date = fields.Date(string='Pick Up Date', tracking=True)
    
    # Dimensions & Weight
    weight = fields.Float(string='Gross Weight (kg)', help="Total weight including goods and pallet base.")
    length = fields.Float(string='Length (cm)', default=120.0)
    width = fields.Float(string='Width (cm)', default=80.0)
    height = fields.Float(string='Height (cm)')
    
    # Shipment Info
    shipment_method = fields.Selection([
        ('air', 'Air'),
        ('road', 'Road'),
        ('sea', 'Sea'),
        ('courier', 'Courier')
    ], string='Shipment Method', tracking=True)
    
    invoice_id = fields.Many2one('account.move', string='Invoice N.', readonly=True, copy=False)
    
    line_ids = fields.One2many('warehouse.checking.line', 'pallet_id', string='Pallet Contents')
    
    state = fields.Selection([
        ('open', 'Open'), 
        ('ready', 'Ready'), 
        ('shipped', 'Picked Up'),
        ('dropship', 'Dropship')
    ], default='open', string='Status', tracking=True)

    pallet_type = fields.Selection([
        ('inhouse', 'In-House'),
        ('virtual_pallet', 'Virtual Pallet'),
        ('my_dropship', 'My Dropship')
    ], string='Pallet Type', default='inhouse', required=True, tracking=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('warehouse.pallet') or _('New')
                
        return super().create(vals_list)

    def action_mark_ready(self):
        self.state = 'ready'
        self.ready_date = fields.Date.today()

    def action_mark_shipped(self):
        self.state = 'shipped'
        self.pickup_date = fields.Date.today()

    def action_reopen(self):
        self.state = 'open'
        self.pickup_date = False

    def action_print_label(self):
        return self.env.ref('intelliwise_custom.action_report_pallet_label').report_action(self)

    @api.ondelete(at_uninstall=False)
    def _unlink_if_empty(self):
        for pallet in self:
            if pallet.line_ids:
                raise UserError(_("You cannot delete Pallet %s because it contains items. Please empty it first.") % pallet.name)

    def action_generate_invoice(self):
        if not self: return
        customers = self.mapped('partner_id')
        if len(set(customers)) > 1:
            raise UserError(_("Cannot generate a single invoice for multiple customers. Please select pallets for one customer only."))
        
        customer = customers[0]
        invoice_vals = {
            'move_type': 'out_invoice',
            'partner_id': customer.id,
            'invoice_date': fields.Date.today(),
            'currency_id': customer.property_product_pricelist.currency_id.id or self.env.company.currency_id.id,
            'invoice_origin': ", ".join(self.mapped('name')),
        }
        invoice_lines = []
        for pallet in self:
            for line in pallet.line_ids:
                so_line = line.sale_line_id
                line_vals = {
                    'product_id': line.product_id.id,
                    'quantity': line.fulfill_qty,
                    'price_unit': so_line.price_unit if so_line else line.product_id.list_price,
                    'tax_ids': [(6, 0, so_line.tax_ids.ids)] if so_line else [],
                    'sale_line_ids': [(6, 0, [so_line.id])] if so_line else [],
                    'origin_country_id': line.product_id.origin.id,
                    'hs_code': line.product_id.hs_code,
                    'supplier_delivery_ref': line.picking_id.name,
                    'linked_so_id': line.sale_order_id.id,
                    'linked_po_id': line.purchase_order_id.id,
                }
                invoice_lines.append((0, 0, line_vals))
        invoice_vals['invoice_line_ids'] = invoice_lines
        try:
            invoice = self.env['account.move'].create(invoice_vals)
            self.write({'invoice_id': invoice.id})
            return {
                'name': 'Draft Invoice',
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'view_mode': 'form',
                'res_id': invoice.id,
            }
        except Exception as e:
            raise UserError(_("Failed to generate invoice: %s") % str(e))

    def action_view_invoice(self):
        self.ensure_one()
        return {
            'name': 'Invoice',
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.invoice_id.id,
        }
        
    def get_qr_code(self):
        self.ensure_one()
        if not qrcode: return False
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
        qr.add_data(self.name)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode()

    def action_export_packing_list_excel(self):
        self.ensure_one()
        if not openpyxl: raise UserError(_("The 'openpyxl' library is missing."))
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Packing List"
        ws.append(["PACKING LIST"])
        ws.append(["Pallet Number:", self.name])
        ws.append(["Customer:", self.partner_id.name])
        ws.append(["Date:", str(self.ready_date or "")])
        ws.append(["Shipment Method:", dict(self._fields['shipment_method'].selection).get(self.shipment_method) or ""])
        ws.append([])
        ws.append(["Dimensions (cm)", "Weight (kg)", "Ref"])
        ws.append([f"{self.length}x{self.width}x{self.height}", self.weight])
        ws.append([])
        headers = ["SKU", "Description", "Sales Order", "Purchase Order", "Quantity"]
        ws.append(headers)
        for line in self.line_ids:
            ws.append([
                line.product_id.default_code or "",
                line.product_id.name or "",
                line.sale_order_id.name or "",
                line.purchase_order_id.name or "",
                line.fulfill_qty
            ])
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        file_content = base64.b64encode(output.read())
        output.close()
        filename = f"Packing_List_{self.name}.xlsx"
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': file_content,
            'res_model': 'warehouse.pallet',
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }
