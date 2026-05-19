import base64
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestMassVendorPriceImport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.vendor = self.env['res.partner'].create({
            'name': 'Acme Supplier',
            'supplier_rank': 1,
        })
        # Seed an existing product through the mass product import so that the
        # product.product variant carries the default_code (BOS_SKU123). The
        # vendor price import matches rows against product_product.default_code.
        self._seed_products(
            "sku,brand,product name,price\n"
            "SKU123,BOSCH,Drill Machine,150"
        )
        self.product = self.env['product.template'].search(
            [('default_code', '=', 'BOS_SKU123')], limit=1)
        self.assertTrue(self.product.exists(), "Seed product was not created")

    def _seed_products(self, csv_content, file_name='seed.csv'):
        wizard = self.env['mass.product.import'].create({
            'file_data': base64.b64encode(csv_content.encode('utf-8')),
            'file_name': file_name,
        })
        wizard.action_read_headers()
        wizard.action_import_direct()

    def _run_import(self, csv_content, vendor=None, file_name='vendor_prices.csv'):
        wizard = self.env['mass.vendor.price.import'].create({
            'file_data': base64.b64encode(csv_content.encode('utf-8')),
            'file_name': file_name,
            'vendor_id': (vendor or self.vendor).id,
        })
        wizard.action_read_headers()
        wizard.action_import()
        return wizard

    def _supplierinfo(self, vendor=None, product=None):
        return self.env['product.supplierinfo'].search([
            ('partner_id', '=', (vendor or self.vendor).id),
            ('product_tmpl_id', '=', (product or self.product).id),
        ])

    def test_01_flow_and_mapping(self):
        """Test the wizard flow: Upload -> Mapping -> Import."""
        wizard = self.env['mass.vendor.price.import'].create({
            'file_data': base64.b64encode(b"sku,brand,price\nSKU123,BOSCH,42.50"),
            'file_name': 'vendor_prices.csv',
            'vendor_id': self.vendor.id,
        })
        self.assertEqual(wizard.state, 'upload')

        wizard.action_read_headers()
        self.assertEqual(wizard.state, 'mapping')
        self.assertTrue(len(wizard.mapping_ids) > 0, "Mappings should have been generated")

        sku_mapping = wizard.mapping_ids.filtered(lambda m: m.file_column_name == 'sku')
        self.assertEqual(sku_mapping.field_name, 'sku', "Fuzzy map failed to identify SKU")
        price_mapping = wizard.mapping_ids.filtered(lambda m: m.file_column_name == 'price')
        self.assertEqual(price_mapping.field_name, 'price', "Fuzzy map failed to identify Price")

        wizard.action_import()

        supplierinfo = self._supplierinfo()
        self.assertEqual(len(supplierinfo), 1, "A vendor price line should have been created")
        self.assertAlmostEqual(supplierinfo.price, 42.50)
        self.assertEqual(supplierinfo.partner_id, self.vendor)

    def test_02_validation_missing_required_mapping(self):
        """Importing without a mapped SKU column must raise."""
        wizard = self.env['mass.vendor.price.import'].create({
            'file_data': base64.b64encode(b"sku,brand,price\nSKU123,BOSCH,10"),
            'file_name': 'vendor_prices.csv',
            'vendor_id': self.vendor.id,
        })
        wizard.action_read_headers()
        wizard.mapping_ids.filtered(lambda m: m.field_name == 'sku').write({'field_name': False})

        with self.assertRaises(UserError):
            wizard.action_import()

    def test_03_unsupported_file_format(self):
        """A non-csv/xlsx file must be rejected on header read."""
        wizard = self.env['mass.vendor.price.import'].create({
            'file_data': base64.b64encode(b"irrelevant"),
            'file_name': 'prices.txt',
            'vendor_id': self.vendor.id,
        })
        with self.assertRaises(UserError):
            wizard.action_read_headers()

    def test_04_default_code_helper(self):
        """The code helper joins a 3-letter brand prefix to the SKU."""
        wizard = self.env['mass.vendor.price.import'].new()
        self.assertEqual(wizard._compute_default_code('BOSCH', 'SKU123'), 'BOS_SKU123')
        self.assertEqual(wizard._compute_default_code('AB', 'X1'), 'AB_X1')

    def test_05_skips_unknown_products(self):
        """Rows whose product does not exist are skipped, matched rows still import."""
        self._run_import(
            "sku,brand,price\n"
            "SKU123,BOSCH,30\n"
            "DOESNOTEXIST,BOSCH,99"
        )
        matched = self.env['product.supplierinfo'].search([('partner_id', '=', self.vendor.id)])
        self.assertEqual(len(matched), 1, "Only the existing product should get a vendor price")
        self.assertEqual(matched.product_tmpl_id, self.product)
        self.assertAlmostEqual(matched.price, 30.0)

    def test_06_reimport_replaces_price(self):
        """Re-importing for the same vendor replaces the previous price (no duplicates)."""
        self._run_import("sku,brand,price\nSKU123,BOSCH,30", file_name='first.csv')
        self._run_import("sku,brand,price\nSKU123,BOSCH,55", file_name='second.csv')

        supplierinfo = self._supplierinfo()
        self.assertEqual(len(supplierinfo), 1, "Re-import must not duplicate the vendor price")
        self.assertAlmostEqual(supplierinfo.price, 55.0)

    def test_07_optional_min_qty_and_lead_time(self):
        """Optional Minimum Quantity and Lead Time columns are imported."""
        self._run_import(
            "sku,brand,price,min qty,lead time\n"
            "SKU123,BOSCH,30,5,7"
        )
        supplierinfo = self._supplierinfo()
        self.assertEqual(len(supplierinfo), 1)
        self.assertAlmostEqual(supplierinfo.min_qty, 5.0)
        self.assertEqual(supplierinfo.delay, 7)

    def test_08_other_vendor_prices_untouched(self):
        """Importing for one vendor must not delete another vendor's prices."""
        vendor2 = self.env['res.partner'].create({
            'name': 'Other Supplier',
            'supplier_rank': 1,
        })
        self._run_import("sku,brand,price\nSKU123,BOSCH,30", vendor=self.vendor)
        self._run_import("sku,brand,price\nSKU123,BOSCH,80", vendor=vendor2)

        v1 = self._supplierinfo(vendor=self.vendor)
        v2 = self._supplierinfo(vendor=vendor2)
        self.assertEqual(len(v1), 1)
        self.assertEqual(len(v2), 1)
        self.assertAlmostEqual(v1.price, 30.0)
        self.assertAlmostEqual(v2.price, 80.0)

    def test_09_empty_price_row_skipped(self):
        """A row with a blank price is skipped instead of importing a zero price."""
        self._run_import(
            "sku,brand,price\n"
            "SKU123,BOSCH,"
        )
        self.assertFalse(self._supplierinfo(), "Blank-price rows must not create a vendor price")
