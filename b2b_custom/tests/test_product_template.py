from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError

class TestProductTemplate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create dummy brands (assuming 'product.brand' is added by 'intelliwise_custom')
        cls.brand_maserati = cls.env['product.brand'].create({'name': 'Maserati'})
        cls.brand_short = cls.env['product.brand'].create({'name': 'Ab'})

    def test_01_compute_internal_reference_success(self):
        """Test that default_code is correctly generated as BRAND_SKU"""
        product = self.env['product.template'].create({
            'name': 'Test Ghibli Bumper',
            'brand': self.brand_maserati.id,
            'sku': '311401111'
        })
        # Note: Ensure you applied the fix from earlier making default_code a computed field
        self.assertEqual(product.default_code, 'MAS_311401111', "SKU generation failed. Expected MAS_311401111.")

    def test_02_compute_internal_reference_short_brand(self):
        """Test that default_code is generated if brand name is < 3 characters with brand na,e"""
        product = self.env['product.template'].create({
            'name': 'Test Short Brand Product',
            'brand': self.brand_short.id,
            'sku': '12345'
        })
        self.assertEqual(product.default_code, 'AB_12345', "SKU generation failed. Expected AB_12345.")

    def test_03_no_sku_provided(self):
        """Test product creation without an SKU"""
        product = self.env['product.template'].create({
            'name': 'Test No SKU',
            'brand': self.brand_maserati.id,
        })
        self.assertFalse(product.default_code, "Default code should be empty if SKU is not provided.")

    def test_04_search_fetch_exact_sku(self):
        """Test that _search_fetch returns ONLY exact SKU matches and ignores partials"""
        product1 = self.env['product.template'].create({
            'name': 'Target Product',
            'brand': self.brand_maserati.id,
            'sku': 'UNIQUE999'
        })
        product2 = self.env['product.template'].create({
            'name': 'Noise Product',
            'brand': self.brand_maserati.id,
            'sku': 'UNIQUE999X' # Notice the extra 'X'
        })
        
        search_detail = {'base_domain': []}
        # Run the search for 'UNIQUE999'
        results, count = self.env['product.template']._search_fetch(search_detail, 'UNIQUE999', limit=10, order='name asc')
        
        # Verify
        self.assertEqual(count, 1, "Should only find exactly 1 product.")
        self.assertIn(product1, results, "The exact SKU product should be found.")
        self.assertNotIn(product2, results, "Partial SKU matches should be strictly ignored!")

    def test_05_search_fetch_empty(self):
        """Test that an empty search caps the count at 50,000 for /shop performance"""
        search_detail = {'base_domain': []}
        results, count = self.env['product.template']._search_fetch(search_detail, '', limit=10, order='name asc')
        
        self.assertTrue(count <= 50000, "Count should be capped at 50,000 to prevent database lag.")