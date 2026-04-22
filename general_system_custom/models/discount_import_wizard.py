import base64
import io
import openpyxl
import xlrd
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class BafDiscountImportWizard(models.TransientModel):
    _name = 'baf.discount.import.wizard'
    _description = 'Import Matrix Discount Tables'

    file_data = fields.Binary('File')
    file_name = fields.Char('File Name')

    format_type = fields.Selection([
        ('full_template', 'Full Master Template (all brands)'),
        ('bmw_mini', 'BMW & MINI Matrix'),
        ('jlr', 'Land Rover & Jaguar Matrix'),
        ('mercedes', 'Mercedes Matrix'),
    ], string="File Format", required=True, default='full_template')

    # ─── Sheet names in the master template ─────────────────────────────
    _SHEET_BMW_MINI = 'BMW-MINI-MOTORRAD'
    _SHEET_JLR = 'JLR'
    _SHEET_MERCEDES = 'MERCEDES'

    # ─── BMW / MINI sheet column layout ─────────────────────────────────
    # Row 0 : section header (PURCHAGES BMW-MINI | Purchages Motocycle |
    #                         SALE PRICE GR1 | GR2 | GR3 | GR4 | GR_MOTORCYCLE)
    # Row 1 : BMW TA1-2 | BMW TA 3-9 | MINI TA 1-2 | MINI TA 3-9 (per section)
    # Row 2 : DC | Supplier1 | Supplier2 | ...   (purchase side only)
    # Row 3+: data rows, col 0 = discount code, then values
    _BMW_MINI_PURCHASE_COLS = [
        (1,  'SUP1_BMW_T12'),
        (2,  'SUP2_BMW_T12'),
        (3,  'SUP1_BMW_T39'),
        (4,  'SUP2_BMW_T39'),
        (5,  'SUP1_MINI_T12'),
        (6,  'SUP2_MINI_T12'),
        (7,  'SUP1_MINI_T39'),
        (8,  'SUP2_MINI_T39'),
        (9,  'SUP3_MOTO'),
    ]
    _BMW_MINI_SALES_SECTIONS = [
        (10, 'GR1'),
        (14, 'GR2'),
        (18, 'GR3'),
        (22, 'GR4'),
        (26, 'MOTO'),
    ]
    _BMW_MINI_SALES_SUBCOLS = ('BMW_T12', 'BMW_T39', 'MINI_T12', 'MINI_T39')

    # ─── JLR sheet layout ───────────────────────────────────────────────
    # Row 0 : DC | GR8 | GR7 | GR6 | GR5 | GR4 | GR3 | GR2 | GR1
    # GR4 doubles as the purchase column AND a sales tier; its percentage
    # is written to both tables so JLR_GR4 is usable as a customer group too.
    _JLR_COLS = [
        # (col_idx, table_type, column_key, group_suffix-or-None)
        (1, 'sales',    'JLR_GR8', 'GR8'),
        (2, 'sales',    'JLR_GR7', 'GR7'),
        (3, 'sales',    'JLR_GR6', 'GR6'),
        (4, 'sales',    'JLR_GR5', 'GR5'),
        (5, 'purchase', 'JLR_GR4', None),
        (5, 'sales',    'JLR_GR4', 'GR4'),
        (6, 'sales',    'JLR_GR3', 'GR3'),
        (7, 'sales',    'JLR_GR2', 'GR2'),
        (8, 'sales',    'JLR_GR1', 'GR1'),
    ]

    # ─── MERCEDES sheet layout ──────────────────────────────────────────
    # Row 0 : DC | Purchages | SALES GR1
    _MERCEDES_COLS = [
        (1, 'purchase', 'MERCEDES_PURCHASE', None),
        (2, 'sales',    'MERCEDES_GR1',      'GR1'),
    ]

    # ────────────────────────────────────────────────────────────────
    # Action entry points
    # ────────────────────────────────────────────────────────────────

    def action_import(self):
        if not self.file_data:
            raise UserError(_("Please upload a file."))

        sheets = self._read_workbook(self.file_name or '', self.file_data)

        created = 0
        updated = 0
        groups_created = 0

        if self.format_type == 'full_template':
            for sheet_name, importer in (
                (self._SHEET_BMW_MINI, self._import_bmw_mini),
                (self._SHEET_JLR,      self._import_jlr),
                (self._SHEET_MERCEDES, self._import_mercedes),
            ):
                rows = self._pick_sheet(sheets, sheet_name, required=False)
                if rows is None:
                    continue
                c, u, g = importer(rows)
                created += c
                updated += u
                groups_created += g
        elif self.format_type == 'bmw_mini':
            rows = self._pick_sheet(sheets, self._SHEET_BMW_MINI, required=True)
            created, updated, groups_created = self._import_bmw_mini(rows)
        elif self.format_type == 'jlr':
            rows = self._pick_sheet(sheets, self._SHEET_JLR, required=True)
            created, updated, groups_created = self._import_jlr(rows)
        elif self.format_type == 'mercedes':
            rows = self._pick_sheet(sheets, self._SHEET_MERCEDES, required=True)
            created, updated, groups_created = self._import_mercedes(rows)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Discount Matrix Imported"),
                'message': _(
                    "%(c)d lines created, %(u)d updated, %(g)d groups created."
                ) % {'c': created, 'u': updated, 'g': groups_created},
                'type': 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def action_download_template(self):
        """Return an action that downloads the master Excel template."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/general_system_custom/discount_matrix_template',
            'target': 'self',
        }

    # ────────────────────────────────────────────────────────────────
    # File reading helpers
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _clean(cell):
        if cell is None:
            return ""
        if isinstance(cell, float) and cell.is_integer():
            return str(int(cell))
        return str(cell).strip()

    def _read_workbook(self, file_name, file_data):
        """
        Parse the uploaded file into a dict {sheet_name: [[cell, ...], ...]}.
        For CSV (single sheet), returns {'': rows}.
        """
        file_name = file_name.lower()
        file_content = base64.b64decode(file_data)
        sheets = {}

        try:
            if file_name.endswith('.xlsx'):
                wb = openpyxl.load_workbook(filename=io.BytesIO(file_content), data_only=True)
                for sn in wb.sheetnames:
                    s = wb[sn]
                    sheets[sn] = [
                        [self._clean(c) for c in row]
                        for row in s.iter_rows(values_only=True)
                    ]
            elif file_name.endswith('.xls'):
                wb = xlrd.open_workbook(file_contents=file_content)
                for sn in wb.sheet_names():
                    s = wb.sheet_by_name(sn)
                    sheets[sn] = [
                        [self._clean(c) for c in s.row_values(r)]
                        for r in range(s.nrows)
                    ]
            else:
                import csv
                csv_data = file_content.decode('utf-8-sig')
                sheets[''] = list(csv.reader(io.StringIO(csv_data), delimiter=','))
        except Exception as e:
            raise UserError(
                _("Error reading file. Please ensure it is a valid CSV or Excel format. Details: %s")
                % str(e)
            )
        return sheets

    def _pick_sheet(self, sheets, name, required=True):
        """
        Locate a sheet by exact name, then case-insensitive match.
        For single-sheet imports, fall back to the first sheet if there's only one.
        """
        if name in sheets:
            return sheets[name]
        for sn in sheets:
            if sn.strip().lower() == name.strip().lower():
                return sheets[sn]
        if len(sheets) == 1:
            return next(iter(sheets.values()))
        if required:
            raise UserError(_("Sheet '%s' not found in the uploaded file.") % name)
        return None

    # ────────────────────────────────────────────────────────────────
    # Helpers shared by importers
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_float(raw):
        s = str(raw).strip()
        if not s:
            return None
        try:
            return float(s.replace(',', '.'))
        except ValueError:
            return None

    def _upsert_line(self, table_type, column_key, discount_code, pct, counters):
        DiscountLine = self.env['baf.discount.line']
        existing = DiscountLine.search([
            ('table_type', '=', table_type),
            ('column_key', '=', column_key),
            ('discount_code', '=', discount_code),
        ], limit=1)
        if existing:
            if existing.discount_pct != pct:
                existing.discount_pct = pct
            counters['updated'] += 1
        else:
            DiscountLine.create({
                'table_type': table_type,
                'column_key': column_key,
                'discount_code': discount_code,
                'discount_pct': pct,
            })
            counters['created'] += 1

    def _ensure_group(self, name, suffix, counters, brand_family='all'):
        """Idempotently create a baf.sales.group with table_lookup pricing,
        tagged with the brand family it serves so the pricing engine can
        pick the right group when a customer holds several."""
        Group = self.env['baf.sales.group']
        existing = Group.search([('name', '=', name)], limit=1)
        if existing:
            vals = {}
            if existing.pricing_method != 'table_lookup':
                vals['pricing_method'] = 'table_lookup'
            if existing.group_column_suffix != suffix:
                vals['group_column_suffix'] = suffix
            if existing.brand_family != brand_family:
                vals['brand_family'] = brand_family
            if vals:
                existing.write(vals)
            return existing
        Group.create({
            'name': name,
            'pricing_method': 'table_lookup',
            'group_column_suffix': suffix,
            'brand_family': brand_family,
        })
        counters['groups_created'] += 1

    # ────────────────────────────────────────────────────────────────
    # BMW / MINI importer
    # ────────────────────────────────────────────────────────────────

    def _import_bmw_mini(self, rows):
        counters = {'created': 0, 'updated': 0, 'groups_created': 0}

        for row in rows:
            if not row:
                continue
            first = str(row[0]).strip()
            if not first.isdigit():
                continue
            discount_code = first  # stored as Char

            for col_idx, column_key in self._BMW_MINI_PURCHASE_COLS:
                if col_idx >= len(row):
                    continue
                pct = self._parse_float(row[col_idx])
                if pct is None:
                    continue
                self._upsert_line('purchase', column_key, discount_code, pct, counters)

            for base_col, suffix in self._BMW_MINI_SALES_SECTIONS:
                for offset, sub in enumerate(self._BMW_MINI_SALES_SUBCOLS):
                    col_idx = base_col + offset
                    if col_idx >= len(row):
                        continue
                    pct = self._parse_float(row[col_idx])
                    if pct is None:
                        continue
                    self._upsert_line('sales', f"{sub}_{suffix}", discount_code, pct, counters)

        # One sales group per section, shared across BMW/MINI/T12/T39.
        for _base_col, suffix in self._BMW_MINI_SALES_SECTIONS:
            self._ensure_group(f"BMW_MINI_{suffix}", suffix, counters, brand_family='bmw_mini')

        return counters['created'], counters['updated'], counters['groups_created']

    # ────────────────────────────────────────────────────────────────
    # JLR importer
    # ────────────────────────────────────────────────────────────────

    def _import_jlr(self, rows):
        counters = {'created': 0, 'updated': 0, 'groups_created': 0}

        for row in rows:
            if not row:
                continue
            dc = str(row[0]).strip()
            if not dc or dc.upper() == 'DC':
                continue

            for col_idx, table_type, column_key, _suffix in self._JLR_COLS:
                if col_idx >= len(row):
                    continue
                pct = self._parse_float(row[col_idx])
                if pct is None:
                    continue
                self._upsert_line(table_type, column_key, dc, pct, counters)

        for _col_idx, table_type, column_key, suffix in self._JLR_COLS:
            if table_type != 'sales':
                continue
            self._ensure_group(column_key, suffix, counters, brand_family='jlr')

        return counters['created'], counters['updated'], counters['groups_created']

    # ────────────────────────────────────────────────────────────────
    # MERCEDES importer
    # ────────────────────────────────────────────────────────────────

    def _import_mercedes(self, rows):
        counters = {'created': 0, 'updated': 0, 'groups_created': 0}

        for row in rows:
            if not row:
                continue
            dc = str(row[0]).strip()
            if not dc or dc.upper() == 'DC':
                continue

            for col_idx, table_type, column_key, _suffix in self._MERCEDES_COLS:
                if col_idx >= len(row):
                    continue
                pct = self._parse_float(row[col_idx])
                if pct is None:
                    continue
                self._upsert_line(table_type, column_key, dc, pct, counters)

        for _col_idx, table_type, column_key, suffix in self._MERCEDES_COLS:
            if table_type != 'sales':
                continue
            self._ensure_group(column_key, suffix, counters, brand_family='mercedes')

        return counters['created'], counters['updated'], counters['groups_created']