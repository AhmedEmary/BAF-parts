from odoo.tests import TransactionCase, tagged
from odoo import Command

@tagged('post_install', '-at_install')
class TestImports(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner_a = self.env['res.partner'].create({'name': 'Vendor A'})
        self.partner_b = self.env['res.partner'].create({'name': 'Vendor B'})

    def test_discount_code_import_upsert(self):
        """ 
        Test the 'Upsert' logic for Discount Codes (simulating Import behavior):
        1. Import (Create) a new code.
        2. Import (Create) the SAME code with DIFFERENT values -> Should Update, not Duplicate.
        3. Import (Create) the SAME code with NEW Supplier -> Should Add Value.
        """
        DiscountCode = self.env['discount.code']

        vals_1 = {
            'name': 'SUMMER',
            'value_ids': [
                Command.create({'partner_id': self.partner_a.id, 'percentage': 10.0})
            ]
        }
        code = DiscountCode.create([vals_1])
        
        self.assertEqual(len(code), 1, "Should create exactly 1 record")
        self.assertEqual(code.name, 'SUMMER')
        self.assertEqual(len(code.value_ids), 1)
        self.assertEqual(code.value_ids[0].percentage, 10.0)

        vals_2 = {
            'name': 'SUMMER',
            'value_ids': [
                Command.create({'partner_id': self.partner_a.id, 'percentage': 20.0})
            ]
        }
        code_2 = DiscountCode.create([vals_2])
        
        all_codes = DiscountCode.search([('name', '=', 'SUMMER')])
        self.assertEqual(len(all_codes), 1, "Should still be only 1 Discount Code 'SUMMER'")
        
        code.invalidate_recordset() 
        self.assertEqual(len(code.value_ids), 1, "Should still have 1 value line (same vendor)")
        self.assertEqual(code.value_ids[0].percentage, 20.0, "Percentage should be updated to 20.0")

        vals_3 = {
            'name': 'SUMMER',
            'value_ids': [
                Command.create({'partner_id': self.partner_b.id, 'percentage': 15.0})
            ]
        }
        DiscountCode.create([vals_3])
        
        self.assertEqual(len(all_codes), 1)
        
        code.invalidate_recordset()
        self.assertEqual(len(code.value_ids), 2, "Should now have 2 value lines (Vendor A and Vendor B)")
        
        val_a = code.value_ids.filtered(lambda v: v.partner_id == self.partner_a)
        val_b = code.value_ids.filtered(lambda v: v.partner_id == self.partner_b)
        
        self.assertEqual(val_a.percentage, 20.0)
        self.assertEqual(val_b.percentage, 15.0)

    def test_auto_product_import_upsert(self):
        """
        Test 'Upsert' logic for Products based on Internal Reference (default_code).
        Simulates importing the same product reference twice with different data.
        """
        Product = self.env['product.template']
        
        vals_1 = {
            'name': 'Product A',
            'default_code': 'REF123',
            'list_price': 100.0
        }
        prod = Product.create([vals_1])
        self.assertEqual(prod.name, 'Product A')
        self.assertEqual(prod.list_price, 100.0)

        vals_2 = {
            'name': 'Product A Updated', # Name change
            'default_code': 'REF123',    # Same Ref
            'list_price': 150.0          # Price change
        }
        prod_2 = Product.create([vals_2])
        
        all_prods = Product.search([('default_code', '=', 'REF123')])
        self.assertEqual(len(all_prods), 1, "Should not create duplicate product for same Ref")
        
        prod.invalidate_recordset()
        self.assertEqual(prod.name, 'Product A Updated')
        self.assertEqual(prod.list_price, 150.0)

    def test_get_import_templates(self):
        """ Test that template retrieval methods return correct structure """
        
        # Discount Codes
        res_dc = self.env['discount.code'].get_import_templates()
        self.assertTrue(isinstance(res_dc, list))
        self.assertTrue(res_dc[0].get('template'))
        self.assertIn('discount_codes_template.xlsx', res_dc[0]['template'])

        # Products
        res_prod = self.env['product.template'].get_import_templates()
        self.assertTrue(isinstance(res_prod, list))
        self.assertTrue(res_prod[0].get('template'))
        self.assertIn('intelliwise_products_template_excel.xlsx', res_prod[0]['template'])
        
        # Sale Order
        res_so = self.env['sale.order'].get_import_templates()
        self.assertTrue(res_so)
        self.assertIn('quotations_import_template.xlsx', res_so[0]['template'])
