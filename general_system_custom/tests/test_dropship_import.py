import base64
import io
import openpyxl
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError
from odoo import Command

@tagged('post_install', '-at_install')
class TestDropshipImport(TransactionCase):

    def setUp(self):
        super().setUp()
        # 1. Setup Master Data
        self.customer = self.env['res.partner'].create({'name': 'Customer D'})
        self.vendor = self.env['res.partner'].create({'name': 'Supplier S'})
        
        self.product = self.env['product.product'].create({
            'name': 'Dropship Item',
            'type': 'consu', # or product
            'default_code': 'SKU-DROP',
            'list_price': 100.0,
        })

        # 2. Create Sales Order
        self.so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_uom_qty': 10.0,
                'price_unit': 100.0,
            })]
        })
        self.so.action_confirm()

        # 3. Create Linked Purchase Order
        self.po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': self.so.id,
            'name': 'PO-DROP-001', # Explicit Name for matching
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': 10.0,
                'price_unit': 50.0,
            })]
        })
        self.po.button_confirm()

        # Ensure the PO is treated as 'Dropship' for auto-split logic
        dropship_type = self.env['stock.picking.type'].search([('code', '=', 'dropship')], limit=1)
        if dropship_type:
            self.po.picking_type_id = dropship_type

    def _create_excel_file(self, rows):
        """ Helper to generate a base64 Excel file from a list of rows """
        output = io.BytesIO()
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for row in rows:
            sheet.append(row)
        workbook.save(output)
        return base64.b64encode(output.getvalue())

    def test_dropship_import_success(self):
        """ Test that valid data creates a Dropship Pallet and Lines """
        
        # Prepare Excel Data
        headers = ['SKU', 'Quantity', 'PO Number', 'Pallet Number']
        data = ['SKU-DROP', 5.0, 'PO-DROP-001', 'PLT-VENDOR-123']
        file_content = self._create_excel_file([headers, data])

        # Create Import Record (New Model)
        import_rec = self.env['dropship.import'].create({
            'file_name': 'test.xlsx',
            'file_data': file_content,
            'customer_id': self.customer.id, # Required field
        })
        
        # 1. Process File (Draft -> Processed)
        import_rec.action_process_file()
        self.assertEqual(import_rec.state, 'processed')
        self.assertEqual(len(import_rec.line_ids), 1)
        self.assertEqual(import_rec.line_ids[0].po_id, self.po)

        # 2. Confirm (Processed -> Done)
        import_rec.action_confirm()
        self.assertEqual(import_rec.state, 'done')

        # Verify Pallet Creation
        pallet = self.env['warehouse.pallet'].search([
            ('line_ids.purchase_order_id', '=', self.po.id),
            ('partner_id', '=', self.customer.id)
        ], limit=1)
        self.assertTrue(pallet, "Pallet should be created")
        self.assertEqual(pallet.partner_id, self.customer, "Pallet should be linked to the SO Customer")
        self.assertEqual(pallet.state, 'dropship', "Pallet state should be 'dropship'")

        # Verify Line Creation
        self.assertEqual(len(pallet.line_ids), 1)
        line = pallet.line_ids[0]
        self.assertEqual(line.product_id, self.product)
        self.assertEqual(line.fulfill_qty, 5.0)
        self.assertEqual(line.purchase_order_id, self.po)
        self.assertEqual(line.sale_order_id, self.so)
        self.assertTrue(line.is_dropship)

    def test_import_auto_split_no_po(self):
        """ Test logic when PO column is empty (Auto Split) """
        headers = ['SKU', 'Quantity', 'PO Number', 'Pallet Number']
        data = ['SKU-DROP', 3.0, '', 'PLT-AUTO'] # PO is blank
        file_content = self._create_excel_file([headers, data])

        import_rec = self.env['dropship.import'].create({
            'file_name': 'test.xlsx',
            'file_data': file_content,
            'customer_id': self.customer.id,
        })
        
        # Should automatically find the open PO based on Customer + Product + Dropship
        import_rec.action_process_file()
        
        self.assertEqual(len(import_rec.line_ids), 1)
        self.assertEqual(import_rec.line_ids[0].po_id, self.po)
        self.assertEqual(import_rec.line_ids[0].qty, 3.0)

    def test_import_error_missing_product(self):
        """ Test error when SKU does not exist """
        headers = ['SKU', 'Quantity', 'PO Number', 'Pallet Number']
        data = ['SKU-UNKNOWN', 5.0, 'PO-DROP-001', 'PLT-1']
        file_content = self._create_excel_file([headers, data])

        import_rec = self.env['dropship.import'].create({
            'file_name': 'test.xlsx',
            'file_data': file_content,
            'customer_id': self.customer.id,
        })
        
        with self.assertRaises(UserError, msg="Should fail on unknown Product"):
            import_rec.action_process_file()

    def test_import_error_missing_po(self):
        """ Test error when PO Number provided does not exist """
        headers = ['SKU', 'Quantity', 'PO Number', 'Pallet Number']
        data = ['SKU-DROP', 5.0, 'PO-UNKNOWN-999', 'PLT-1']
        file_content = self._create_excel_file([headers, data])

        import_rec = self.env['dropship.import'].create({
            'file_name': 'test.xlsx',
            'file_data': file_content,
            'customer_id': self.customer.id,
        })
        
        with self.assertRaises(UserError, msg="Should fail on unknown PO"):
            import_rec.action_process_file()
