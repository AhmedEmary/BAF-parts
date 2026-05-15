import base64
from io import BytesIO
from odoo import models, fields, _
from odoo.exceptions import UserError
import openpyxl


# Header-label synonyms (compared after lower-casing + stripping).
_HEADER_SYNONYMS = {
    'sku':   {'sku', 'code', 'codice', 'article', 'articolo', 'art',
              'artikel', 'artikelnummer', 'reference', 'ref', 'part',
              'partnumber', 'part number', 'part no', 'part-no', 'item',
              'item code', 'internal reference', 'default_code',
              'oem', 'oem number'},
    'qty':   {'qty', 'quantity', 'quantità', 'qta', 'q.ty', 'menge',
              'anzahl', 'pieces', 'pcs', 'stk', 'stück', 'stuck', 'count'},
    'price': {'price', 'unit price', 'preis', 'prezzo', 'prix', 'amount',
              'net price', 'netto'},
    'brand': {'brand', 'marke', 'make', 'marca', 'manufacturer',
              'hersteller', 'maker'},
}


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

    # ──────────────────────────────────────────────────────────────────────
    # Entry point
    # ──────────────────────────────────────────────────────────────────────
    def action_import(self):
        """Parse the uploaded file and create SO lines.

        The importer is intentionally forgiving:
          * Columns are found by header synonyms; missing roles are inferred
            from the column data (brand names, numeric values, catalogue
            matches). A file with no header works too.
          * Each value is resolved by SKU, then by Internal Reference, then
            by splitting a brand prefix ("BMW_12345").
          * A blank/missing quantity defaults to 1.
          * A SKU that exists for several brands triggers an interactive
            brand-picker instead of failing.
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

            roles, data_rows, first_row_number = self._detect_columns(rows)
            sku_idx = roles.get('sku', 0)
            qty_idx = roles.get('qty', -1)
            price_idx = roles.get('price', -1)
            brand_idx = roles.get('brand', -1)

            # User-resolved brand overrides from the conflicts step
            brand_overrides = {
                (c.sku or '').strip(): c.brand_id
                for c in self.conflict_ids
                if c.brand_id
            }

            resolved_rows = []   # (product_variant, qty, price)
            conflicts = {}       # sku → set(brand_ids)

            for i, row in enumerate(data_rows, start=first_row_number):
                sku_raw = self._cell(row, sku_idx)
                if sku_raw is None or str(sku_raw).strip() == '':
                    continue
                sku = str(sku_raw).strip()

                qty = self._to_float(self._cell(row, qty_idx))
                if qty is None:
                    qty = 1.0

                price = self._to_float(self._cell(row, price_idx))

                brand_raw = self._cell(row, brand_idx)
                brand = str(brand_raw).strip() if brand_raw not in (None, '') else None
                if not brand and sku in brand_overrides:
                    brand = brand_overrides[sku].name

                product, conflict_brand_ids = self._resolve_product(sku, brand, row_number=i)
                if product:
                    resolved_rows.append((product, qty, price))
                else:
                    conflicts.setdefault(sku, set()).update(conflict_brand_ids)

            if conflicts:
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

    # ──────────────────────────────────────────────────────────────────────
    # Column detection
    # ──────────────────────────────────────────────────────────────────────
    def _detect_columns(self, rows):
        """Return (roles, data_rows, first_row_number).

        roles maps 'sku' / 'qty' / 'price' / 'brand' → column index. Roles
        not found from the header row are inferred from the column data.
        """
        header = [self._norm(c) for c in (rows[0] or ())]

        roles = {}
        for idx, label in enumerate(header):
            for role, synonyms in _HEADER_SYNONYMS.items():
                if role not in roles and label in synonyms:
                    roles[role] = idx
                    break

        if roles:
            # Row 0 carried at least one recognised header label → it is a
            # header row; data starts on the next row.
            data_rows, first_row_number = rows[1:], 2
        else:
            # No header at all → every row is data.
            data_rows, first_row_number = rows, 1

        self._detect_columns_by_content(data_rows, roles)
        return roles, data_rows, first_row_number

    def _detect_columns_by_content(self, data_rows, roles):
        """Fill any missing role by inspecting the actual column values."""
        if not data_rows:
            roles.setdefault('sku', 0)
            return

        n_cols = max((len(r) for r in data_rows if r), default=0)
        if n_cols == 0:
            roles.setdefault('sku', 0)
            return

        sample = data_rows[:40]
        brand_names = {
            (b.name or '').strip().lower()
            for b in self.env['product.brand'].search([])
            if b.name
        }

        stats = []
        for idx in range(n_cols):
            vals = [self._cell(r, idx) for r in sample]
            vals = [str(v).strip() for v in vals if v not in (None, '')]
            n = len(vals) or 1
            stats.append({
                'idx': idx,
                'count': len(vals),
                'brand': sum(1 for v in vals if v.lower() in brand_names) / n,
                'numeric': sum(1 for v in vals if self._to_float(v) is not None) / n,
                'vals': vals,
            })

        used = set(roles.values())

        # Brand — a column where most values are known brand names.
        if 'brand' not in roles:
            free = [s for s in stats if s['idx'] not in used and s['count']]
            best = max(free, key=lambda s: s['brand'], default=None)
            if best and best['brand'] >= 0.5:
                roles['brand'] = best['idx']
                used.add(best['idx'])

        # SKU — the column whose values best match the product catalogue.
        if 'sku' not in roles:
            best_idx, best_score = None, -1.0
            for s in stats:
                if s['idx'] in used or not s['count']:
                    continue
                score = self._catalogue_match_ratio(s['vals'])
                if score > best_score:
                    best_idx, best_score = s['idx'], score
            if best_idx is None or best_score <= 0.0:
                # Nothing matched — fall back to the leftmost free column.
                for s in stats:
                    if s['idx'] not in used:
                        best_idx = s['idx']
                        break
            roles['sku'] = best_idx if best_idx is not None else 0
            used.add(roles['sku'])

        # Qty — the first remaining numeric column.
        if 'qty' not in roles:
            for s in stats:
                if s['idx'] in used or not s['count']:
                    continue
                if s['numeric'] >= 0.6:
                    roles['qty'] = s['idx']
                    used.add(s['idx'])
                    break

    def _catalogue_match_ratio(self, values):
        """Fraction of sampled values that exist as a SKU or Internal Reference."""
        sample = values[:12]
        if not sample:
            return 0.0
        Template = self.env['product.template']
        hits = 0
        for v in sample:
            if Template.search_count(
                ['|', ('sku', '=', v), ('default_code', '=', v)], limit=1,
            ):
                hits += 1
        return hits / len(sample)

    # ──────────────────────────────────────────────────────────────────────
    # Product resolution
    # ──────────────────────────────────────────────────────────────────────
    def _resolve_product(self, raw_value, brand, row_number):
        """Resolve a (value, brand) pair to a product variant.

        Returns ``(product, [])`` on success or ``(False, [brand_ids])`` when
        the value is ambiguous across several brands. Raises UserError for a
        hard failure (unknown value, unknown brand name).
        """
        Brand = self.env['product.brand']
        value = (raw_value or '').strip()
        if not value:
            raise UserError(_("Row %s: empty SKU.") % row_number)

        brand_rec = Brand
        if brand:
            brand_rec = (
                Brand.search([('name', '=ilike', brand)], limit=1)
                or self._brand_from_prefix(brand)
            )
            if not brand_rec:
                raise UserError(_(
                    "Row %(row)s: Brand '%(brand)s' not found."
                ) % {'row': row_number, 'brand': brand})

        templates = self._lookup_templates(value, brand_rec)

        # No brand given and nothing found yet → try a brand-prefixed code.
        if not templates and not brand_rec:
            prefix_brand, rest = self._split_brand_prefix(value)
            if prefix_brand and rest:
                templates = self._lookup_templates(rest, prefix_brand)

        if not templates:
            raise UserError(_(
                "Row %(row)s: Product not found for '%(sku)s'%(brand)s."
            ) % {
                'row': row_number,
                'sku': value,
                'brand': _(" (brand '%s')") % brand if brand else '',
            })

        unique_brands = templates.mapped('brand')
        if len(templates) > 1 and len(unique_brands) > 1:
            return (False, unique_brands.ids)

        return (templates[0].product_variant_ids[:1], [])

    def _lookup_templates(self, value, brand_rec):
        """Search templates by SKU, then by Internal Reference (default_code)."""
        Template = self.env['product.template']
        brand_domain = [('brand', '=', brand_rec.id)] if brand_rec else []

        templates = Template.search(brand_domain + [('sku', '=', value)])
        if templates:
            return templates

        candidates = {value, value.replace('-', '_').replace(' ', '_')}
        return Template.search(
            brand_domain + [('default_code', 'in', list(candidates))]
        )

    def _split_brand_prefix(self, value):
        """Split "BMW_12345" / "BMW-12345" / "BMW 12345" into (brand, rest)."""
        Brand = self.env['product.brand']
        for sep in ('_', '-', ' '):
            if sep in value:
                prefix, _sep, rest = value.partition(sep)
                prefix, rest = prefix.strip(), rest.strip()
                if prefix and rest:
                    brand = self._brand_from_prefix(prefix)
                    if brand:
                        return brand, rest
        return Brand, value

    def _brand_from_prefix(self, prefix):
        """Resolve a brand from a full name or its 3-letter reference prefix."""
        Brand = self.env['product.brand']
        prefix = (prefix or '').strip()
        if not prefix:
            return Brand

        brand = Brand.search([('name', '=ilike', prefix)], limit=1)
        if brand:
            return brand

        # Internal Reference is built as NAME[:3].upper() + '_' + SKU.
        matches = Brand.search([('name', '=ilike', prefix + '%')])
        exact = matches.filtered(
            lambda b: (b.name or '')[:len(prefix)].upper() == prefix.upper()
        )
        if len(exact) == 1:
            return exact
        return Brand

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _norm(cell):
        return str(cell).strip().lower() if cell is not None else ''

    @staticmethod
    def _cell(row, idx):
        """Safely fetch a cell; short rows / negative indexes return None."""
        if row is None or idx is None or idx < 0 or idx >= len(row):
            return None
        return row[idx]

    @staticmethod
    def _to_float(value):
        """Parse a cell into a float, tolerating European decimals; None if blank."""
        if value is None or value == '':
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        if not s:
            return None
        if ',' in s and '.' in s:
            s = s.replace('.', '').replace(',', '.')
        elif ',' in s:
            s = s.replace(',', '.')
        try:
            return float(s)
        except ValueError:
            return None


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
