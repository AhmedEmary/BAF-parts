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

    state = fields.Selection(
        [('upload', 'Upload'), ('conflicts', 'Resolve Brand Conflicts')],
        default='upload',
        required=True,
    )
    conflict_ids = fields.One2many(
        'import.so.lines.conflict', 'wizard_id',
        string='Ambiguous SKUs',
    )

    def action_import(self):
        """Process the uploaded file and create SO lines.

        Two-pass flow:
          1. Parse + validate every row. If any SKU is ambiguous (matches
             multiple products across different brands and no brand column
             entry resolves it), collect those into ``conflict_ids`` and
             switch to the ``conflicts`` state so the user can pick a brand
             per SKU.
          2. Re-run with the user's brand picks applied and create the
             SO lines.
        """
        if not openpyxl:
            raise UserError(_("The 'openpyxl' library is missing. Please install it to import Excel files."))

        self.ensure_one()
        sale_order = self.env['sale.order'].browse(self.env.context.get('active_id'))

        if self.state == 'conflicts':
            unresolved = self.conflict_ids.filtered(lambda c: not c.brand_id)
            if unresolved:
                skus = ', '.join(unresolved.mapped('sku'))
                raise UserError(_(
                    "Please pick a brand for every ambiguous SKU before "
                    "continuing. Still missing: %s"
                ) % skus)

        try:
            wb = openpyxl.load_workbook(filename=BytesIO(base64.b64decode(self.file_data)), read_only=True)
            ws = wb.active

            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                raise UserError(_("The file is empty."))

            header = [
                str(cell).lower().strip() if cell is not None else ''
                for cell in (rows[0] or ())
            ]

            try:
                sku_idx = header.index('sku')
                qty_idx = header.index('qty')
            except ValueError:
                raise UserError(_("File must contain columns 'SKU' and 'qty'. Found: %s") % header)

            price_idx = header.index('price') if 'price' in header else -1
            brand_idx = header.index('brand') if 'brand' in header else -1

            # User-resolved brand overrides from the conflicts step
            brand_overrides = {
                (c.sku or '').strip(): c.brand_id
                for c in self.conflict_ids
                if c.brand_id
            }

            resolved_rows = []         # (product_variant, qty, price)
            conflicts = {}             # sku → set(brand_ids)
            unresolved_lookups = []    # rows pending user brand pick

            for i, row in enumerate(rows[1:], start=2):
                sku_raw = self._cell(row, sku_idx)
                if sku_raw is None or str(sku_raw).strip() == '':
                    continue

                sku = str(sku_raw).strip()

                qty_raw = self._cell(row, qty_idx)
                qty = float(qty_raw) if qty_raw not in (None, '') else 0.0

                price_raw = self._cell(row, price_idx) if price_idx != -1 else None
                price = float(price_raw) if price_raw not in (None, '') else None

                brand_raw = self._cell(row, brand_idx) if brand_idx != -1 else None
                brand = str(brand_raw).strip() if brand_raw not in (None, '') else None

                # Apply user override from a previous conflicts step
                if not brand and sku in brand_overrides:
                    brand = brand_overrides[sku].name

                product, conflict_brand_ids = self._resolve_product(sku, brand, row_number=i)

                if product:
                    resolved_rows.append((product, qty, price))
                else:
                    # ambiguous — remember the candidate brands for the UI
                    conflicts.setdefault(sku, set()).update(conflict_brand_ids)
                    unresolved_lookups.append((i, sku))

            if conflicts:
                # Drop the previous list and re-populate with fresh conflicts
                self.conflict_ids.unlink()
                self.write({
                    'state': 'conflicts',
                    'conflict_ids': [
                        (0, 0, {
                            'sku': sku,
                            'candidate_brand_ids': [(6, 0, list(brand_ids))],
                        })
                        for sku, brand_ids in conflicts.items()
                    ],
                })
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': 'import.so.lines',
                    'res_id': self.id,
                    'view_mode': 'form',
                    'target': 'new',
                    'name': _('Resolve Brand Conflicts'),
                    'context': self.env.context,
                }

            if resolved_rows:
                lines_to_create = [
                    {
                        'order_id': sale_order.id,
                        'product_id': product.id,
                        'product_uom_qty': qty,
                    }
                    for (product, qty, _price) in resolved_rows
                ]
                created_lines = self.env['sale.order.line'].create(lines_to_create)
                for line, (_product, _qty, price) in zip(created_lines, resolved_rows):
                    if price is not None:
                        line.write({'price_unit': price})

        except UserError:
            raise
        except Exception as e:
            raise UserError(_("Error processing file: %s") % str(e))

        return {'type': 'ir.actions.act_window_close'}

    @staticmethod
    def _cell(row, idx):
        """Safely fetch a cell from an openpyxl row; short rows return None."""
        if row is None or idx is None or idx < 0 or idx >= len(row):
            return None
        return row[idx]

    def _resolve_product(self, sku, brand, row_number):
        """Resolve a (sku, brand) pair to a product variant.

        Returns ``(product, [])`` on success.
        Returns ``(False, [brand_ids])`` if the SKU is ambiguous across
        multiple brands so the caller can prompt the user. Raises UserError
        only for hard failures (unknown SKU, unknown brand name).
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

        unique_brands = templates.mapped('brand')
        if len(templates) > 1 and len(unique_brands) > 1:
            return (False, unique_brands.ids)

        return (templates[0].product_variant_ids[:1], [])


class ImportSOLinesConflict(models.TransientModel):
    _name = 'import.so.lines.conflict'
    _description = 'Ambiguous SKU resolution for SO line import'

    wizard_id = fields.Many2one(
        'import.so.lines', required=True, ondelete='cascade', index=True,
    )
    sku = fields.Char(string='SKU', readonly=True, required=True)
    candidate_brand_ids = fields.Many2many(
        'product.brand', string='Available Brands',
        help="Brands under which this SKU exists in the catalogue.",
    )
    brand_id = fields.Many2one(
        'product.brand', string='Pick Brand',
        domain="[('id', 'in', candidate_brand_ids)]",
    )
