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

FUZZY_MAP = {
    'sku':           ['sku', 'internal reference', 'default_code', 'part number', 'oem'],
    'brand':         ['brand', 'make', 'manufacturer'],
    'name':          ['name', 'product name', 'description', 'title'],
    'price':         ['price', 'sales price', 'list_price', 'retail', 'upe'],
    'uos':           ['unit of sales', 'unit of sale', 'uos', 'moq', 'min qty'],
    'baf_disc_code': ['discount code', 'disc code', 'discount_code', 'baf_discount_code', 'rabattcode'],
    'baf_type_code': ['type code', 'type_code', 'baf_type_code', 'type'],
    'baf_mod':       ['mod', 'baf_mod', 'motorrad', 'motorcycle'],
    'supplier_route':['supplier route', 'supplier_route', 'route'],
    'origin':        ['origin', 'origine', 'country', 'country_code', 'origin_country', 'coo'],
    'hs_code':       ['hs code', 'hscode', 'hs_code', 'tariff'],
    'surcharge':     ['surcharge', 'fee', 'core charge'],
    'weight':        ['weight', 'kg', 'mass'],
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
        self.ensure_one()
        if not self.file_name or not self.file_name.lower().endswith(('.xlsx', '.csv')):
            raise UserError(_("Unsupported file format. Please upload a .csv or .xlsx file."))

        headers = self._get_file_headers()
        mapping_lines = []
        for index, header in enumerate(headers):
            clean_header = str(header).lower().strip()
            guessed_field = False
            for field_key, fuzzy_list in FUZZY_MAP.items():
                if clean_header in fuzzy_list:
                    guessed_field = field_key
                    break
            mapping_lines.append((0, 0, {
                'column_index': index,
                'file_column_name': str(header),
                'field_name': guessed_field,
            }))

        self.mapping_ids = False
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
        self.ensure_one()
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
            return next(csv.reader(io.StringIO(file_content)))
        else:
            file_content = io.BytesIO(base64.b64decode(self.file_data))
            wb = openpyxl.load_workbook(filename=file_content, read_only=True)
            return next(wb.active.iter_rows(values_only=True))

    def _compute_default_code(self, brand_name, sku):
        if not sku or not brand_name:
            return None
        brand_name = str(brand_name).strip()
        sku = str(sku).strip()
        prefix = brand_name[:3].upper() if len(brand_name) >= 3 else brand_name.upper()
        return f"{prefix}_{sku}"

    def _compute_barcode_from_code(self, default_code):
        if not default_code or '_' not in default_code:
            return None
        number_part = default_code.split('_', 1)[-1]
        prefix = default_code[:3].upper()
        return number_part.zfill(9) if prefix in ['MAS', 'FER'] else number_part

    def _compute_raw_column_key(self, brand_name, type_code, mod):
        brand_name = str(brand_name or '').upper().strip()
        if mod == 'motorcycle':
            return 'MOTO'
        if brand_name not in ['BMW', 'MINI']:
            return ''

        type_code = type_code or 0
        if type_code in (1, 2):
            type_bucket = 'T12'
        elif type_code >= 3:
            type_bucket = 'T39'
        else:
            type_bucket = 'T12' # Default fallback

        return f"{brand_name}_{type_bucket}"

    def _process_direct_sql(self, attachment_id):
        total_start_time = time.time()
        attachment = self.env['ir.attachment'].browse(attachment_id)
        if not attachment:
            return

        uom_record = self.env.ref('uom.product_uom_unit', raise_if_not_found=False)
        uom_id = uom_record.id if uom_record else self.env['uom.uom'].search([], limit=1).id

        categ_record = self.env['product.category'].search([('name', '=ilike', 'Goods')], limit=1)
        if not categ_record:
            categ_record = self.env['product.category'].create({'name': 'Goods'})
        categ_id = categ_record.id

        if not uom_id or not categ_id:
            raise UserError("Missing Category or Unit of Measure in the database!")

        brand_cache = {b.name.strip().lower(): b.id for b in self.env['product.brand'].search([]) if b.name}
        country_cache = {c.code.upper(): c.id for c in self.env['res.country'].search([]) if c.code}

        # Read rows
        file_name_lower = self.file_name.lower()
        if file_name_lower.endswith('.csv'):
            try:
                file_content = base64.b64decode(attachment.datas).decode('utf-8-sig')
            except UnicodeDecodeError:
                file_content = base64.b64decode(attachment.datas).decode('ISO-8859-1')
            csv_reader = csv.reader(io.StringIO(file_content))
            next(csv_reader)
            rows = csv_reader
        else:
            file_content = io.BytesIO(base64.b64decode(attachment.datas))
            wb = openpyxl.load_workbook(filename=file_content, read_only=True)
            rows_iter = wb.active.iter_rows(values_only=True)
            next(rows_iter)
            rows = rows_iter

        col_map = {line.field_name: line.column_index for line in self.mapping_ids if line.field_name}

        sku_idx          = col_map.get('sku')
        brand_idx        = col_map.get('brand')
        name_idx         = col_map.get('name')
        price_idx        = col_map.get('price')
        uos_idx          = col_map.get('uos')
        baf_disc_idx     = col_map.get('baf_disc_code')
        baf_type_idx     = col_map.get('baf_type_code')
        baf_mod_idx      = col_map.get('baf_mod')
        route_idx        = col_map.get('supplier_route')
        origin_idx       = col_map.get('origin')
        hs_code_idx      = col_map.get('hs_code')
        surcharge_idx    = col_map.get('surcharge')
        weight_idx       = col_map.get('weight')

        upsert_query = """
                    INSERT INTO product_template (
                        name, default_code, sku, brand, list_price,
                        type, is_storable, uom_id, categ_id, active, service_tracking,
                        tracking, base_unit_count,
                        sale_ok, purchase_ok,
                        baf_discount_code, baf_type_code, baf_mod, supplier_route,
                        baf_column_key,
                        origin, hs_code, surcharge, weight, invoice_policy, publish_date
                    ) VALUES %s
                    ON CONFLICT (default_code) DO UPDATE SET
                        name             = EXCLUDED.name,
                        list_price       = EXCLUDED.list_price,
                        sku              = EXCLUDED.sku,
                        brand            = EXCLUDED.brand,
                        baf_discount_code= EXCLUDED.baf_discount_code,
                        baf_type_code    = EXCLUDED.baf_type_code,
                        baf_mod          = EXCLUDED.baf_mod,
                        supplier_route   = EXCLUDED.supplier_route,
                        baf_column_key   = EXCLUDED.baf_column_key,
                        origin           = EXCLUDED.origin,
                        hs_code          = EXCLUDED.hs_code,
                        surcharge        = EXCLUDED.surcharge,
                        weight           = EXCLUDED.weight,
                        invoice_policy   = EXCLUDED.invoice_policy,
                        publish_date     = EXCLUDED.publish_date
                    RETURNING id, default_code;
                """
        def get_val(row_data, idx, default=''):
            if idx is not None and len(row_data) > idx and row_data[idx] not in (None, ''):
                return str(row_data[idx]).strip()
            return default

        def safe_float(val_str):
            if not val_str:
                return None
            try:
                return float(str(val_str).replace(',', ''))
            except ValueError:
                return None

        def safe_int(val_str):
            if not val_str:
                return None
            try:
                return int(float(str(val_str).replace(',', '')))
            except ValueError:
                return None

        batch_size = 1000
        data_batch_dict = {}
        batch_counter = 0

        for row in rows:
            if not row or not row[sku_idx] or not row[brand_idx]:
                continue

            sku = get_val(row, sku_idx)
            brand_name = get_val(row, brand_idx)
            if not sku or not brand_name:
                continue

            # Brand
            brand_key = brand_name.lower()
            if brand_key not in brand_cache:
                new_brand = self.env['product.brand'].create({'name': brand_name})
                brand_cache[brand_key] = new_brand.id
            brand_id = brand_cache[brand_key]

            # Origin
            origin_id = None
            origin_input = get_val(row, origin_idx)
            if origin_input:
                country_code = origin_input.strip().upper()
                if country_code in country_cache:
                    origin_id = country_cache[country_code]
                else:
                    target_country = self.env['res.country'].search([('code', '=', country_code)], limit=1)
                    if target_country:
                        origin_id = target_country.id
                        country_cache[country_code] = origin_id

            raw_name = get_val(row, name_idx, f"{brand_name} {sku}")
            name_json = json.dumps({"en_US": raw_name})

            price      = safe_float(get_val(row, price_idx))
            uos        = safe_int(get_val(row, uos_idx))
            surcharge  = safe_float(get_val(row, surcharge_idx)) or 0.0
            weight     = safe_float(get_val(row, weight_idx)) or 0.0
            hs_code    = get_val(row, hs_code_idx, None)

            # BAF pricing fields
            baf_disc   = safe_int(get_val(row, baf_disc_idx)) or 0
            baf_type   = safe_int(get_val(row, baf_type_idx)) or 0
            baf_mod    = get_val(row, baf_mod_idx, 'car').lower() or 'car'
            # Normalise mod value to match selection
            if baf_mod in ('motorrad', 'motorcycle', 'moto'):
                baf_mod = 'motorcycle'
            elif baf_mod == 'sb':
                baf_mod = 'sb'
            else:
                baf_mod = 'car'

            route = get_val(row, route_idx, 'de_table').lower() or 'de_table'
            if route not in ('de_table', 'lr_level', 'de_table'):
                route = 'de_table'

            default_code = self._compute_default_code(brand_name, sku)

            # 1. Calculate the Column Key manually for SQL
            computed_col_key = self._compute_raw_column_key(brand_name, baf_type, baf_mod)

            # 2. EXACTLY 26 columns to match the SQL query
            data_batch_dict[default_code] = (
                name_json,             # 1. name
                default_code,          # 2. default_code
                sku,                   # 3. sku
                brand_id,              # 4. brand
                price,                 # 5. list_price
                'consu',               # 6. type
                True,                  # 7. is_storable
                uom_id,                # 8. uom_id
                categ_id,              # 9. categ_id
                True,                  # 10. active
                'no',                  # 11. service_tracking
                'none',                # 12. tracking
                0.0,                   # 13. base_unit_count
                True,                  # 14. sale_ok
                True,                  # 15. purchase_ok
                baf_disc,              # 16. baf_discount_code
                baf_type,              # 17. baf_type_code
                baf_mod,               # 18. baf_mod
                route,                 # 19. supplier_route
                computed_col_key,      # 20. baf_column_key
                origin_id,             # 21. origin
                hs_code,               # 22. hs_code
                surcharge,             # 23. surcharge
                weight,                # 24. weight
                'order',               # 25. invoice_policy
                fields.Datetime.now(), # 26. publish_date (REQUIRED BY ODOO)
            )

            if len(data_batch_dict) >= batch_size:
                batch_counter += 1
                self._execute_sql_batch(upsert_query, list(data_batch_dict.values()), batch_counter)
                data_batch_dict.clear()

        if data_batch_dict:
            batch_counter += 1
            self._execute_sql_batch(upsert_query, list(data_batch_dict.values()), batch_counter)

        attachment.unlink()
        _logger.info(
            "MASS SQL IMPORT: finished in %.2f minutes.",
            (time.time() - total_start_time) / 60.0,
        )

    def _execute_sql_batch(self, query, data_batch, batch_counter):
        self.env.flush_all()
        cr = self.env.cr
        upserted_templates = execute_values(cr._obj, query, data_batch, fetch=True)
        if upserted_templates:
            template_ids = tuple([row[0] for row in upserted_templates])

            cr.execute("""
                INSERT INTO product_product (product_tmpl_id, default_code, active, base_unit_count, weight)
                SELECT pt.id, pt.default_code, true, 0.0, pt.weight
                FROM product_template pt
                LEFT JOIN product_product pp ON pp.product_tmpl_id = pt.id
                WHERE pt.id IN %s AND pp.id IS NULL;
            """, (template_ids,))

            barcode_batch = [
                (self._compute_barcode_from_code(dc), dc)
                for _, dc in upserted_templates
                if self._compute_barcode_from_code(dc)
            ]
            if barcode_batch:
                execute_values(cr._obj, """
                    UPDATE product_product AS pp
                    SET barcode = v.barcode
                    FROM (VALUES %s) AS v(barcode, default_code)
                    WHERE pp.default_code = v.default_code;
                """, barcode_batch)


class MassProductImportMapping(models.TransientModel):
    _name = 'mass.product.import.mapping'
    _description = 'Mass Product Import Mapping Line'

    import_id = fields.Many2one('mass.product.import', ondelete='cascade')
    column_index = fields.Integer("Column Position")
    file_column_name = fields.Char("Excel/CSV Header")

    field_name = fields.Selection([
        ('sku',           'SKU (Required)'),
        ('brand',         'Brand (Required)'),
        ('name',          'Product Name'),
        ('price',         'Sales Price / UPE'),
        ('uos',           'Unit of Sales'),
        ('weight',        'Weight'),
        ('surcharge',     'Surcharge'),
        ('hs_code',       'HS Code'),
        ('baf_disc_code', 'BAF Discount Code #'),
        ('baf_type_code', 'BAF Type Code (1-9)'),
        ('baf_mod',       'BAF Mod (car / motorcycle / sb)'),
        ('supplier_route','Supplier Route (de_table / lr_level / eu_direct)'),
        ('origin',        'Origin (Country Code)'),
    ], string="Odoo Field")
