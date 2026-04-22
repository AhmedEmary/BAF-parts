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
