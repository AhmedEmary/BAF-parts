import base64
import csv
import io
import openpyxl
import xlrd
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class BafDiscountImportWizard(models.TransientModel):
    _name = 'baf.discount.import.wizard'
    _description = 'Import Matrix Discount Tables'

    file_data = fields.Binary('File', required=True)
    file_name = fields.Char('File Name')

    # E.g., GR1, GR2 based on the customer group you are uploading for
    group_suffix = fields.Char('Customer Group Suffix (e.g., GR1)', required=True, default='GR1')

    format_type = fields.Selection([
        ('bmw_mini', 'BMW & MINI Matrix'),
        ('jlr', 'Land Rover & Jaguar Matrix')
    ], string="File Format", required=True, default='bmw_mini')

    def action_import(self):
        if not self.file_data:
            raise UserError(_("Please upload a file."))

        file_name = (self.file_name or '').lower()
        file_content = base64.b64decode(self.file_data)
        lines = []

        # Helper function to clean Excel cells (removes .0 from integers and converts to string)
        def _clean_cell(cell):
            if cell is None:
                return ""
            if isinstance(cell, float) and cell.is_integer():
                return str(int(cell))
            return str(cell).strip()

        try:
            # 1. Handle .xlsx files
            if file_name.endswith('.xlsx'):
                wb = openpyxl.load_workbook(filename=io.BytesIO(file_content), data_only=True)
                sheet = wb.active
                for row in sheet.iter_rows(values_only=True):
                    lines.append([_clean_cell(cell) for cell in row])

            # 2. Handle older .xls files
            elif file_name.endswith('.xls'):
                wb = xlrd.open_workbook(file_contents=file_content)
                sheet = wb.sheet_by_index(0)
                for row_idx in range(sheet.nrows):
                    row = sheet.row_values(row_idx)
                    lines.append([_clean_cell(cell) for cell in row])

            # 3. Default to handling .csv files
            else:
                csv_data = file_content.decode('utf-8-sig')
                reader = csv.reader(io.StringIO(csv_data), delimiter=',')
                lines = list(reader)

        except Exception as e:
            raise UserError(_("Error reading file. Please ensure it is a valid CSV or Excel format. Details: %s") % str(e))

        # Route the parsed lines to the correct function based on the selected format
        if self.format_type == 'bmw_mini':
            self._import_bmw_mini(lines)
        elif self.format_type == 'jlr':
            self._import_jlr(lines)

        return {'type': 'ir.actions.act_window_close'}

    def _import_bmw_mini(self, lines):
        DiscountLine = self.env['baf.discount.line']

        # We will loop through looking for integer codes in the first column
        for row in lines:
            if not row or not str(row[0]).strip().isdigit():
                continue # Skip header rows or empty rows

            try:
                discount_code = int(str(row[0]).strip())
                # The columns in your file: #, BMW 1-2, BMW 3-9, MINI 1-2, MINI 3-9
                bmw_12 = float(str(row[1]).strip() or 0.0)
                bmw_39 = float(str(row[2]).strip() or 0.0)
                mini_12 = float(str(row[3]).strip() or 0.0)
                mini_39 = float(str(row[4]).strip() or 0.0)
            except (ValueError, IndexError):
                continue

            # Helper function to create or update the discount lines
            def upsert_line(col_key, pct):
                domain = [('table_type', '=', 'sales'), ('column_key', '=', col_key), ('discount_code', '=', discount_code)]
                existing = DiscountLine.search(domain, limit=1)
                if existing:
                    # CHANGED HERE: discount_percentage -> discount_pct
                    existing.write({'discount_pct': pct})
                else:
                    DiscountLine.create({
                        'table_type': 'sales',
                        'column_key': col_key,
                        'discount_code': discount_code,
                        # CHANGED HERE: discount_percentage -> discount_pct
                        'discount_pct': pct
                    })

            # Map the columns directly to your BAF Engine's column keys!
            upsert_line(f"BMW_T12_{self.group_suffix}", bmw_12)
            upsert_line(f"BMW_T39_{self.group_suffix}", bmw_39)
            upsert_line(f"MINI_T12_{self.group_suffix}", mini_12)
            upsert_line(f"MINI_T39_{self.group_suffix}", mini_39)

    def _import_jlr(self, lines):
        # Implement similar logic for the JLR CSV/Excel mapping columns to JLR keys
        pass