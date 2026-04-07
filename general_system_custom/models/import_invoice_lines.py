import base64
from io import BytesIO
from odoo import models, fields, _
from odoo.exceptions import UserError
import openpyxl

class ImportInvoiceLines(models.TransientModel):
    _name = 'import.invoice.lines'
    _description = 'Import Manual Invoice Lines'

    file_data = fields.Binary('Excel File', required=True)
    file_name = fields.Char('File Name')

    def action_import(self):
        """Manual Invoice & Excel Import Logic """
        self.ensure_one()
        if not openpyxl:
            raise UserError(_("The 'openpyxl' library is missing."))

        invoice = self.env['account.move'].browse(self.enc.context.get('active_id'))
        if not invoice or invoice.state != 'draft':
            raise UserError(_("You can only import lines into a Draft Invoice."))

        try:
            wb = openpyxl.load_workbook(filename=BytesIO(base64.b64decode(self.file_data)), read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            
            if not rows: raise UserError("File is empty")

            headers = [str(h).lower().strip() for h in rows[0]]
            try:
                sku_idx = headers.index('sku')
                qty_idx = headers.index('qty')
                price_idx = headers.index('price')
                so_idx = headers.index('so number')
            except ValueError as e:
                raise UserError(_("Missing required columns: SKU, Qty, Price, SO Number"))

            lines_to_create = []

            for row in rows[1:]:
                if not row[sku_idx]: continue

                sku = str(row[sku_idx]).strip()
                qty = float(row[qty_idx]) if row[qty_idx] else 0.0
                price = float(row[price_idx]) if row[price_idx] else 0.0
                so_name = str(row[so_idx]).strip()

                product = self.env['product.product'].search([('default_code', '=', sku)], limit=1)
                if not product:
                    raise UserError(_("Product not found for SKU: %s") % sku)

                so = self.env['sale.order'].search([('name', '=', so_name)], limit=1)
                if not so:
                    raise UserError(_("Sales Order not found: %s") % so_name)

                so_line = so.order_line.filtered(lambda l: l.product_id == product)
                
                vals = {
                    'move_id': invoice.id,
                    'product_id': product.id,
                    'quantity': qty,
                    'price_unit': price,
                    'sale_line_ids': [(6, 0, so_line.ids)] if so_line else [], 
                    'origin_country_id': product.origin.id,
                    'hs_code': product.hs_code,
                }
                lines_to_create.append(vals)

            if lines_to_create:
                self.env['account.move.line'].create(lines_to_create)

        except Exception as e:
            raise UserError(_("Error processing file: %s") % str(e))
            
        return {'type': 'ir.actions.act_window_close'}
