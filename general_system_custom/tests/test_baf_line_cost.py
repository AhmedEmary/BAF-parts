from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBafLineCost(TransactionCase):
    """Line Cost comes from the BAF pricing engine, and an unresolvable cost
    names its own reason instead of silently reading 0."""

    def setUp(self):
        super().setUp()
        Partner = self.env['res.partner']
        self.Disc = self.env['baf.discount.line']

        # Named BMW so the engine resolves a real column key (BMW_T12).
        self.brand = self.env['product.brand'].create({'name': 'BMW'})
        self.orphan_brand = self.env['product.brand'].create({'name': 'LC-ORPHAN'})

        self.tmpl = self.env['product.template'].create({
            'name': 'LC Part', 'sku': 'LC-001', 'brand': self.brand.id,
            'list_price': 100.0, 'baf_discount_code': '10', 'baf_type_code': 1,
        })
        self.product = self.tmpl.product_variant_id

        self.orphan = self.env['product.template'].create({
            'name': 'LC Orphan Part', 'sku': 'LC-002',
            'brand': self.orphan_brand.id, 'list_price': 100.0,
        }).product_variant_id

        # Matrix vendor, 25% off 100.00 -> 75.00, delivers in 3-4 weeks.
        self.vendor_matrix = Partner.create({
            'name': 'LC Matrix Vendor', 'baf_is_vendor': True,
            'baf_purchase_method': 'matrix', 'baf_delivery_weeks': 3,
            'baf_brand_ids': [(6, 0, [self.brand.id])],
        })
        self.assertTrue(self.tmpl.baf_column_key,
                        "fixture: the brand must resolve to a column key")
        self.Disc.create({
            'table_type': 'purchase', 'column_key': self.tmpl.baf_column_key,
            'discount_code': '10', 'discount_pct': 25.0,
            'partner_id': self.vendor_matrix.id,
        })

        # Direct vendor at 60.00, delivers in 1-2 weeks so it wins by default.
        self.vendor_direct = Partner.create({
            'name': 'LC Direct Vendor', 'baf_is_vendor': True,
            'baf_purchase_method': 'direct', 'baf_delivery_weeks': 1,
            'baf_direct_sale_markup_pct': 20.0,
            'baf_brand_ids': [(6, 0, [self.brand.id])],
        })
        self.env['product.supplierinfo'].create({
            'partner_id': self.vendor_direct.id,
            'product_tmpl_id': self.tmpl.id, 'price': 60.0,
        })

        self.customer = Partner.create({'name': 'LC Customer'})
        self.website = self.env['website'].search([], limit=1)

    def _order(self, website=False):
        vals = {'partner_id': self.customer.id}
        if website:
            vals['website_id'] = self.website.id
        return self.env['sale.order'].create(vals)

    def _line(self, order, product=None, qty=1.0):
        return self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': (product or self.product).id,
            'product_uom_qty': qty,
        })

    # ── Costing vendor ───────────────────────────────────────────────────────

    def test_cost_uses_selected_vendor(self):
        line = self._line(self._order())
        line.purchase_vendor_id = self.vendor_matrix
        self.assertEqual(line.baf_cost_status, 'ok')
        self.assertAlmostEqual(line.purchase_price, 75.0, places=2)

    def test_cost_falls_back_to_best_vendor_on_webshop_line(self):
        line = self._line(self._order(website=True))
        self.assertFalse(line.purchase_vendor_id)
        self.assertEqual(line.baf_cost_status, 'ok')
        self.assertAlmostEqual(line.purchase_price, 60.0, places=2)

    def test_cost_follows_a_vendor_change(self):
        line = self._line(self._order())
        line.purchase_vendor_id = self.vendor_direct
        self.assertAlmostEqual(line.purchase_price, 60.0, places=2)
        line.purchase_vendor_id = self.vendor_matrix
        self.assertAlmostEqual(line.purchase_price, 75.0, places=2)

    def test_alternative_vendor_drives_cost(self):
        line = self._line(self._order(website=True))
        line.baf_alt_vendor_id = self.vendor_matrix
        self.assertEqual(line.purchase_vendor_id, self.vendor_matrix)
        self.assertAlmostEqual(line.purchase_price, 75.0, places=2)

    # ── The three gaps ───────────────────────────────────────────────────────

    def test_no_eligible_vendor(self):
        line = self._line(self._order(), product=self.orphan)
        self.assertEqual(line.baf_cost_status, 'no_vendor')
        self.assertEqual(line.purchase_price, 0.0)

    def test_selected_vendor_cannot_price_the_part(self):
        self.Disc.search([
            ('partner_id', '=', self.vendor_matrix.id),
            ('discount_code', '=', '10'),
        ]).unlink()
        line = self._line(self._order())
        line.purchase_vendor_id = self.vendor_matrix
        self.assertEqual(line.baf_cost_status, 'no_price')
        self.assertEqual(line.purchase_price, 0.0)

    def test_no_vendor_inside_the_customer_delivery_window(self):
        self.customer.baf_max_delivery_weeks = 1
        self.vendor_direct.baf_delivery_weeks = 5
        self.vendor_matrix.baf_delivery_weeks = 5
        line = self._line(self._order(website=True))
        self.assertEqual(line.baf_cost_status, 'no_delivery_window')
        self.assertEqual(line.purchase_price, 0.0)

    # ── Cost is engine-only ──────────────────────────────────────────────────

    def test_cost_is_not_user_editable(self):
        """Cost is whatever the vendor charges, so there is nothing for a user
        to override: the purchase order would disagree with any typed figure."""
        field = self.env['sale.order.line']._fields['purchase_price']
        self.assertTrue(field.readonly)
        self.assertFalse(field.inverse)

    def test_no_manual_cost_state_exists(self):
        line = self._line(self._order())
        states = dict(line._fields['baf_cost_status'].selection)
        self.assertNotIn('manual', states)

    # ── Margin ───────────────────────────────────────────────────────────────

    def test_gap_line_reports_no_margin_rather_than_full_subtotal(self):
        order = self._order()
        line = self._line(order, product=self.orphan)
        line.price_unit = 200.0
        self.assertEqual(line.baf_cost_status, 'no_vendor')
        self.assertEqual(line.margin, 0.0)
        self.assertEqual(line.margin_percent, 0.0)

    def test_order_margin_counts_costed_lines_only(self):
        order = self._order()
        costed = self._line(order)
        costed.purchase_vendor_id = self.vendor_matrix
        costed.price_unit = 100.0
        gap = self._line(order, product=self.orphan)
        gap.price_unit = 200.0
        self.assertAlmostEqual(costed.margin, 25.0, places=2)
        self.assertAlmostEqual(order.margin, 25.0, places=2)

    # ── Order summary and PO guard ───────────────────────────────────────────

    def test_order_summarises_gaps_without_blocking(self):
        order = self._order()
        self._line(order, product=self.orphan)
        self._line(order, product=self.orphan)
        costed = self._line(order)
        costed.purchase_vendor_id = self.vendor_matrix
        self.assertEqual(order.baf_cost_gap_count, 2)
        self.assertIn('no eligible vendor', order.baf_cost_gap_summary)
        self.assertIn('2 lines', order.baf_cost_gap_summary)
        order.action_confirm()
        self.assertEqual(order.state, 'sale')

    def test_summary_names_each_cause_separately(self):
        order = self._order()
        self._line(order, product=self.orphan)          # no_vendor
        cannot_price = self._line(order)                # no_price
        self.Disc.search([
            ('partner_id', '=', self.vendor_matrix.id),
            ('discount_code', '=', '10'),
        ]).unlink()
        cannot_price.purchase_vendor_id = self.vendor_matrix
        self.assertEqual(order.baf_cost_gap_count, 2)
        self.assertIn('1 no eligible vendor', order.baf_cost_gap_summary)
        self.assertIn('1 vendor cannot price', order.baf_cost_gap_summary)

    def test_single_gap_reads_as_one_line(self):
        order = self._order()
        self._line(order, product=self.orphan)
        self.assertEqual(order.baf_cost_gap_count, 1)
        self.assertIn('1 line without cost', order.baf_cost_gap_summary)
        self.assertNotIn('lines', order.baf_cost_gap_summary)

    def test_order_margin_is_still_summed_from_costed_lines(self):
        # The total is hidden in the view while gaps exist, not zeroed: the
        # stored field still feeds sale.report and the pivot views.
        order = self._order()
        costed = self._line(order)
        costed.purchase_vendor_id = self.vendor_matrix
        costed.price_unit = 100.0
        self._line(order, product=self.orphan)
        self.assertEqual(order.baf_cost_gap_count, 1)
        self.assertAlmostEqual(order.margin, 25.0, places=2)

    def test_po_creation_refuses_a_part_the_vendor_cannot_price(self):
        """A purchase order is an outward commitment: rather than send a vendor
        an invented price, refuse and make someone extend the discount table."""
        self.Disc.search([
            ('partner_id', '=', self.vendor_matrix.id),
            ('discount_code', '=', '10'),
        ]).unlink()
        order = self._order()
        line = self._line(order, qty=5.0)
        line.purchase_vendor_id = self.vendor_matrix
        order.action_confirm()
        before = self.env['purchase.order'].search_count([])
        with self.assertRaises(UserError):
            line.action_create_purchase_order()
        self.assertEqual(self.env['purchase.order'].search_count([]), before)

    def test_po_creation_uses_the_engine_price(self):
        order = self._order()
        line = self._line(order, qty=5.0)
        line.purchase_vendor_id = self.vendor_matrix
        order.action_confirm()
        line.action_create_purchase_order()
        pol = self.env['purchase.order.line'].search([
            ('order_id.sale_order_id', '=', order.id)])
        self.assertEqual(len(pol), 1)
        self.assertAlmostEqual(pol.price_unit, 75.0, places=2)
        self.assertFalse(pol.baf_cost_unknown)
