import base64
import io
import re

import openpyxl
import xlrd
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class BafDiscountImportWizard(models.TransientModel):
    _name = 'baf.discount.import.wizard'
    _description = 'Import Sales Discount Matrix Tables'

    file_data = fields.Binary('File')
    file_name = fields.Char('File Name')

    # SALES-ONLY matrix import. This wizard loads the global sales discount
    # tiers (customer groups x brand/type columns) and creates the matching
    # baf.sales.group records. Purchase pricing is NOT handled here: it is
    # per-vendor and uploaded inline on each contact
    # (res.partner.action_import_vendor_pricing_file).
    #
    # Group columns are detected from the sheet header, so any number of groups
    # works (2, 5, ...). A group is any column whose header names a group tier:
    #   "SALE PRICE GR1" / "GR8" / "SALES GR3" -> GR1 / GR8 / GR3
    #   "GR_MOTORCYCLE" / anything with MOTO    -> MOTO
    # JLR & Mercedes use one column per group; BMW/MINI uses 4 type sub-columns
    # per group (BMW_T12, BMW_T39, MINI_T12, MINI_T39) starting at the group's
    # header column.
    format_type = fields.Selection([
        ('bmw_mini', 'BMW & MINI Matrix'),
        ('jlr', 'Land Rover & Jaguar Matrix'),
        ('mercedes', 'Mercedes Matrix'),
    ], string="File Format", required=True, default='bmw_mini')

    # ─── Sheet names in the templates ───────────────────────────────────
    _SHEET_BMW_MINI = 'BMW-MINI-MOTORRAD'
    _SHEET_JLR = 'JLR'
    _SHEET_MERCEDES = 'MERCEDES'

    # BMW/MINI: fixed 4 type sub-columns per group (the number of GROUPS is
    # variable; the number of types within a group is not).
    _BMW_MINI_SALES_SUBCOLS = ('BMW_T12', 'BMW_T39', 'MINI_T12', 'MINI_T39')

    # ────────────────────────────────────────────────────────────────
    # Action entry points
    # ────────────────────────────────────────────────────────────────

    def action_import(self):
        if not self.file_data:
            raise UserError(_("Please upload a file."))

        sheets = self._read_workbook(self.file_name or '', self.file_data)

        importer, sheet_name = {
            'bmw_mini': (self._import_bmw_mini, self._SHEET_BMW_MINI),
            'jlr':      (self._import_jlr,      self._SHEET_JLR),
            'mercedes': (self._import_mercedes, self._SHEET_MERCEDES),
        }[self.format_type]
        rows = self._pick_sheet(sheets, sheet_name, required=True)
        created, updated, groups_created = importer(rows)

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
        """Download the Excel template for the selected brand format."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/general_system_custom/discount_matrix_template?format_type=%s'
                   % (self.format_type or 'bmw_mini'),
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

    @staticmethod
    def _group_suffix(cell):
        """Map a header cell to a group suffix, or None if it isn't a group.
        'SALE PRICE GR1'->GR1, 'GR8'->GR8, 'GR_MOTORCYCLE'/'MOTO'->MOTO."""
        text = str(cell or '').strip().upper()
        if not text:
            return None
        if 'MOTO' in text or 'MOTORRAD' in text or 'MOTORCYCLE' in text:
            return 'MOTO'
        m = re.search(r'GR[\s_]*0*(\d+)', text)
        return ('GR' + m.group(1)) if m else None

    def _detect_group_columns(self, rows):
        """Find the header row and its group columns. Returns
        (header_index, [(col_index, suffix), ...]); ([] if none found).
        The header is the first row (col 0 skipped) that names any group."""
        for idx, row in enumerate(rows):
            cols = [
                (c, self._group_suffix(cell))
                for c, cell in enumerate(row)
                if c >= 1 and self._group_suffix(cell)
            ]
            if cols:
                return idx, cols
        return None, []

    def _upsert_line(self, table_type, column_key, discount_code, pct, counters, group=None):
        DiscountLine = self.env['baf.discount.line']
        existing = DiscountLine.search([
            ('table_type', '=', table_type),
            ('column_key', '=', column_key),
            ('discount_code', '=', discount_code),
        ], limit=1)
        if existing:
            vals = {}
            if existing.discount_pct != pct:
                vals['discount_pct'] = pct
            if group and existing.group_id != group:
                vals['group_id'] = group.id
            if vals:
                existing.write(vals)
            counters['updated'] += 1
        else:
            DiscountLine.create({
                'table_type': table_type,
                'column_key': column_key,
                'discount_code': discount_code,
                'discount_pct': pct,
                'group_id': group.id if group else False,
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
        group = Group.create({
            'name': name,
            'pricing_method': 'table_lookup',
            'group_column_suffix': suffix,
            'brand_family': brand_family,
        })
        counters['groups_created'] += 1
        return group

    # ────────────────────────────────────────────────────────────────
    # BMW / MINI importer (variable group count; 4 type sub-cols per group)
    # ────────────────────────────────────────────────────────────────

    def _import_bmw_mini(self, rows):
        counters = {'created': 0, 'updated': 0, 'groups_created': 0}

        header_idx, group_cols = self._detect_group_columns(rows)
        if not group_cols:
            raise UserError(_(
                "No group columns found. The header must name each group, "
                "e.g. 'SALE PRICE GR1', 'GR_MOTORCYCLE'."))

        # One sales group per detected section, shared across BMW/MINI/T12/T39.
        groups = {
            suffix: self._ensure_group(f"BMW_MINI_{suffix}", suffix, counters,
                                       brand_family='bmw_mini')
            for _base_col, suffix in group_cols
        }

        for i, row in enumerate(rows):
            if i == header_idx or not row:
                continue
            code = str(row[0]).strip()
            if not code.isdigit():  # BMW/MINI codes are numeric
                continue
            for base_col, suffix in group_cols:
                for offset, sub in enumerate(self._BMW_MINI_SALES_SUBCOLS):
                    col_idx = base_col + offset
                    if col_idx >= len(row):
                        continue
                    pct = self._parse_float(row[col_idx])
                    if pct is None:
                        continue
                    self._upsert_line('sales', f"{sub}_{suffix}", code, pct,
                                      counters, group=groups[suffix])

        return counters['created'], counters['updated'], counters['groups_created']

    # ────────────────────────────────────────────────────────────────
    # JLR / MERCEDES importers (variable group count; one column per group)
    # ────────────────────────────────────────────────────────────────

    def _import_single_col(self, rows, brand_prefix, brand_family):
        counters = {'created': 0, 'updated': 0, 'groups_created': 0}

        header_idx, group_cols = self._detect_group_columns(rows)
        if not group_cols:
            raise UserError(_(
                "No group columns found. The header must name each group, "
                "e.g. 'GR1', 'SALES GR2'."))

        groups = {
            suffix: self._ensure_group(f"{brand_prefix}_{suffix}", suffix, counters,
                                       brand_family=brand_family)
            for _col_idx, suffix in group_cols
        }

        for i, row in enumerate(rows):
            if i == header_idx or not row:
                continue
            dc = str(row[0]).strip()
            if not dc or dc.upper() == 'DC':
                continue
            for col_idx, suffix in group_cols:
                if col_idx >= len(row):
                    continue
                pct = self._parse_float(row[col_idx])
                if pct is None:
                    continue
                self._upsert_line('sales', f"{brand_prefix}_{suffix}", dc, pct,
                                  counters, group=groups[suffix])

        return counters['created'], counters['updated'], counters['groups_created']

    def _import_jlr(self, rows):
        return self._import_single_col(rows, 'JLR', 'jlr')

    def _import_mercedes(self, rows):
        return self._import_single_col(rows, 'MERCEDES', 'mercedes')

