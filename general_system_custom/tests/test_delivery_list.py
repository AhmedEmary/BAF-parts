from odoo.tests import TransactionCase, tagged, Form
from odoo import Command, fields
from odoo.exceptions import UserError
import base64
import io
import openpyxl
from datetime import timedelta


@tagged('post_install', '-at_install')
class TestDeliveryListImport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.supplier = self.env['res.partner'].create({'name': 'Test Supplier', 'supplier_rank': 1})
        self.product_a = self.env['product.product'].create({
            'name': 'Product A',
            'default_code': 'SKU_A',
            'type': 'consu',
        })
        self.product_b = self.env['product.product'].create({
            'name': 'Product B',
            'default_code': 'SKU_B',
            'type': 'consu',
        })

    def _create_excel_file(self, headers, rows):
        """ Helper to generate a base64 encoded Excel file """
        output = io.BytesIO()
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        workbook.save(output)
        return base64.b64encode(output.getvalue())

    def test_import_fifo_allocation(self):
        """ Test normal FIFO allocation across multiple POs """
        # 1. Create 2 POs (Oldest first)
        po1 = self.env['purchase.order'].create({
            'partner_id': self.supplier.id,
            'date_order': fields.Datetime.now() - timedelta(days=1),
            'order_line': [Command.create({'product_id': self.product_a.id, 'product_qty': 10.0, 'price_unit': 100.0})]
            
        })
        po1.button_confirm() # Date order is set here usually
        
        # Ensure distinct timing
        po2 = self.env['purchase.order'].create({
            'partner_id': self.supplier.id,
            'order_line': [Command.create({'product_id': self.product_a.id, 'product_qty': 10.0, 'price_unit': 100.0})]
        })
        po2.button_confirm()

        # 2. Prepare Import (Received 15 units -> Should fill PO1 (10) and take 5 from PO2)
        excel_data = self._create_excel_file(
            ['SKU', 'Qty', 'Price'],
            [['SKU_A', 15, 100.0]]
        )

        import_rec = self.env['delivery.list.import'].create({
            'supplier_id': self.supplier.id,
            'file_data': excel_data,
            'file_name': 'test.xlsx'
        })

        # 3. Process
        import_rec.action_process_file()

        # 4. Verify Results
        self.assertEqual(len(import_rec.line_ids), 2, "Should have split into 2 lines")
        
        # Check Lines (Ordered by FIFO)
        line1 = import_rec.line_ids[0]
        line2 = import_rec.line_ids[1]

        self.assertEqual(line1.po_id, po1, "First line should match oldest PO")
        self.assertEqual(line1.qty_split, 10.0, "PO1 should be fully allocated")
        
        self.assertEqual(line2.po_id, po2, "Second line should match newer PO")
        self.assertEqual(line2.qty_split, 5.0, "PO2 should take the remainder")

        # Verify Summary
        self.assertEqual(import_rec.total_received, 15.0)
        self.assertFalse(import_rec.has_price_variance)

    def test_validation_no_po_found(self):
        """ Test error when no PO exists for the product/supplier """
        excel_data = self._create_excel_file(
            ['SKU', 'Qty', 'Price'],
            [['SKU_A', 5, 100.0]]
        )
        import_rec = self.env['delivery.list.import'].create({
            'supplier_id': self.supplier.id,
            'file_data': excel_data,
            'file_name': 'test.xlsx'
        })

        with self.assertRaisesRegex(UserError, "No Purchase Orders found"):
            import_rec.action_process_file()

    def test_validation_product_not_found(self):
        """ Test error when SKU does not exist """
        excel_data = self._create_excel_file(
            ['SKU', 'Qty', 'Price'],
            [['INVALID_SKU', 5, 100.0]]
        )
        import_rec = self.env['delivery.list.import'].create({
            'supplier_id': self.supplier.id,
            'file_data': excel_data,
            'file_name': 'test.xlsx'
        })

        with self.assertRaisesRegex(UserError, "Product not found"):
            import_rec.action_process_file()

    def test_confirm_delivery_updates_po(self):
        """ Test that confirming updates the PO line 'qty_split' field """
        po = self.env['purchase.order'].create({
            'partner_id': self.supplier.id,
            'order_line': [Command.create({'product_id': self.product_a.id, 'product_qty': 10.0})]
        })
        po.button_confirm()

        excel_data = self._create_excel_file(['SKU', 'Qty', 'Price'], [['SKU_A', 4, 100.0]])
        
        import_rec = self.env['delivery.list.import'].create({
            'supplier_id': self.supplier.id,
            'file_data': excel_data,
            'file_name': 'test.xlsx'
        })
        import_rec.action_process_file()
        
        # Confirm
        import_rec.action_confirm_delivery()
        
        self.assertEqual(import_rec.state, 'confirmed')
        # Check custom field on PO Line (assuming purchase_order_line.py has qty_split)
        self.assertEqual(po.order_line[0].qty_split, 4.0)

    def test_confirm_mismatch_error(self):
        """ Test error if split lines do not sum up to Excel quantity """
        po = self.env['purchase.order'].create({
            'partner_id': self.supplier.id,
            'order_line': [Command.create({'product_id': self.product_a.id, 'product_qty': 10.0})]
        })
        po.button_confirm()

        excel_data = self._create_excel_file(['SKU', 'Qty', 'Price'], [['SKU_A', 5, 100.0]])
        
        import_rec = self.env['delivery.list.import'].create({
            'supplier_id': self.supplier.id,
            'file_data': excel_data,
            'file_name': 'test.xlsx'
        })
        import_rec.action_process_file()

        # Manually tamper with the split line
        import_rec.line_ids[0].qty_split = 3.0  # Excel says 5, we changed it to 3

        with self.assertRaisesRegex(UserError, "split quantities do not match received quantities"):
            import_rec.action_confirm_delivery()

    def test_price_variance_flags(self):
        """ Test computation of price variance """
        po = self.env['purchase.order'].create({
            'partner_id': self.supplier.id,
            'order_line': [Command.create({'product_id': self.product_a.id, 'product_qty': 10.0, 'price_unit': 100.0})]
        })
        po.button_confirm()

        # Import with different price (120 vs 100)
        excel_data = self._create_excel_file(['SKU', 'Qty', 'Price'], [['SKU_A', 5, 120.0]])
        
        import_rec = self.env['delivery.list.import'].create({
            'supplier_id': self.supplier.id,
            'file_data': excel_data,
            'file_name': 'test.xlsx'
        })
        import_rec.action_process_file()

        self.assertTrue(import_rec.has_price_variance)
        line = import_rec.line_ids[0]
        self.assertTrue(line.price_variance)
        self.assertEqual(line.price_difference, 20.0)

    def test_onchange_logic(self):
        """ Test the UI onchange logic for manual editing """
        po = self.env['purchase.order'].create({
            'partner_id': self.supplier.id,
            'order_line': [Command.create({'product_id': self.product_a.id, 'product_qty': 10.0, 'price_unit': 50.0})]
        })
        po.button_confirm()

        import_rec = self.env['delivery.list.import'].create({'supplier_id': self.supplier.id})
        
        # Create Excel Line manually to simulate data existence
        self.env['delivery.list.excel.line'].create({
            'import_id': import_rec.id,
            'product_id': self.product_a.id,
            'sku': 'SKU_A',
            'qty_received': 5.0,
            'price_supplier': 55.0
        })

        # Use Form to simulate the UI behavior correctly
        with Form(self.env['delivery.list.line']) as line_form:
            line_form.import_id = import_rec
            
            # 1. Test Product Onchange (should fill SKU/Price from excel line)
            line_form.product_id = self.product_a
            self.assertEqual(line_form.sku, 'SKU_A')
            self.assertEqual(line_form.price_supplier, 55.0)

            # 2. Test PO Onchange (should fill PO details)
            line_form.po_id = po
            # The computes run automatically in the Form view
            self.assertEqual(line_form.po_line_id, po.order_line[0])
            self.assertEqual(line_form.open_qty_po, 10.0)
            self.assertEqual(line_form.price_po, 50.0)