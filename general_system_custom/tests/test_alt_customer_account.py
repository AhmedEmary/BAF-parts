import base64
import io

import openpyxl

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAltCustomerAccountNumber(TransactionCase):

    def setUp(self):
        super().setUp()
        Partner = self.env['res.partner']
        self.customer = Partner.create({
            'name': 'Alt Test Customer',
            'baf_alt_account_number': 'E3BF',
        })
        # Pin a known contact_number: the create() sequence value would otherwise
        # depend on DB state.
        self.customer.contact_number = '10012'
        self.vendor = Partner.create({
            'name': 'Alt Test Vendor',
            'is_trusted_vendor': True,
        })
        self.customer_no_alt = Partner.create({
            'name': 'Customer With No Alt',
            'contact_number': '10099',
        })

    def _make_po(self, customer, source='default'):
        so = self.env['sale.order'].create({'partner_id': customer.id})
        return self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': so.id,
            'baf_customer_account_source': source,
        })

    # ── partner helper ─────────────────────────────────────────────────────
    def test_partner_helper_returns_default_by_default(self):
        self.assertEqual(
            self.customer._baf_customer_account_number(), '10012')

    def test_partner_helper_returns_alt_when_requested(self):
        self.assertEqual(
            self.customer._baf_customer_account_number(use_alt=True), 'E3BF')

    def test_partner_helper_falls_back_when_alt_missing(self):
        self.assertEqual(
            self.customer_no_alt._baf_customer_account_number(use_alt=True),
            '10099',
        )

    def test_po_default_source_uses_contact_number(self):
        po = self._make_po(self.customer, source='default')
        self.assertEqual(po.baf_customer_account_source, 'default')
        self.assertEqual(po.baf_customer_account_number, '10012')

    def test_po_alternative_source_uses_alt(self):
        po = self._make_po(self.customer, source='alternative')
        self.assertEqual(po.baf_customer_account_number, 'E3BF')

    def test_po_alternative_falls_back_when_customer_has_no_alt(self):
        po = self._make_po(self.customer_no_alt, source='alternative')
        self.assertEqual(po.baf_customer_account_number, '10099')

    def test_po_switching_source_recomputes_number(self):
        po = self._make_po(self.customer, source='default')
        self.assertEqual(po.baf_customer_account_number, '10012')
        po.baf_customer_account_source = 'alternative'
        self.assertEqual(po.baf_customer_account_number, 'E3BF')
        po.baf_customer_account_source = 'default'
        self.assertEqual(po.baf_customer_account_number, '10012')

    def test_po_without_sale_order_has_empty_number(self):
        po = self.env['purchase.order'].create({'partner_id': self.vendor.id})
        self.assertEqual(po.baf_customer_account_number, '')

    def _grouped_xlsx_headers_and_rows(self, po):
        po.action_send_grouped_po_email()
        attachment = self.env['ir.attachment'].search(
            [('res_model', '=', 'purchase.order'), ('res_id', '=', po.id)],
            order='id desc', limit=1,
        )
        self.assertTrue(attachment, "grouped-PO XLSX attachment was not created")
        wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(attachment.datas)))
        ws = wb['Standard Orders']
        rows = list(ws.iter_rows(values_only=True))
        return rows[0], rows[1:]

    def _po_with_one_line(self, customer, source='default'):
        product = self.env['product.template'].create({
            'name': 'Alt Test Part',
            'list_price': 10.0,
            'sale_ok': True,
            'purchase_ok': True,
        })
        po = self._make_po(customer, source=source)
        self.env['purchase.order.line'].create({
            'order_id': po.id,
            'product_id': product.product_variant_id.id,
            'product_qty': 1.0,
            'price_unit': 10.0,
        })
        return po

    def test_xlsx_includes_customer_number_column_for_trusted_vendor(self):
        po = self._po_with_one_line(self.customer, source='alternative')
        headers, rows = self._grouped_xlsx_headers_and_rows(po)
        self.assertIn('Customer', headers)
        self.assertIn('Customer #', headers)
        col = headers.index('Customer #')
        self.assertEqual(rows[0][col], 'E3BF')

    def test_xlsx_omits_customer_columns_for_untrusted_vendor(self):
        self.vendor.is_trusted_vendor = False
        po = self._po_with_one_line(self.customer, source='alternative')
        headers, _rows = self._grouped_xlsx_headers_and_rows(po)
        self.assertNotIn('Customer', headers)
        self.assertNotIn('Customer #', headers)
