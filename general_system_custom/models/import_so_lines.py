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
        """Process the uploaded file and create SO lines.

        Lookup is by product.template.sku (not default_code). The optional
        ``brand`` column disambiguates when the same SKU exists for multiple
        brands; without it, an ambiguous SKU raises an error rather than
        silently picking one.
        """
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

            header = [str(cell).lower().strip() if cell is not None else '' for cell in rows[0]]

            try:
                sku_idx = header.index('sku')
                qty_idx = header.index('qty')
            except ValueError:
                raise UserError(_("File must contain columns 'SKU' and 'qty'. Found: %s") % header)

            price_idx = header.index('price') if 'price' in header else -1
            brand_idx = header.index('brand') if 'brand' in header else -1

            lines_to_create = []
            price_overrides = []

            for i, row in enumerate(rows[1:], start=2):
                if not row[sku_idx]:
                    continue

                sku = str(row[sku_idx]).strip()
                qty = float(row[qty_idx]) if row[qty_idx] else 0.0
                price = float(row[price_idx]) if price_idx != -1 and row[price_idx] else None
                brand = str(row[brand_idx]).strip() if brand_idx != -1 and row[brand_idx] else None

                product = self._find_product_for_import(sku, brand, row_number=i)

                line_vals = {
                    'order_id': sale_order.id,
                    'product_id': product.id,
                    'product_uom_qty': qty,
                }
                lines_to_create.append(line_vals)
                price_overrides.append(price)

            if lines_to_create:
                created_lines = self.env['sale.order.line'].create(lines_to_create)
                for line, price in zip(created_lines, price_overrides):
                    if price is not None:
                        line.write({'price_unit': price})

        except UserError:
            raise
        except Exception as e:
            raise UserError(_("Error processing file: %s") % str(e))

        return {'type': 'ir.actions.act_window_close'}

    def _find_product_for_import(self, sku, brand, row_number):
        """Resolve a product variant from (sku, brand) pair.

        Errors if the SKU matches products across multiple brands and no
        brand was provided to disambiguate.
        """
        Brand = self.env['product.brand']
        ProductTemplate = self.env['product.template']

        brand_rec = Brand
        if brand:
            brand_rec = Brand.search([('name', '=ilike', brand)], limit=1)
            if not brand_rec:
                raise UserError(_(
                    "Row %(row)s: Brand '%(brand)s' not found."
                ) % {'row': row_number, 'brand': brand})

        domain = [('sku', '=', sku)]
        if brand_rec:
            domain.append(('brand', '=', brand_rec.id))
        templates = ProductTemplate.search(domain)

        if not templates:
            raise UserError(_(
                "Row %(row)s: Product not found for SKU '%(sku)s'%(brand)s."
            ) % {
                'row': row_number,
                'sku': sku,
                'brand': _(" (brand '%s')") % brand if brand else '',
            })

        if len(templates) > 1:
            unique_brands = templates.mapped('brand')
            if len(unique_brands) > 1:
                brand_names = ', '.join(unique_brands.mapped('name')) or _('<no brand>')
                raise UserError(_(
                    "Row %(row)s: SKU '%(sku)s' exists for multiple brands "
                    "(%(brands)s). Please add a 'brand' column to disambiguate."
                ) % {'row': row_number, 'sku': sku, 'brands': brand_names})

        return templates[0].product_variant_ids[:1]