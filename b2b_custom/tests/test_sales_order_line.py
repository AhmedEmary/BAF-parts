from odoo.tests.common import TransactionCase


class TestSalesOrderLineCart(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create Dummy Website & Customer
        cls.website = cls.env['website'].create({'name': 'Test B2B Website'})
        cls.partner = cls.env['res.partner'].create({'name': 'Test B2B Customer'})
        

        cls.disc_code_1 = cls.env['discount.code'].create({
            'name': 'Test VIP Discount',
            'value_ids': [(0, 0, {
                'partner_id': cls.partner.id,
                'percentage': 20.0,
            })]
        })
        
        cls.product = cls.env['product.product'].create({
            'name': 'Test Cart Product',
            'list_price': 200.0,
            'disc_code_1': cls.disc_code_1.id,
            'surcharge': 0.0, # Ensuring no surcharge interferes
        })

    def test_01_cart_price_interceptor(self):
        """Test that the website cart cannot force a retail price_unit and custom math applies"""
        # Create a website cart for the B2B Customer
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'website_id': self.website.id,
        })

        # Simulate the website adding to cart with a forced price of 9.99
        line = self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': self.product.id,
            'product_uom_qty': 2,
            'price_unit': 9.99, # Odoo's forced retail price
        })

        # 1. Verify Interceptor Dropped the 9.99
        self.assertNotEqual(line.price_unit, 9.99, "Interceptor failed to drop the forced website price.")
        
        # 2. Verify Custom Math Took Over 
        # Base list_price is 200. Discount 1 is 20%. Expected = 200 * (1 - 0.20) = 160.0
        self.assertEqual(line.price_unit, 160.0, "Unit price should be perfectly discounted to 160.0")
        
        # 3. Verify backend breakdown fields are populated correctly
        self.assertEqual(line.retail_price, 200.0, "Retail price field should be 200.0")
        self.assertEqual(line.disc_code_1, 20.0, "Discount 1 field should record 20.0%")
        
        # 4. Verify standard discount is wiped
        self.assertEqual(line.discount, 0.0, "Standard Odoo discount must be forced to 0.0")

        # 5. Verify Custom Cart Subtotal (2 qty * 160.0)
        display_price = line._get_cart_display_price()
        self.assertEqual(display_price, 320.0, "Cart display price should be price_unit * qty.")

    def test_02_strikethrough_hidden(self):
        """Test that strikethrough price is hidden for website orders"""
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'website_id': self.website.id,
        })
        line = self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': self.product.id,
        })
        
        self.assertFalse(line._should_show_strikethrough_price(), "Strikethrough should be disabled for website carts.")


class TestSaleOrderLineDiscounts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        
        # 1. Create Test Partners
        cls.partner_specific = cls.env['res.partner'].create({'name': 'Specific VIP Customer'})
        cls.partner_regular = cls.env['res.partner'].create({'name': 'Regular Customer'})
        cls.partner_public = cls.env['res.partner'].create({'name': 'Public Anonymous Visitor'})

        # 2. Create the Discount Code with Rules
        cls.discount_code = cls.env['discount.code'].create({
            'name': 'TEST-DISCOUNT',
            'value_ids': [
                # Specific rule for VIP: 20%
                (0, 0, {
                    'partner_id': cls.partner_specific.id,
                    'percentage': 20.0,
                }),
                # Fallback rule for everyone else (Partner is False): 5%
                (0, 0, {
                    'partner_id': False,
                    'percentage': 5.0,
                }),
            ]
        })

        # 3. Create a Product and assign the discount code
        cls.product = cls.env['product.product'].create({
            'name': 'Discounted Product',
            'list_price': 100.0,
            'disc_code_1': cls.discount_code.id,
        })
        
        # Create a Product with NO discount code
        cls.product_no_discount = cls.env['product.product'].create({
            'name': 'Standard Product',
            'list_price': 100.0,
        })

    def test_specific_customer_discount(self):
        """ Test that a customer with a specific rule gets their exact percentage """
        order = self.env['sale.order'].create({
            'partner_id': self.partner_specific.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })]
        })
        line = order.order_line[0]
        self.assertEqual(line.disc_code_1, 20.0, "VIP Customer should receive the specific 20% discount.")

    def test_regular_customer_fallback_discount(self):
        """ Test that a registered customer without a specific rule gets the fallback percentage """
        order = self.env['sale.order'].create({
            'partner_id': self.partner_regular.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })]
        })
        line = order.order_line[0]
        self.assertEqual(line.disc_code_1, 5.0, "Regular customer should receive the fallback 5% discount.")

    def test_anonymous_visitor_fallback_discount(self):
        """ Test that an anonymous/public website visitor gets the fallback percentage """
        # Simulate a website cart created for a public user
        order = self.env['sale.order'].create({
            'partner_id': self.partner_public.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id,
                'product_uom_qty': 1,
            })]
        })
        line = order.order_line[0]
        self.assertEqual(line.disc_code_1, 5.0, "Anonymous visitor should receive the fallback 5% discount.")

    def test_no_discount_code(self):
        """ Test that a product with no discount code applied returns 0% for everyone """
        order = self.env['sale.order'].create({
            'partner_id': self.partner_specific.id,
            'order_line': [(0, 0, {
                'product_id': self.product_no_discount.id,
                'product_uom_qty': 1,
            })]
        })
        line = order.order_line[0]
        self.assertEqual(line.disc_code_1, 0.0, "Product with no discount code should return 0%.")
