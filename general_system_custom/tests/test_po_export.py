import base64
import io
import openpyxl
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError
from odoo import Command

@tagged('post_install', '-at_install')
class TestGroupedPOExport(TransactionCase):

    def setUp(self):
        super().setUp()
        self.vendor = self.env['res.partner'].create({'name': 'Test Vendor'})
        self.vendor_other = self.env['res.partner'].create({'name': 'Other Vendor'})
        self.customer = self.env['res.partner'].create({'name': 'Test Customer'})
        
        # 2. Setup Product
        self.brand = self.env['product.brand'].create({'name': 'Test Brand'})
        self.product = self.env['product.product'].create({
            'name': 'Test Widget',
            'type': 'consu',
            'list_price': 100.0,
            'brand': self.brand.id,
            'default_code': 'SKU123'
        })

    def test_vendor_consistency_check(self):
        """ Test that selecting POs from different suppliers raises an error [cite: 1102] """
        po1 = self.env['purchase.order'].create({'partner_id': self.vendor.id})
        po2 = self.env['purchase.order'].create({'partner_id': self.vendor_other.id})

        # Expect Error: "Different suppliers detected"
        with self.assertRaises(UserError):
            (po1 | po2).action_send_grouped_po_email()

    def test_export_vendor_columns(self):
        """ Test PO export has the expected headers and data rows """
        so = self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'order_line': [Command.create({'product_id': self.product.id})]
        })
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': so.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': 1.0,
                'retail_price': 100.0,
                'price_unit': 80.0,
            })],
        })

        action = po.action_send_grouped_po_email()

        self.assertEqual(po.send_po_status, 'success', "PO status should be updated to 'success'")

        attachment_id = action['context']['default_attachment_ids'][0]
        attachment = self.env['ir.attachment'].browse(attachment_id)
        self.assertTrue(attachment, "An Excel attachment should be generated")

        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(attachment.datas)))
        ws = wb.active
        headers = [cell.value for cell in ws[1]]
        self.assertEqual(
            headers,
            ["PO N.", "Brand", "SKU", "Quantity", "Retail",
             "Surcharge", "Unit Net", "Total"],
        )

        row_values = [cell.value for cell in ws[2]]
        self.assertIn('Test Brand', row_values)
        self.assertIn('SKU123', row_values)

    def test_dropship_sheet_separation(self):
        """ Test that standard and dropship orders go to different sheets [cite: 1103] """
        dropship_addr = self.env['res.partner'].create({'name': 'Dropship Loc', 'street': '123 Drop St'})

        po_std = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 1})]
        })

        po_drop = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'dest_address_id': dropship_addr.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_qty': 1})]
        })

        # Run action on both
        action = (po_std | po_drop).action_send_grouped_po_email()
        
        attachment_id = action['context']['default_attachment_ids'][0]
        attachment = self.env['ir.attachment'].browse(attachment_id)
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(attachment.datas)))
        
        # [cite_start]Verify Sheets [cite: 1103]
        self.assertIn("Standard Orders", wb.sheetnames)
        self.assertIn("Dropship Orders", wb.sheetnames)
        
        # [cite_start]Verify Dropship Headers have address info [cite: 1104]
        ws_drop = wb["Dropship Orders"]
        headers = [cell.value for cell in ws_drop[1]]
        self.assertIn("Delivery Address", headers)

    def test_purchase_templates_attach_no_report(self):
        """No vendor-facing purchase mail template may carry a PDF report."""
        for xmlid in (
            'purchase.email_template_edi_purchase',
            'purchase.email_template_edi_purchase_done',
            'purchase.email_template_edi_purchase_reminder',
        ):
            template = self.env.ref(xmlid)
            self.assertFalse(
                template.report_template_ids,
                "%s must not attach a report" % xmlid)

    def test_rfq_send_attaches_excel(self):
        """ The form buttons (Send RFQ / Send PO) attach the same workbook """
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': 2.0,
                'price_unit': 50.0,
            })],
        })

        action = po.action_rfq_send()

        self.assertEqual(po.send_po_status, 'success')
        attachment_id = action['context']['default_attachment_ids'][0]
        attachment = self.env['ir.attachment'].browse(attachment_id)
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(attachment.datas)))
        headers = [cell.value for cell in wb.active[1]]
        self.assertIn("SKU", headers)
        row_values = [cell.value for cell in wb.active[2]]
        self.assertIn('SKU123', row_values)

    def test_rfq_send_attaches_excel_on_confirmed_order(self):
        """ Confirmed POs take the same path - Excel, still no PDF """
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': 1.0,
                'price_unit': 50.0,
            })],
        })
        po.button_confirm()
        self.assertEqual(po.state, 'purchase')

        action = po.action_rfq_send()

        template = self.env['mail.template'].browse(
            action['context']['default_template_id'])
        self.assertFalse(template.report_template_ids)
        attachment_id = action['context']['default_attachment_ids'][0]
        self.assertTrue(self.env['ir.attachment'].browse(attachment_id).exists())
