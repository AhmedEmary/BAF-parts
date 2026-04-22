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

    def _run_import(self, csv_content, file_name='test_import.csv'):
        wizard = self.env['mass.product.import'].create({
            'file_data': base64.b64encode(csv_content.encode('utf-8')),
            'file_name': file_name,
        })
        wizard.action_read_headers()
        wizard.action_import_direct()
        return wizard

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

    def test_04_replaced_by_creates_same_brand_placeholder(self):
        """Missing replaced_by SKU should create a same-brand placeholder.

        The placeholder must use the same default_code as a later real import,
        so future imports update it instead of creating a duplicate product.
        """
        self._run_import(
            "sku,brand,product name,replaced by\n"
            "OLD001,BOSCH,Old Bosch Part,NEW001"
        )

        old_product = self.env['product.template'].search([('default_code', '=', 'BOS_OLD001')], limit=1)
        replacement = old_product.replaced_by_id
        self.assertTrue(replacement.exists(), "Replacement placeholder should have been created")
        self.assertEqual(replacement.sku, 'NEW001')
        self.assertEqual(replacement.default_code, 'BOS_NEW001')
        self.assertEqual(replacement.brand.name, 'BOSCH')

    def test_05_replaced_by_prefers_same_brand_match(self):
        """If the same replacement SKU exists under multiple brands, use the same-brand product."""
        self.env['product.brand'].create({'name': 'BOSCH'})
        self.env['product.brand'].create({'name': 'VALEO'})

        self._run_import(
            "sku,brand,product name\n"
            "REP001,VALEO,Valeo Replacement\n"
            "REP001,BOSCH,Bosch Replacement\n"
            "OLD002,BOSCH,Old Bosch Part\n",
            file_name='seed_products.csv',
        )
        self._run_import(
            "sku,brand,product name,replaced by\n"
            "OLD003,BOSCH,Another Old Bosch Part,REP001",
            file_name='replaced_by_same_brand.csv',
        )

        old_product = self.env['product.template'].search([('default_code', '=', 'BOS_OLD003')], limit=1)
        self.assertTrue(old_product.replaced_by_id.exists())
        self.assertEqual(old_product.replaced_by_id.default_code, 'BOS_REP001')

    def test_05b_replaced_by_creates_target_before_real_row_arrives(self):
        """If A references B before B's real row appears, B is pre-created and later updated in place."""
        self._run_import(
            "sku,brand,product name,replaced by,price\n"
            "OLDLATE,BOSCH,Old Late Product,NEWLATE,50\n"
            "NEWLATE,BOSCH,Real Replacement Product,,80",
            file_name='same_file_replaced_by_late_target.csv',
        )

        old_product = self.env['product.template'].search([('default_code', '=', 'BOS_OLDLATE')], limit=1)
        replacement_products = self.env['product.template'].search([('default_code', '=', 'BOS_NEWLATE')])

        self.assertEqual(len(replacement_products), 1, "Replacement placeholder must be updated in place, not duplicated")
        self.assertEqual(replacement_products.name, 'Real Replacement Product')
        self.assertEqual(replacement_products.list_price, 80.0)
        self.assertEqual(old_product.replaced_by_id, replacement_products)

    def test_06_import_dimensions_origin_and_normalized_defaults(self):
        """Dimensions/origin import correctly and invalid mod/route fallback safely."""
        self._run_import(
            "sku,brand,product name,price,weight,height,width,length,origin,hs code,surcharge,discount code,type code,mod,supplier route\n"
            "DIM001,BOSCH,#NV,200,5.5,10.1,20.2,30.3,IT,8409,12.5,10,2,invalid_mod,invalid_route"
        )

        product = self.env['product.template'].search([('default_code', '=', 'BOS_DIM001')], limit=1)
        self.assertTrue(product.exists())
        self.assertEqual(product.name, 'BOSCH DIM001')
        self.assertEqual(product.origin.code, 'IT')
        self.assertEqual(product.hs_code, '8409')
        self.assertEqual(product.baf_mod, 'car')
        self.assertEqual(product.supplier_route, 'de_table')
        self.assertAlmostEqual(product.weight, 5.5)
        self.assertAlmostEqual(product.height, 10.1)
        self.assertAlmostEqual(product.width, 20.2)
        self.assertAlmostEqual(product.length, 30.3)
        self.assertAlmostEqual(product.surcharge, 12.5)

    def test_07_import_upserts_existing_product(self):
        """Importing the same brand+SKU twice should update the template, not duplicate it."""
        self._run_import(
            "sku,brand,product name,price,height,width,length\n"
            "UPS001,BOSCH,First Name,100,1,2,3",
            file_name='upsert_first.csv',
        )
        self._run_import(
            "sku,brand,product name,price,height,width,length\n"
            "UPS001,BOSCH,Updated Name,150,11,22,33",
            file_name='upsert_second.csv',
        )

        products = self.env['product.template'].search([('default_code', '=', 'BOS_UPS001')])
        self.assertEqual(len(products), 1)
        self.assertEqual(products.name, 'Updated Name')
        self.assertEqual(products.list_price, 150.0)
        self.assertAlmostEqual(products.height, 11.0)
        self.assertAlmostEqual(products.width, 22.0)
        self.assertAlmostEqual(products.length, 33.0)

    def test_08_replaced_by_ambiguous_global_sku_creates_same_brand_target(self):
        """If another brand already uses the replacement SKU, create/use the same-brand target anyway."""
        self._run_import(
            "sku,brand,product name\n"
            "AMB001,BOSCH,Bosch Replacement\n"
            "AMB001,VALEO,Valeo Replacement",
            file_name='ambiguous_seed.csv',
        )
        self._run_import(
            "sku,brand,product name,replaced by\n"
            "OLD004,JAGUAR,Old Jaguar Part,AMB001",
            file_name='ambiguous_replaced_by.csv',
        )

        old_product = self.env['product.template'].search([('default_code', '=', 'JAG_OLD004')], limit=1)
        replacement = self.env['product.template'].search([('default_code', '=', 'JAG_AMB001')], limit=1)
        self.assertTrue(old_product.exists())
        self.assertTrue(replacement.exists())
        self.assertEqual(replacement.brand.name, 'JAGUAR')
        self.assertEqual(old_product.replaced_by_id, replacement)

