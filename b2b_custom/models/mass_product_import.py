import base64
import io
import csv
import json
import logging
import time
from psycopg2.extras import execute_values
from odoo import models, fields, api, _
from odoo.exceptions import UserError

try:
    import openpyxl
except ImportError:
    openpyxl = None

_logger = logging.getLogger(__name__)

# Dictionary to auto-guess the fields based on file headers
FUZZY_MAP = {
    'sku': ['sku', 'internal reference', 'default_code', 'part number'],
    'brand': ['brand', 'make', 'manufacturer'],
    'name': ['name', 'product name', 'description', 'title'],
    'price': ['price', 'sales price', 'list_price', 'retail'],
    'uos': ['unit of sales', 'unit of sale', 'uos', 'moq'],
    'dc1': ['disc code 1', 'discount code 1', 'discount 1', 'disc_code_1'],
    'dc2': ['disc code 2', 'discount code 2', 'discount 2', 'disc_code_2'],
    'origin': ['origin', 'origine', 'country', 'country_code', 'origin_country', 'coo'],
    'hs_code': ['hs code', 'hscode', 'hs_code', 'tariff'],
    'surcharge': ['surcharge', 'fee', 'core charge'],
    'weight': ['weight', 'kg', 'mass']
}

class MassProductImport(models.TransientModel):
    _name = 'mass.product.import'
    _description = 'Mass Product Import via Direct SQL'

    state = fields.Selection([
        ('upload', 'Upload File'),
        ('mapping', 'Map Columns')
    ], string='Status', default='upload')

    file_data = fields.Binary('Excel/CSV File', required=True)
    file_name = fields.Char('File Name')

    mapping_ids = fields.One2many('mass.product.import.mapping', 'import_id', string='Column Mappings')

    def action_read_headers(self):
        """ Step 1: Reads the file headers and prepares the mapping grid """
        self.ensure_one()

        if not self.file_name or not self.file_name.lower().endswith(('.xlsx', '.csv')):
            raise UserError(_("Unsupported file format. Please upload a .csv or .xlsx file."))

        headers = self._get_file_headers()

        # Build mapping lines
        mapping_lines = []
        for index, header in enumerate(headers):
            clean_header = str(header).lower().strip()

            # Auto-guess the field
            guessed_field = False
            for field_key, fuzzy_list in FUZZY_MAP.items():
                if clean_header in fuzzy_list:
                    guessed_field = field_key
                    break

            mapping_lines.append((0, 0, {
                'column_index': index,
                'file_column_name': str(header),
                'field_name': guessed_field
            }))

        self.mapping_ids = False # Clear old mappings
        self.mapping_ids = mapping_lines
        self.state = 'mapping'

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mass.product.import',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_reset(self):
        """ Go back to upload step """
        self.state = 'upload'
        self.mapping_ids = False
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mass.product.import',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_import_direct(self):
        """ Step 2: Processes the file using the user-defined mappings """
        self.ensure_one()

        # Verify required fields are mapped
        mapped_fields = [line.field_name for line in self.mapping_ids if line.field_name]
        if 'sku' not in mapped_fields or 'brand' not in mapped_fields:
            raise UserError(_("You must map both 'SKU' and 'Brand' columns to proceed!"))

        attachment = self.env['ir.attachment'].create({
            'name': self.file_name,
            'type': 'binary',
            'datas': self.file_data,
            'res_model': 'mass.product.import',
            'res_id': self.id,
        })

        self._process_direct_sql(attachment.id)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Import Completed',
                'message': 'Your products were successfully imported!',
                'type': 'success',
                'sticky': False,
            }
        }

    def _get_file_headers(self):
        file_name_lower = self.file_name.lower()
        if file_name_lower.endswith('.csv'):
            try:
                file_content = base64.b64decode(self.file_data).decode('utf-8-sig')
            except UnicodeDecodeError:
                file_content = base64.b64decode(self.file_data).decode('ISO-8859-1')
            csv_reader = csv.reader(io.StringIO(file_content))
            return next(csv_reader)
        else:
            file_content = io.BytesIO(base64.b64decode(self.file_data))
            wb = openpyxl.load_workbook(filename=file_content, read_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            return next(rows_iter)

    def _compute_default_code(self, brand_name, sku):
        if not sku or not brand_name: return None
        brand_name = str(brand_name).strip()
        sku = str(sku).strip()
        if len(brand_name) >= 3:
            return f"{brand_name[:3].upper()}_{sku}"
        return f"{brand_name.upper()}_{sku}"

    def _compute_discount_code(self, brand_name, raw_dc):
        if not raw_dc or not brand_name: return None
        brand_name = str(brand_name).strip()
        raw_dc = str(raw_dc).strip()
        if len(brand_name) >= 3:
            return f"{brand_name[:3].upper()}_{raw_dc}"
        return f"{brand_name.upper()}_{raw_dc}"

    def _compute_barcode_from_code(self, default_code):
        if not default_code or '_' not in default_code: return None
        parts = default_code.split('_', 1)
        number_part = parts[-1]
        prefix = default_code[:3].upper()
        if prefix in ['MAS', 'FER']:
            return number_part.zfill(9)
        return number_part

    def _process_direct_sql(self, attachment_id):
        total_start_time = time.time()
        attachment = self.env['ir.attachment'].browse(attachment_id)
        if not attachment: return

        # 1. Default required Odoo IDs
        uom_record = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
        uom_id = uom_record.id if uom_record else self.env['uom.uom'].search([], limit=1).id

        categ_record = self.env['product.category'].search([('name', '=ilike', 'Goods')], limit=1)
        if not categ_record:
            categ_record = self.env['product.category'].create({'name': 'Goods'})
        categ_id = categ_record.id

        if not uom_id or not categ_id:
            raise UserError("Missing Category or Unit of Measure in the database!")

        # 2. Caches
        brand_cache = {b.name.strip().lower(): b.id for b in self.env['product.brand'].search([]) if b.name}
        dc_cache = {d.name.strip().lower(): d.id for d in self.env['discount.code'].search([]) if d.name}
        country_cache = {c.code.upper(): c.id for c in self.env['res.country'].search([]) if c.code}

        # 3. Read File Rows
        file_name_lower = self.file_name.lower()
        if file_name_lower.endswith('.csv'):
            try:
                file_content = base64.b64decode(attachment.datas).decode('utf-8-sig')
            except UnicodeDecodeError:
                file_content = base64.b64decode(attachment.datas).decode('ISO-8859-1')
            csv_reader = csv.reader(io.StringIO(file_content))
            next(csv_reader) # Skip header
            rows = csv_reader
        else:
            file_content = io.BytesIO(base64.b64decode(attachment.datas))
            wb = openpyxl.load_workbook(filename=file_content, read_only=True)
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            next(rows_iter) # Skip header
            rows = rows_iter

        # 4. Extract indices from user mapping
        col_map = {line.field_name: line.column_index for line in self.mapping_ids if line.field_name}

        sku_idx = col_map.get('sku')
        brand_idx = col_map.get('brand')
        name_idx = col_map.get('name')
        price_idx = col_map.get('price')
        uos_idx = col_map.get('uos')
        dc1_idx = col_map.get('dc1')
        dc2_idx = col_map.get('dc2')
        origin_idx = col_map.get('origin')
        hs_code_idx = col_map.get('hs_code')
        surcharge_idx = col_map.get('surcharge')
        weight_idx = col_map.get('weight')

        batch_size = 1000
        data_batch_dict = {}
        batch_counter = 0

        # 5. SQL Query
        upsert_template_query = """
            INSERT INTO product_template (
                name, default_code, sku, brand, unit_of_sales, list_price, 
                type, is_storable, uom_id, categ_id, active, service_tracking, tracking, publish_date, base_unit_count,
                sale_ok, purchase_ok, disc_code_1, disc_code_2, origin, hs_code, surcharge, weight, invoice_policy
            ) VALUES %s
            ON CONFLICT (default_code) DO UPDATE SET
                name = EXCLUDED.name,
                list_price = EXCLUDED.list_price,
                sku = EXCLUDED.sku,
                brand = EXCLUDED.brand,
                unit_of_sales = EXCLUDED.unit_of_sales,
                sale_ok = EXCLUDED.sale_ok,
                purchase_ok = EXCLUDED.purchase_ok,
                disc_code_1 = EXCLUDED.disc_code_1,
                disc_code_2 = EXCLUDED.disc_code_2,
                origin = EXCLUDED.origin,
                hs_code = EXCLUDED.hs_code,
                surcharge = EXCLUDED.surcharge,
                weight = EXCLUDED.weight,
                invoice_policy = EXCLUDED.invoice_policy
            RETURNING id, default_code;
        """

        def get_val(row_data, idx, default=''):
            if idx is not None and len(row_data) > idx and row_data[idx] not in (None, ''):
                return str(row_data[idx]).strip()
            return default

        def safe_float(val_str):
            if not val_str: return None
            try: return float(val_str.replace(',', ''))
            except ValueError: return None

        def safe_int(val_str):
            if not val_str: return None
            try: return int(float(val_str.replace(',', '')))
            except ValueError: return None

        for row in rows:
            if not row or len(row) <= max(sku_idx, brand_idx) or not row[sku_idx] or not row[brand_idx]:
                continue

            sku = get_val(row, sku_idx)
            brand_name = get_val(row, brand_idx)

            if not sku or not brand_name:
                continue

            origin_input = get_val(row, origin_idx)
            origin_id = None
            if origin_input:
                country_code = origin_input.strip().upper()
                if country_code in country_cache:
                    origin_id = country_cache[country_code]
                else:
                    target_country = self.env['res.country'].search([('code', '=', country_code)], limit=1)
                    if target_country:
                        origin_id = target_country.id
                        country_cache[country_code] = origin_id

            brand_key = brand_name.lower()
            if brand_key not in brand_cache:
                new_brand = self.env['product.brand'].create({'name': brand_name})
                brand_cache[brand_key] = new_brand.id
            brand_id = brand_cache[brand_key]

            raw_name = get_val(row, name_idx, f"{brand_name} {sku}")
            name_json = json.dumps({"en_US": raw_name})

            # Cleaned Numerics
            price = safe_float(get_val(row, price_idx))
            uos = safe_int(get_val(row, uos_idx))
            surcharge = safe_float(get_val(row, surcharge_idx))
            weight = safe_float(get_val(row, weight_idx))

            # Discount Codes Cache
            def get_dc_id(dc_name):
                if not dc_name: return None
                dc_key = dc_name.lower()
                if dc_key not in dc_cache:
                    new_dc = self.env['discount.code'].create({'name': dc_name})
                    dc_cache[dc_key] = new_dc.id
                return dc_cache[dc_key]

            dc1_id = get_dc_id(self._compute_discount_code(brand_name, get_val(row, dc1_idx, None)))
            dc2_id = get_dc_id(self._compute_discount_code(brand_name, get_val(row, dc2_idx, None)))
            hs_code = get_val(row, hs_code_idx, None)

            default_code = self._compute_default_code(brand_name, sku)

            data_batch_dict[default_code] = (
                name_json, default_code, sku, brand_id, uos, price,
                'consu', True, uom_id, categ_id, True, 'no', 'none', fields.Datetime.now(), 0.0,
                True, True, dc1_id, dc2_id, origin_id, hs_code, surcharge, weight, 'order'
            )

            if len(data_batch_dict) >= batch_size:
                batch_counter += 1
                data_batch = list(data_batch_dict.values())
                self._execute_sql_batch(upsert_template_query, data_batch, batch_counter)
                data_batch_dict.clear()

        if data_batch_dict:
            batch_counter += 1
            data_batch = list(data_batch_dict.values())
            self._execute_sql_batch(upsert_template_query, data_batch, batch_counter)

        attachment.unlink()
        _logger.info(f"MASS SQL IMPORT: Successfully finished direct SQL import in {(time.time() - total_start_time) / 60.0:.2f} minutes.")

    def _execute_sql_batch(self, query, data_batch, batch_counter):
        batch_start_time = time.time()
        self.env.flush_all()
        cr = self.env.cr
        upserted_templates = execute_values(cr._obj, query, data_batch, fetch=True)
        if upserted_templates:
            template_ids = tuple([row[0] for row in upserted_templates])

            # 2. Insert missing product_product variants
            product_product_query = """
                INSERT INTO product_product (product_tmpl_id, default_code, active, base_unit_count, weight)
                SELECT pt.id, pt.default_code, true, 0.0, pt.weight
                FROM product_template pt
                LEFT JOIN product_product pp ON pp.product_tmpl_id = pt.id
                WHERE pt.id IN %s AND pp.id IS NULL;
            """
            cr.execute(product_product_query, (template_ids,))

            # 3. Batch Update Barcodes onto existing Variant table
            barcode_batch = []
            for tmpl_id, default_code in upserted_templates:
                barcode = self._compute_barcode_from_code(default_code)
                if barcode:
                    barcode_batch.append((barcode, default_code))

            if barcode_batch:
                barcode_update_query = """
                    UPDATE product_product AS pp
                    SET barcode = v.barcode
                    FROM (VALUES %s) AS v(barcode, default_code)
                    WHERE pp.default_code = v.default_code;
                """
                execute_values(cr._obj, barcode_update_query, barcode_batch)


class MassProductImportMapping(models.TransientModel):
    _name = 'mass.product.import.mapping'
    _description = 'Mass Product Import Mapping Line'

    import_id = fields.Many2one('mass.product.import', ondelete='cascade')
    column_index = fields.Integer("Column Position")
    file_column_name = fields.Char("Excel/CSV Header")

    field_name = fields.Selection([
        ('sku', 'SKU (Required)'),
        ('brand', 'Brand (Required)'),
        ('name', 'Product Name'),
        ('price', 'Sales Price'),
        ('uos', 'Unit of Sales'),
        ('weight', 'Weight'),
        ('surcharge', 'Surcharge'),
        ('hs_code', 'HS Code'),
        ('dc1', 'Discount Code 1'),
        ('dc2', 'Discount Code 2'),
        ('origin', 'Origin (Country Code)')
    ], string="Odoo Field", help="Select the Odoo field this column maps to")
