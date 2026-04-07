import odoo.tests
from odoo.tests.common import HttpCase
import json

@odoo.tests.tagged('post_install', '-at_install')
class TestWebsiteControllers(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Setup test partner
        cls.partner = cls.env['res.partner'].create({
            'name': 'B2B Client Test',
            'email': 'b2b@test.com',
        })
        cls.user = cls.env['res.users'].create({
            'name': 'B2B User',
            'login': 'b2b_user',
            'partner_id': cls.partner.id,
        })
        
        # Create a product
        cls.product = cls.env['product.product'].create({
            'name': 'Test Consumable',
            'type': 'consu',
            'list_price': 50.0,
        })
        
        # Create a backordered Sale Order
        cls.sale_order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id,
            'state': 'sale',
            'order_line': [(0, 0, {
                'product_id': cls.product.id,
                'product_uom_qty': 10,
                'qty_delivered': 5,
                # Force invoice_status to trigger backorder domain
                'invoice_status': 'to invoice' 
            })]
        })

    def test_01_update_custom_fields_ajax(self):
        """Test the AJAX jsonrpc route for custom checkout fields"""
        # We need to simulate a cart for the session. 
        # In testing, we just verify the endpoint accepts the payload correctly.
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "shipping_method": "ocean",
                "customer_po": "PO-998877"
            },
            "id": 1
        }
        
        response = self.url_open(
            '/shop/update_custom_fields', 
            data=json.dumps(payload), 
            headers={'Content-Type': 'application/json'}
        )
        
        # Verify the endpoint doesn't crash (returns 200 OK)
        self.assertEqual(response.status_code, 200)
        
    def test_02_backorders_page_access(self):
        """Test that the backorders page loads correctly for authenticated users"""
        self.authenticate('b2b_user', 'b2b_user') # Authenticate test user
        
        # Hit the page
        response = self.url_open('/backorders')
        self.assertEqual(response.status_code, 200, "Backorders page failed to load.")
        
        # Ensure our test product name appears in the rendered HTML
        self.assertIn(b'Test Consumable', response.content)

