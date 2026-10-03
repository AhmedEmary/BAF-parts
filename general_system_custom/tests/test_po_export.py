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

    def test_excel_sku_strips_brand_prefix(self):
        """Suppliers see the bare SKU. The brand-prefixed `default_code`
        (LR_LR097165) is a BAF-internal identifier and must not leak."""
        brand = self.env['product.brand'].create({'name': 'LR'})
        prefixed = self.env['product.product'].create({
            'name': 'LR product',
            'type': 'consu',
            'brand': brand.id,
            'default_code': 'LR_LR097165',
        })
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({
                'product_id': prefixed.id, 'product_qty': 1, 'price_unit': 10.0,
            })],
        })
        action = po.action_send_grouped_po_email()
        attachment = self.env['ir.attachment'].browse(
            action['context']['default_attachment_ids'][0])
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(attachment.datas)))
        row_values = [cell.value for cell in wb.active[2]]
        self.assertIn('LR097165', row_values)
        self.assertNotIn('LR_LR097165', row_values)

    def test_excel_sku_prefers_product_sku_field(self):
        """When product.sku is set, it wins over default_code — that's the
        per-brand SKU vendors already recognize."""
        brand = self.env['product.brand'].create({'name': 'HON'})
        product = self.env['product.product'].create({
            'name': 'HON product',
            'type': 'consu',
            'brand': brand.id,
            'default_code': 'HON_12345',
            'sku': '12345',
        })
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({
                'product_id': product.id, 'product_qty': 1, 'price_unit': 10.0,
            })],
        })
        action = po.action_send_grouped_po_email()
        attachment = self.env['ir.attachment'].browse(
            action['context']['default_attachment_ids'][0])
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(attachment.datas)))
        row_values = [cell.value for cell in wb.active[2]]
        self.assertIn('12345', row_values)

    def test_sender_routing_kalkan_group(self):
        """Arnold, Euler, Brass, Kalkan → Kalkan mailbox."""
        for vname, expected_marker in [
            ('Autohaus Arnold GmbH & Co. KG', 'kalkan-auto.de'),
            ('Hermann Arnold GmbH', 'kalkan-auto.de'),
            ('Autohaus Euler GmbH', 'kalkan-auto.de'),
            ('Autohaus Brass Vertriebs GmbH & Co. KG', 'kalkan-auto.de'),
            ('Kalkan Automobile GmbH', 'kalkan-auto.de'),
        ]:
            vendor = self.env['res.partner'].create({'name': vname})
            po = self.env['purchase.order'].create({
                'partner_id': vendor.id,
                'order_line': [Command.create({
                    'product_id': self.product.id, 'product_qty': 1,
                })],
            })
            action = po.action_send_grouped_po_email()
            self.assertIn(expected_marker,
                          action['context']['default_email_from'],
                          "%s should send from Kalkan mailbox" % vname)

    def test_sender_routing_defaults_to_baf(self):
        """Krah, Enders, and everyone else → BAF mailbox."""
        for vname in ('Autohaus Krah & Enders GmbH',
                      'Krah+Enders GmbH & Co. KG',
                      'Autocenter Enders GmbH',
                      'Some Random Vendor'):
            vendor = self.env['res.partner'].create({'name': vname})
            po = self.env['purchase.order'].create({
                'partner_id': vendor.id,
                'order_line': [Command.create({
                    'product_id': self.product.id, 'product_qty': 1,
                })],
            })
            action = po.action_send_grouped_po_email()
            self.assertIn('info@baf-parts.com',
                          action['context']['default_email_from'],
                          "%s should send from BAF mailbox" % vname)

    def test_portal_button_disabled_on_recipient_groups(self):
        """The Odoo "View Quotation / View Order" access button is stripped
        from every recipient group so suppliers get plain text + Excel only."""
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({
                'product_id': self.product.id, 'product_qty': 1,
            })],
        })
        groups = po._notify_get_recipients_groups(
            self.env['mail.message'], model_description='Purchase Order',
            msg_vals={})
        for group in groups:
            self.assertFalse(
                group[2].get('has_button_access'),
                "Group %s must not carry the portal access button" % group[0])

    def _kalkan_po(self):
        vendor = self.env['res.partner'].create({
            'name': 'Autohaus Euler GmbH', 'email': 'order@euler.example.com'})
        return self.env['purchase.order'].create({
            'partner_id': vendor.id,
            'order_line': [Command.create({
                'product_id': self.product.id, 'product_qty': 1,
            })],
        })

    def test_kalkan_vendor_gets_kalkan_template(self):
        kalkan_template = self.env.ref(
            'general_system_custom.email_template_kalkan_purchase')
        po = self._kalkan_po()
        grouped_action = po.action_send_grouped_po_email()
        self.assertEqual(
            grouped_action['context']['default_template_id'], kalkan_template.id)
        rfq_action = po.action_rfq_send()
        self.assertEqual(
            rfq_action['context']['default_template_id'], kalkan_template.id)
        po.button_confirm()
        confirmed_action = po.action_rfq_send()
        self.assertEqual(
            confirmed_action['context']['default_template_id'], kalkan_template.id)

    def test_other_vendor_keeps_standard_template(self):
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'order_line': [Command.create({
                'product_id': self.product.id, 'product_qty': 1,
            })],
        })
        action = po.action_send_grouped_po_email()
        self.assertEqual(
            action['context']['default_template_id'],
            self.env.ref('purchase.email_template_edi_purchase').id)

    def test_kalkan_mail_never_mentions_baf(self):
        """Sent through the real composer, nothing the supplier sees (subject,
        body, sender, reply-to) mentions BAF; it is signed by Kalkan."""
        self.env.company.name = 'BAF Handels GmbH'
        self.env.user.signature = '<p>BAF Parts Team</p>'
        po = self._kalkan_po()
        action = po.action_send_grouped_po_email()
        composer = self.env['mail.compose.message'].with_context(
            action['context']).create({})
        composer._action_send_mail()

        mail = self.env['mail.mail'].search(
            [('model', '=', 'purchase.order'), ('res_id', '=', po.id)])
        self.assertEqual(len(mail), 1)
        for field in ('subject', 'body_html', 'email_from', 'reply_to'):
            self.assertNotIn('BAF', mail[field] or '', field)
        self.assertIn('Kalkan Automobile GmbH', mail.subject)
        self.assertIn('Kalkan Automobile GmbH', mail.body_html)
        self.assertIn('kalkan-auto.de', mail.email_from)
        self.assertIn('kalkan-auto.de', mail.reply_to)
