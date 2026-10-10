import datetime
import io

import openpyxl
from odoo import Command
from odoo.tests import TransactionCase, tagged

from odoo.addons.general_system_custom.models.supplier_status_entry import parse_date, parse_status

TODAY = datetime.date(2026, 10, 10)


@tagged('post_install', '-at_install')
class TestSupplierStatusImport(TransactionCase):
    """Supplier confirmations go through Odoo's standard import into
    baf.supplier.status.entry; the columns are mapped like any import."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env['res.partner'].create({'name': 'Kalkan Test'})
        cls.customer = cls.env['res.partner'].create({'name': 'Customer'})
        cls.brand = cls.env['product.brand'].create({'name': 'BMW'})
        cls.products = {}
        for sku in ('51711910956', '51718208483', '51718232894', '52101924801',
                    '51711916199', '61119141621', '16116754637'):
            cls.products[sku] = cls.env['product.product'].create({
                'name': f'Part {sku}', 'type': 'consu', 'brand': cls.brand.id,
                'sku': sku, 'default_code': f'BMW_{sku}',
            })
        cls.so = cls.env['sale.order'].create({
            'partner_id': cls.customer.id,
            'order_line': [Command.create({'product_id': p.id, 'product_uom_qty': 2})
                           for p in cls.products.values()],
        })
        cls.po = cls.env['purchase.order'].create({
            'partner_id': cls.vendor.id,
            'sale_order_id': cls.so.id,
            'order_line': [Command.create({'product_id': p.id, 'product_qty': 2, 'price_unit': 10})
                           for p in cls.products.values()],
        })

    def _line(self, sku):
        return self.po.order_line.filtered(lambda l: l.product_id.sku == sku)

    def _import(self, rows, mapping, has_headers=False, context=None):
        """Run base_import as the import screen does. `mapping` gives the
        field per column (False = ignored), as picked by the user."""
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(row)
        out = io.BytesIO()
        wb.save(out)
        if context is None:
            context = self.po.action_import_supplier_status()['params']['context']
        importer = self.env['base_import.import'].with_context(**context).create({
            'res_model': 'baf.supplier.status.entry',
            'file': out.getvalue(),
            'file_name': 'confirmation.xlsx',
            'file_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        result = importer.execute_import(mapping, [False] * len(mapping), {
            'has_headers': has_headers, 'quoting': '"', 'separator': '', 'encoding': '',
            'date_format': '%d.%m.%Y', 'datetime_format': '',
            'float_thousand_separator': ',', 'float_decimal_separator': '.',
            'import_skip_records': [], 'import_set_empty_fields': [], 'fallback_values': {},
            'name_create_enabled_fields': {}, 'advanced': True, 'keep_matches': False,
            'limit': 2000, 'skip': 0,
        })
        self.assertFalse(result['messages'], result['messages'])
        return self.env['baf.supplier.status.entry'].browse(result['ids'])

    def test_layout_a_from_po(self):
        """First screenshot, imported from the PO: part number in E, remark in
        Q; the file has no order number."""
        mapping = [False] * 18
        mapping[4] = 'part_number'
        mapping[16] = 'remark'
        entries = self._import([
            [80, None, 'BMW', 1, '51711910956', None, 10, 'Halteklammer', 'Tespit klipsi', 0.94,
             15, None, 10, 0.002, 0.84, 8.4, 1, 'DE'],
            [432, None, 'BMW', 1, '51718208483', None, None, 'Abdeckung Windlauf', 'Kapak', 94.24,
             15, None, 0, 0.68, 83.87, 0, '3x Rückstand', None],
            [379, None, 'BMW', 1, '51718232894', None, None, 'Abdeckung Windlauf', 'Kapak', 84.80,
             15, None, 0, 0.68, 75.47, 0, '1x Rückstand LT 20.11.2026', None],
            [121, None, 'BMW', 1, '52101924801', None, None, 'Griff aussen links', 'Tutamak', 13.48,
             17, None, 5, 0.017, 11.73, 0, 'nicht bestellbar', None],
            [7, None, 'MINI', 1, '61119158050', None, 1, 'Not on this PO', 'x', 26.86,
             23, None, 0, 0.12, 19.85, 0, 'Entfallen', None],
        ], mapping)
        self.assertEqual(entries.mapped('purchase_id'), self.po)
        self.assertFalse(self._line('51711910956').baf_supplier_status)
        self.assertEqual(self._line('51718208483').baf_supplier_status, 'backorder')
        self.assertEqual(self._line('51718208483').baf_supplier_note, '3x Rückstand')
        self.assertEqual(self._line('51718232894').baf_eta, datetime.date(2026, 11, 20))
        self.assertEqual(self._line('52101924801').baf_supplier_status, 'not_orderable')
        self.assertEqual(entries[-1].result, 'not_found')
        body = self.po.message_ids[0].body
        self.assertIn('51711916199', body, "PO lines missing from the file are listed.")
        self.assertIn('61119158050', body, "Rows for parts not on the PO are listed.")

    def test_layout_b_numeric_part_and_date_column(self):
        """Second screenshot: shifted columns, numeric part number, remark in
        O, plus a mapped delivery date column."""
        mapping = [False] * 17
        mapping[4] = 'part_number'
        mapping[14] = 'remark'
        mapping[16] = 'eta'
        self._import([
            [3, 'Motorrad', 'BMW', 1, 16116754637, 1, 'Halteband', 4.29, 32, None, 10, 0.001,
             2.6, 2.6, 1, 'DE', None],
            [9, None, 'BMW', 3, 51711916199, None, 'Klammer', 66.39, 27, None, 0, 0.04,
             47.98, 0, 'Entfallen', None, None],
            [6, None, 'MINI', 1, 61119141621, 1, 'Rep.-Kabel', 47, 23, None, 0, 0.213,
             34.74, 34.74, 1, 'RO', '01.12.2026'],
        ], mapping)
        self.assertFalse(self._line('16116754637').baf_supplier_status)
        self.assertEqual(self._line('51711916199').baf_supplier_status, 'nla')
        self.assertEqual(self._line('61119141621').baf_supplier_status, 'backorder')
        self.assertEqual(self._line('61119141621').baf_eta, datetime.date(2026, 12, 1))

    def test_general_file_with_headers_and_so_column(self):
        """A file that names the sales order and the status itself, imported
        from the log menu (no PO in context)."""
        self._import([
            ['Order', 'Article', 'Status', 'Delivery'],
            [self.so.name, '51 71 8 208 483', 'Backorder', '15.01.2027'],
            [self.so.name, '51711910956', 'NLA', None],
        ], ['sale_order_id', 'part_number', 'status', 'eta'], has_headers=True, context={})
        self.assertEqual(self._line('51718208483').baf_supplier_status, 'backorder')
        self.assertEqual(self._line('51718208483').baf_eta, datetime.date(2027, 1, 15))
        self.assertEqual(self._line('51711910956').baf_supplier_status, 'nla')

    def test_reimport_clears_resolved_backorder(self):
        self._import([['51718208483', '3x Rückstand']], ['part_number', 'remark'])
        self.assertEqual(self._line('51718208483').baf_supplier_status, 'backorder')
        self._import([['51718208483', 2]], ['part_number', 'remark'])
        self.assertFalse(self._line('51718208483').baf_supplier_status)

    def test_remark_parsing(self):
        self.assertEqual(parse_status('Entfallen'), 'nla')
        self.assertEqual(parse_status('3x Rückstand'), 'backorder')
        self.assertFalse(parse_status('DE'))
        self.assertEqual(parse_date('Rückstand KW 47/2026', TODAY), datetime.date(2026, 11, 16))
        self.assertEqual(parse_date('Rückstand LT 15.11.', TODAY), datetime.date(2026, 11, 15))
        self.assertEqual(parse_date('Rückstand 05.01.', TODAY), datetime.date(2027, 1, 5))
        self.assertIsNone(parse_date('36.09', TODAY))
        self.assertIsNone(parse_date('12.24.', TODAY), "No remark: a bare 12.24. is not a date.")
        self.assertIsNone(parse_date('Rückstand 31.02.2027', TODAY))

    def test_backorder_feedback(self):
        """/backorders reads the worst status and the latest ETA of the PO
        lines for the product, also on draft POs."""
        self._import([['51718232894', 'Rückstand 20.11.2026']], ['part_number', 'remark'])
        product = self.products['51718232894']
        self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'sale_order_id': self.so.id,
            'order_line': [Command.create({
                'product_id': product.id, 'product_qty': 1, 'price_unit': 10,
                'baf_supplier_status': 'nla', 'baf_eta': datetime.date(2026, 10, 30),
            })],
        })
        so_line = self.so.order_line.filtered(lambda l: l.product_id == product)
        self.assertEqual(so_line.baf_supplier_status, 'nla')
        self.assertEqual(so_line.baf_supplier_eta, datetime.date(2026, 11, 20))
        other = self.so.order_line.filtered(lambda l: l.product_id.sku == '51711910956')
        self.assertFalse(other.baf_supplier_status)
