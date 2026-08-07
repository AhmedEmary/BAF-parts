import base64
import io

import openpyxl

from odoo.tests import TransactionCase, tagged


def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue())


@tagged('post_install', '-at_install')
class TestDirectImportAmbiguousResolution(TransactionCase):
    """Duplicate-SKU rows are stashed for staff review; a brand column in
    the file auto-resolves rows where it uniquely picks one candidate."""

    def setUp(self):
        super().setUp()
        self.Tmpl = self.env['product.template']
        self.Brand = self.env['product.brand']
        self.Sup = self.env['product.supplierinfo']
        self.Amb = self.env['baf.direct.import.ambiguous']

        self.vendor = self.env['res.partner'].create({
            'name': 'AMB Vendor', 'baf_purchase_method': 'direct',
        })
        self.bmw = self.Brand.create({'name': 'BMW'})
        self.mini = self.Brand.create({'name': 'MINI'})

        # A duplicate SKU under two brands.
        self.p_bmw = self.Tmpl.create({
            'name': 'BMW brake pad', 'default_code': 'AMB-BMW-1',
            'sku': 'AMB-DUP-1', 'brand': self.bmw.id, 'list_price': 100.0,
        })
        self.p_mini = self.Tmpl.create({
            'name': 'MINI brake pad', 'default_code': 'AMB-MINI-1',
            'sku': 'AMB-DUP-1', 'brand': self.mini.id, 'list_price': 100.0,
        })
        # A non-ambiguous SKU on the same vendor to sanity-check we don't
        # break the fast path.
        self.p_clean = self.Tmpl.create({
            'name': 'BMW filter', 'default_code': 'AMB-BMW-2',
            'sku': 'AMB-UNIQUE', 'brand': self.bmw.id, 'list_price': 100.0,
        })

    def _run(self, rows):
        self.vendor.baf_purchase_method = 'direct'
        self.vendor.baf_pricing_file = _xlsx(rows)
        self.vendor.baf_pricing_filename = 'f.xlsx'
        return self.vendor.action_import_vendor_pricing_file()

    def test_ambiguous_sku_without_brand_column_gets_stashed(self):
        self._run([['SKU', 'Discounted Price'], ['AMB-DUP-1', '65']])
        stash = self.Amb.search([('partner_id', '=', self.vendor.id)])
        self.assertEqual(len(stash), 1)
        self.assertEqual(stash.sku, 'AMB-DUP-1')
        self.assertEqual(stash.price, 65.0)
        self.assertFalse(stash.file_brand)
        # Both candidate templates were captured.
        self.assertEqual(
            set(stash.candidate_ids.ids), {self.p_bmw.id, self.p_mini.id})
        # No supplierinfo row was inserted while it stays unresolved.
        self.assertFalse(self.Sup.search([
            ('partner_id', '=', self.vendor.id),
            ('product_tmpl_id', 'in', (self.p_bmw + self.p_mini).ids),
        ]))

    def test_file_brand_uniquely_resolves_ambiguous_row(self):
        self._run([
            ['SKU', 'Discounted Price', 'Brand'],
            ['AMB-DUP-1', '65', 'MINI'],
        ])
        # No stash — the brand column picked MINI.
        self.assertFalse(self.Amb.search(
            [('partner_id', '=', self.vendor.id)]))
        # The MINI product got the price, BMW did not.
        self.assertEqual(
            self.Sup.search([
                ('partner_id', '=', self.vendor.id),
                ('product_tmpl_id', '=', self.p_mini.id),
            ]).price, 65.0)
        self.assertFalse(self.Sup.search([
            ('partner_id', '=', self.vendor.id),
            ('product_tmpl_id', '=', self.p_bmw.id),
        ]))

    def test_non_ambiguous_row_still_imports_alongside_ambiguous(self):
        self._run([
            ['SKU', 'Discounted Price'],
            ['AMB-UNIQUE', '50'],
            ['AMB-DUP-1', '65'],
        ])
        self.assertEqual(
            self.Sup.search([
                ('partner_id', '=', self.vendor.id),
                ('product_tmpl_id', '=', self.p_clean.id),
            ]).price, 50.0)
        self.assertEqual(len(self.Amb.search(
            [('partner_id', '=', self.vendor.id)])), 1)

    def test_resolve_action_imports_only_rows_with_a_brand_pick(self):
        self._run([['SKU', 'Discounted Price'], ['AMB-DUP-1', '65']])
        stash = self.Amb.search([('partner_id', '=', self.vendor.id)])
        stash.selected_brand_id = self.mini.id
        result = self.vendor.action_resolve_direct_import_ambiguous()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(
            self.Sup.search([
                ('partner_id', '=', self.vendor.id),
                ('product_tmpl_id', '=', self.p_mini.id),
            ]).price, 65.0)
        # Stash row is consumed.
        self.assertFalse(self.Amb.search(
            [('partner_id', '=', self.vendor.id)]))

    def test_resolve_action_warns_when_no_brand_picked(self):
        self._run([['SKU', 'Discounted Price'], ['AMB-DUP-1', '65']])
        result = self.vendor.action_resolve_direct_import_ambiguous()
        self.assertEqual(result['params']['type'], 'warning')

    def test_new_import_wipes_the_previous_stash(self):
        self._run([['SKU', 'Discounted Price'], ['AMB-DUP-1', '65']])
        self.assertTrue(self.Amb.search([('partner_id', '=', self.vendor.id)]))
        # Second import with no duplicates → previous stash cleared.
        self._run([['SKU', 'Discounted Price'], ['AMB-UNIQUE', '77']])
        self.assertFalse(self.Amb.search([('partner_id', '=', self.vendor.id)]))
