import base64
from io import BytesIO
from odoo import models, fields, _
from odoo.exceptions import UserError
import openpyxl


class ImportSOLines(models.TransientModel):
    _name = 'import.so.lines'
    _description = 'Import Sales Order Lines'

    file_data = fields.Binary('Excel File', required=True)
    file_name = fields.Char('File Name')

    def action_import(self):
        """ Process the uploaded file and create SO lines """
        if not openpyxl:
            raise UserError(_("The 'openpyxl' library is missing. Please install it to import Excel files."))
        
        self.ensure_one()
        sale_order = self.env['sale.order'].browse(self.env.context.get('active_id'))
        
        try:
            wb = openpyxl.load_workbook(filename=BytesIO(base64.b64decode(self.file_data)), read_only=True)
            ws = wb.active
            
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                raise UserError(_("The file is empty."))

            header = [str(cell).lower().strip() for cell in rows[0]]
            
            try:
                sku_idx = header.index('sku')
                qty_idx = header.index('qty')
            except ValueError:
                raise UserError(_("File must contain columns 'SKU' and 'qty'. Found: %s") % header)
            
            price_idx = header.index('price') if 'price' in header else -1

            lines_to_create = []
            
            for i, row in enumerate(rows[1:], start=2):
                if not row[sku_idx]: 
                    continue
                    
                sku = str(row[sku_idx]).strip()
                qty = float(row[qty_idx]) if row[qty_idx] else 0.0
                price = float(row[price_idx]) if price_idx != -1 and row[price_idx] else None
                
                # Find Product
                product = self.env['product.product'].search([('default_code', '=', sku)], limit=1)
                if not product:
                    raise UserError(_(f"Row {i}: Product not found for SKU '{sku}'"))

                line_vals = {
                    'order_id': sale_order.id,
                    'product_id': product.id,
                    'product_uom_qty': qty,
                }
                
                if price is not None:
                    line_vals['price_unit'] = price
                    
                lines_to_create.append(line_vals)
            
            if lines_to_create:
                self.env['sale.order.line'].create(lines_to_create)
                
        except UserError as e:
            raise UserError(_("Error processing file: %s") % str(e))
            
        return {'type': 'ir.actions.act_window_close'}
    