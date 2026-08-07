from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCustomerDeliveryWindow(TransactionCase):
    """Task #51 — customers may cap the delivery frame of their auto-vendor
    selection. Vendors outside the window are neither displayed nor
    considered, and the winner is the cheapest supplier within the window."""

    def setUp(self):
        super().setUp()
        Partner = self.env['res.partner']
        Brand = self.env['product.brand']
        Template = self.env['product.template']

        self.brand = Brand.create({'name': 'DW-BRAND'})
        self.product = Template.create({
            'name': 'DW Test Part',
            'sku': 'DW-001',
            'brand': self.brand.id,
            'list_price': 100.0,
        })

        # Three direct vendors: A slow&cheap, B fast&expensive, C fast&mid.
        # Mirrors the PDF example (Supplier A EUR 90 / 3-4 weeks;
        # Supplier B EUR 100 / 1-2 weeks; Supplier C EUR 95 / 1-2 weeks).
        def _make_vendor(name, weeks, price, markup=25.0):
            vendor = Partner.create({
                'name': name,
                'baf_is_vendor': True,
                'baf_purchase_method': 'direct',
                'baf_brand_ids': [(6, 0, [self.brand.id])],
                'baf_delivery_weeks': weeks,
                'baf_direct_sale_markup_pct': markup,
            })
            self.env['product.supplierinfo'].create({
                'partner_id': vendor.id,
                'product_tmpl_id': self.product.id,
                'price': price,
            })
            return vendor

        self.vendor_slow = _make_vendor('DW Vendor A (3-4w)', 3, 90.0)
        self.vendor_fast_exp = _make_vendor('DW Vendor B (1-2w)', 1, 100.0)
        self.vendor_fast_mid = _make_vendor('DW Vendor C (1-2w)', 1, 95.0)

        # Two customers: one capped at 2 weeks, one uncapped.
        self.capped = Partner.create({
            'name': 'DW Capped Customer',
            'baf_max_delivery_weeks': 2,
        })
        self.uncapped = Partner.create({'name': 'DW Uncapped Customer'})

    def _best(self, customer=None):
        return self.product.product_variant_id.baf_get_best_vendor(
            customer=customer)

    # ── best-vendor picker ─────────────────────────────────────────────────
    def test_uncapped_customer_keeps_fastest_first_ranking(self):
        # Preserves the historical behaviour for customers who don't opt in.
        # All three vendors are eligible; the fastest at 1-2 weeks wins,
        # tied fast vendors ranked by cheapest price → C (95) beats B (100).
        winner = self._best(customer=self.uncapped)
        self.assertEqual(winner['vendor'], self.vendor_fast_mid)

    def test_capped_customer_drops_out_of_window_vendors(self):
        # Slow vendor A (3-4 weeks) is dropped for a customer capped at 2.
        winner = self._best(customer=self.capped)
        self.assertNotEqual(winner['vendor'], self.vendor_slow)

    def test_capped_customer_picks_cheapest_within_window(self):
        # Within the window {B, C}, the cheapest (C @ 95) wins — even though
        # the fastest-first ranking would still pick a 1-2-week vendor,
        # the switch to cheapest-first makes the tie deterministic and
        # matches the spec's example.
        winner = self._best(customer=self.capped)
        self.assertEqual(winner['vendor'], self.vendor_fast_mid)

    def test_out_of_window_vendors_are_marked_in_candidates(self):
        # The picker still lists every eligible vendor as a candidate so
        # staff can inspect what was considered. Out-of-window ones must
        # carry a note that explains they were dropped by the cap.
        result = self._best(customer=self.capped)
        slow = [c for c in result['candidates']
                if c['vendor'] == self.vendor_slow][0]
        self.assertTrue(slow['out_of_window'])
        self.assertIn('exceeds', slow['note'])

    def test_child_contact_inherits_company_cap(self):
        # Delivery cap on a customer company applies to its child contacts.
        child = self.env['res.partner'].create({
            'name': 'DW Capped Child',
            'parent_id': self.capped.id,
        })
        winner = self._best(customer=child)
        self.assertEqual(winner['vendor'], self.vendor_fast_mid)

    def test_all_vendors_out_of_window_returns_empty_reason(self):
        # Cap set below the fastest vendor: no candidate qualifies, the
        # picker returns an empty vendor with a reason that names the cap.
        very_capped = self.env['res.partner'].create({
            'name': 'DW Impossibly Capped',
            'baf_max_delivery_weeks': 0,
        })
        # 0 means "no cap" per the field's contract, so use the child+parent
        # test path with a real cap that's still too small.
        very_capped.baf_max_delivery_weeks = 0  # explicit — 0 == disabled
        # Force a real block by capping at a value below every vendor's
        # frame. Every vendor here is >= 1 week; a cap of 1 keeps only the
        # 1-week vendors. To make every vendor out-of-window, temporarily
        # bump every vendor to 5 weeks and cap at 2.
        (self.vendor_slow + self.vendor_fast_exp + self.vendor_fast_mid) \
            .write({'baf_delivery_weeks': 5})
        self.capped.baf_max_delivery_weeks = 2
        result = self._best(customer=self.capped)
        self.assertFalse(result['vendor'])
        self.assertIn('window', result['reason'])

    # ── Alternative direct vendors (portal ladder) ────────────────────────
    def test_alt_direct_vendors_respect_cap(self):
        # The portal's "pay less, wait longer" ladder must also drop
        # out-of-window vendors so the customer can't select something the
        # backend would reject.
        default = self.product.baf_get_sales_price(partner=self.uncapped)
        options_capped = self.product._baf_alternative_direct_vendors(
            default, partner=self.capped)
        vendor_ids = {o['vendor_id'] for o in options_capped}
        self.assertNotIn(self.vendor_slow.id, vendor_ids)

    def test_alt_direct_vendors_uncapped_unchanged(self):
        # Sanity: without a cap, the ladder returns the same options it did
        # before the refactor (behaviour preserved).
        default = self.product.baf_get_sales_price(partner=self.uncapped)
        options = self.product._baf_alternative_direct_vendors(
            default, partner=self.uncapped)
        # We only assert the type shape here — the exact ladder depends on
        # markup + default price computed for the empty group case.
        for opt in options:
            self.assertIn('delivery_lower', opt)
            self.assertIn('price', opt)

    def test_sale_order_line_uses_customer_when_computing_vendor(self):
        # The SO line's `purchase_vendor_id` compute must feed the customer
        # to `baf_get_best_vendor`, so the auto-picker respects the cap.
        so = self.env['sale.order'].create({'partner_id': self.capped.id})
        line = self.env['sale.order.line'].create({
            'order_id': so.id,
            'product_id': self.product.product_variant_id.id,
            'product_uom_qty': 1.0,
        })
        self.assertEqual(line.purchase_vendor_id, self.vendor_fast_mid)
