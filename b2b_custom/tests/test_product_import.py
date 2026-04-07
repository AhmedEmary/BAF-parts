import base64
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError

@tagged('post_install', '-at_install')
class TestMassProductImport(TransactionCase):

    def setUp(self):
        super(TestMassProductImport, self).setUp()
        # Setup basic data
        self.uom_unit = self.env.ref('uom.product_uom_unit')

        # Create a sample CSV: SKU, Brand, Name, Price
        csv_content = "sku,brand,product name,price\nSKU123,BOSCH,Drill Machine,150.00"
        self.csv_base64 = base64.b64encode(csv_content.encode('utf-8'))

    def test_01_flow_and_mapping(self):
        """ Test the wizard flow: Upload -> Mapping -> Import """

        # 1. Initialize Wizard
        wizard = self.env['mass.product.import'].create({
            'file_data': self.csv_base64,
            'file_name': 'test_import.csv',
        })
        self.assertEqual(wizard.state, 'upload')

        # 2. Read Headers (State should change to mapping)
        wizard.action_read_headers()
        self.assertEqual(wizard.state, 'mapping')
        self.assertTrue(len(wizard.mapping_ids) > 0, "Mappings should have been generated")

        # 3. Check Fuzzy Mapping
        sku_mapping = wizard.mapping_ids.filtered(lambda m: m.file_column_name == 'sku')
        self.assertEqual(sku_mapping.field_name, 'sku', "Fuzzy map failed to identify SKU")

        # 4. Execute Import
        wizard.action_import_direct()

        # 5. Verify Database State
        # Expected default_code = BOS_SKU123 (based on your _compute_default_code logic)
        product = self.env['product.template'].search([('default_code', '=', 'BOS_SKU123')])
        self.assertTrue(product.exists(), "Product was not created via SQL import")
        self.assertEqual(product.list_price, 150.00)
        self.assertEqual(product.sku, 'SKU123')

    def test_02_validation_errors(self):
        """ Ensure it raises error if required fields are missing """
        wizard = self.env['mass.product.import'].create({
            'file_data': self.csv_base64,
            'file_name': 'test.csv',
        })
        wizard.action_read_headers()

        # Manually clear the SKU mapping to trigger error
        wizard.mapping_ids.filtered(lambda m: m.field_name == 'sku').write({'field_name': False})

        with self.assertRaises(UserError):
            wizard.action_import_direct()

    def test_03_helper_methods(self):
        """ Test the logic of ID generation and barcode formatting """
        wizard = self.env['mass.product.import'].new()

        # Test default code logic
        code = wizard._compute_default_code('Apple', 'iPhone15')
        self.assertEqual(code, 'APP_iPhone15')

        barcode = wizard._compute_barcode_from_code('FER_123')
        self.assertEqual(barcode, '000000123')

    def test_04_all_fields_import(self):
        """ Verify every mapped field is correctly written to the database via SQL """

        # 1. Prepare a CSV with all supported fields
        # Headers match the FUZZY_MAP keys for auto-guessing
        headers = "sku,brand,name,price,uos,weight,surcharge,hs_code,discount code 1,discount code 2,origin"
        row_data = "EXT-999,BOSCH,Professional Drill,550.50,10,2.5,15.00,8467.21,REBATE20,WINTER10,BE"
        csv_content = f"{headers}\n{row_data}"
        file_base64 = base64.b64encode(csv_content.encode('utf-8'))

        # 2. Create Wizard and Run Header Mapping
        wizard = self.env['mass.product.import'].create({
            'file_data': file_base64,
            'file_name': 'full_import.csv',
        })
        wizard.action_read_headers()

        # Verify all 11 fields were mapped (based on your selection list)
        mapped_fields = wizard.mapping_ids.filtered(lambda m: m.field_name)
        self.assertEqual(len(mapped_fields), 11, "Not all fields were auto-mapped by FUZZY_MAP")

        # 3. Trigger the Direct SQL Import
        wizard.action_import_direct()

        # 4. Verify the Product Template
        # default_code logic: BOS_EXT-999
        product = self.env['product.template'].search([('default_code', '=', 'BOS_EXT-999')])

        self.assertTrue(product.exists(), "Product was not created")

        # Check standard fields
        self.assertEqual(product.name, "Professional Drill")
        self.assertEqual(product.list_price, 550.50)
        self.assertEqual(product.weight, 2.5)
        self.assertEqual(product.surcharge, 15.00)
        self.assertEqual(product.hs_code, "8467.21")
        self.assertEqual(product.unit_of_sales, 10)

        # Check Relational fields (Brand)
        self.assertEqual(product.brand.name, "BOSCH")

        # Check Relational fields (Country/Origin)
        belgium = self.env.ref('base.be')
        self.assertEqual(product.origin.id, belgium.id)

        # Check Discount Codes (Logic: BrandPrefix_Code)
        self.assertEqual(product.disc_code_1.name, "BOS_REBATE20")
        self.assertEqual(product.disc_code_2.name, "BOS_WINTER10")

        # Check Barcode Logic (BOS prefix doesn't pad, just returns SKU part)
        # default_code is 'BOS_EXT-999', so barcode should be 'EXT-999'
        product_variant = self.env['product.product'].search([('product_tmpl_id', '=', product.id)])
        self.assertEqual(product_variant.barcode, "EXT-999")