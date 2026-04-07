from odoo.tests.common import TransactionCase
from unittest.mock import MagicMock

# Import the controller module so we can patch its 'request' variable safely
from odoo.addons.fratellileo_custom.controllers import main as main_controller
from odoo.addons.fratellileo_custom.controllers.main import B2BListToPart

class TestListToPartParser(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.controller = B2BListToPart()        
        cls.brand_ferrari = cls.env['product.brand'].create({'name': 'Ferrari'})
        cls.product_ferrari = cls.env['product.product'].create({
            'name': 'Ferrari Engine Part',
            'brand': cls.brand_ferrari.id,
            'sku': '98765',
            'default_code': 'FER_98765',
            'list_price': 1500.0,
        })

    def setUp(self):
        super().setUp()
        self.mock_request = MagicMock()
        self.mock_request.env = self.env
        
        self.original_request = main_controller.request
        main_controller.request = self.mock_request

    def tearDown(self):
        # Restore the original request object after each test
        main_controller.request = self.original_request
        super().tearDown()

    def test_01_parse_and_search_found(self):
        """Test parsing raw text where the product exists in the DB"""
        raw_text = "Ferrari\t98765\t5"
        
        results, not_found = self.controller._parse_and_search(raw_text)

        self.assertEqual(len(results), 1, "Should find 1 product.")
        self.assertEqual(len(not_found), 0, "No products should be missing.")
        
        res = results[0]
        self.assertEqual(res['brand'], 'Ferrari')
        self.assertEqual(res['sku'], '98765')
        self.assertEqual(res['qty'], 5)
        self.assertEqual(res['product'].id, self.product_ferrari.id)

    def test_02_parse_and_search_not_found(self):
        """Test parsing raw text where the product does NOT exist"""
        raw_text = "Porsche\t111222\t10"
        
        results, not_found = self.controller._parse_and_search(raw_text)

        # Assertions
        self.assertEqual(len(results), 0, "Should find 0 products.")
        self.assertEqual(len(not_found), 1, "Should list 1 product as not found.")
        
        nf = not_found[0]
        self.assertEqual(nf['brand'], 'Porsche')
        self.assertEqual(nf['sku'], '111222')
        self.assertEqual(nf['qty'], 10)

    def test_03_parse_and_search_malformed_input(self):
        """Test handling of weird spaces, missing quantities, and empty lines"""
        # Malformed text: extra spaces instead of tabs, missing quantity defaults to 1
        raw_text = "\nFerrari      98765\n\n"
        
        results, not_found = self.controller._parse_and_search(raw_text)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['qty'], 1, "Quantity should default to 1 if missing.")
