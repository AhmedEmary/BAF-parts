from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCustomerDeliveryWindow(TransactionCase):
    """baf_get_best_vendor ignores the customer's delivery cap: ranking is
    always shortest-delivery-first, cheapest-second. The cap only shapes
    the webshop's alternative-direct-vendor list (_baf_alternative_direct_vendors)."""

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

        # A slow&cheap + B fast&expensive + C fast&mid — the PDF example.
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

        self.capped = Partner.create({
            'name': 'DW Capped Customer',
            'baf_max_delivery_weeks': 2,
        })
        self.uncapped = Partner.create({'name': 'DW Uncapped Customer'})

    def _best(self, customer=None):
        return self.product.product_variant_id.baf_get_best_vendor(
            customer=customer)

    def test_uncapped_customer_keeps_fastest_first_ranking(self):
        winner = self._best(customer=self.uncapped)
        self.assertEqual(winner['vendor'], self.vendor_fast_mid)

    def test_capped_customer_gets_same_winner_as_uncapped(self):
        # The cap does not filter the picker: same ranking either way.
        self.assertEqual(
            self._best(customer=self.capped)['vendor'],
            self._best(customer=self.uncapped)['vendor'],
        )

    def test_capped_customer_still_picks_fastest_cheapest(self):
        # Among {A slow&cheap, B fast&exp, C fast&mid}, shortest-first
        # eliminates A; cheapest at 1-2 weeks picks C.
        winner = self._best(customer=self.capped)
        self.assertEqual(winner['vendor'], self.vendor_fast_mid)

    def test_all_priced_candidates_appear(self):
        # Every eligible vendor with a price must show up as a candidate.
        result = self._best(customer=self.capped)
        vendor_ids = {c['vendor'].id for c in result['candidates']}
        self.assertIn(self.vendor_slow.id, vendor_ids)
        self.assertIn(self.vendor_fast_exp.id, vendor_ids)
        self.assertIn(self.vendor_fast_mid.id, vendor_ids)

    def test_alt_direct_vendors_respect_cap(self):
        # The webshop's direct-vendor offer list still filters by cap.
        default = self.product.baf_get_sales_price(partner=self.uncapped)
        options_capped = self.product._baf_alternative_direct_vendors(
            default, partner=self.capped)
        vendor_ids = {o['vendor_id'] for o in options_capped}
        self.assertNotIn(self.vendor_slow.id, vendor_ids)

    def test_alt_direct_vendors_uncapped_unchanged(self):
        default = self.product.baf_get_sales_price(partner=self.uncapped)
        options = self.product._baf_alternative_direct_vendors(
            default, partner=self.uncapped)
        for opt in options:
            self.assertIn('delivery_lower', opt)
            self.assertIn('price', opt)

    def test_sale_order_line_picks_best_vendor_regardless_of_cap(self):
        so = self.env['sale.order'].create({'partner_id': self.capped.id})
        line = self.env['sale.order.line'].create({
            'order_id': so.id,
            'product_id': self.product.product_variant_id.id,
            'product_uom_qty': 1.0,
        })
        self.assertEqual(line.purchase_vendor_id, self.vendor_fast_mid)

    def test_compare_prices_wizard_matches_line_auto_selection(self):
        # The Compare Prices wizard must preselect the same vendor the
        # SO-line compute picks — capped customer.
        so = self.env['sale.order'].create({'partner_id': self.capped.id})
        line = self.env['sale.order.line'].create({
            'order_id': so.id,
            'product_id': self.product.product_variant_id.id,
            'product_uom_qty': 1.0,
        })
        wizard = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id).create({})
        self.assertEqual(wizard.selected_vendor_id, line.purchase_vendor_id)

    def test_wizard_and_line_agree_when_customer_has_no_cap(self):
        so = self.env['sale.order'].create({'partner_id': self.uncapped.id})
        line = self.env['sale.order.line'].create({
            'order_id': so.id,
            'product_id': self.product.product_variant_id.id,
            'product_uom_qty': 1.0,
        })
        wizard = self.env['baf.vendor.price.compare'].with_context(
            default_sale_line_id=line.id).create({})
        self.assertEqual(wizard.selected_vendor_id, line.purchase_vendor_id)
