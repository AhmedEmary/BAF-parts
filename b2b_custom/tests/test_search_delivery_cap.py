from odoo.tests import TransactionCase, tagged

from odoo.addons.b2b_custom.controllers.baf_b2b import _expand_product_options


@tagged('post_install', '-at_install')
class TestSearchDeliveryCap(TransactionCase):
    """The B2B part-search results must hide alternative direct vendors whose
    delivery frame exceeds the customer's Max Delivery Weeks cap. The cap lives
    on _baf_alternative_direct_vendors; this guards that _expand_product_options
    actually passes the partner through so the cap takes effect on the results
    page (regression: it used to call it without the partner)."""

    def setUp(self):
        super().setUp()
        Partner = self.env['res.partner']
        Brand = self.env['product.brand']
        Template = self.env['product.template']

        self.brand = Brand.create({'name': 'SDC-BRAND', 'is_public': True})
        self.template = Template.create({
            'name': 'SDC Test Part',
            'sku': 'SDC-001',
            'brand': self.brand.id,
            'list_price': 100.0,
            'sale_ok': True,
        })
        self.product = self.template.product_variant_id

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
                'product_tmpl_id': self.template.id,
                'price': price,
            })
            return vendor

        # Both cheaper than the 100.0 default, so both would otherwise appear.
        self.vendor_fast = _make_vendor('SDC Fast (1-2w)', 1, 40.0)
        self.vendor_slow = _make_vendor('SDC Slow (3-4w)', 3, 30.0)

        self.capped = Partner.create(
            {'name': 'SDC Capped', 'baf_max_delivery_weeks': 2})
        self.uncapped = Partner.create({'name': 'SDC Uncapped'})

    def _alt_vendor_ids(self, partner):
        rows = _expand_product_options(self.product, partner)
        return {r['alt_vendor_id'] for r in rows if r.get('alt_vendor_id')}

    def test_capped_customer_hides_slow_vendor(self):
        ids = self._alt_vendor_ids(self.capped)
        self.assertIn(self.vendor_fast.id, ids)
        self.assertNotIn(self.vendor_slow.id, ids)

    def test_uncapped_customer_sees_all_vendors(self):
        ids = self._alt_vendor_ids(self.uncapped)
        self.assertIn(self.vendor_fast.id, ids)
        self.assertIn(self.vendor_slow.id, ids)
