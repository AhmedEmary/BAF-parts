from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWebsiteDefaultLineVendor(TransactionCase):
    """A webshop line without a customer-chosen vendor is the default option
    and must not be auto-sourced from a direct vendor the customer skipped."""

    def setUp(self):
        super().setUp()
        Partner = self.env['res.partner']
        self.brand = self.env['product.brand'].create({'name': 'WDL-BRAND'})
        self.tmpl = self.env['product.template'].create({
            'name': 'WDL Part',
            'sku': 'WDL-001',
            'brand': self.brand.id,
            'list_price': 0.66,
            'sale_ok': True,
        })
        self.product = self.tmpl.product_variant_id
        self.vendor = Partner.create({
            'name': 'WDL Slow Vendor',
            'baf_is_vendor': True,
            'baf_purchase_method': 'direct',
            'baf_brand_ids': [(6, 0, [self.brand.id])],
            'baf_delivery_weeks': 3,
            'baf_direct_sale_markup_pct': 10.0,
        })
        self.env['product.supplierinfo'].create({
            'partner_id': self.vendor.id,
            'product_tmpl_id': self.tmpl.id,
            'price': 0.50,
        })
        self.customer = Partner.create({'name': 'WDL Customer'})
        self.website = self.env['website'].search([], limit=1)

    def _website_order(self):
        return self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'website_id': self.website.id,
        })

    def test_website_default_line_has_no_auto_vendor(self):
        order = self._website_order()
        order._cart_add(product_id=self.product.id, quantity=1)
        line = order.order_line
        self.assertFalse(line.baf_alt_vendor_id)
        self.assertFalse(line.purchase_vendor_id)

    def test_website_alt_line_keeps_chosen_vendor(self):
        order = self._website_order()
        order._cart_add(
            product_id=self.product.id, quantity=1,
            baf_alt_vendor_id=self.vendor.id)
        line = order.order_line
        self.assertEqual(line.purchase_vendor_id, self.vendor)

    def test_website_mixed_lines_stay_independent(self):
        order = self._website_order()
        order._cart_add(product_id=self.product.id, quantity=1)
        order._cart_add(
            product_id=self.product.id, quantity=1,
            baf_alt_vendor_id=self.vendor.id)
        default_line = order.order_line.filtered(
            lambda l: not l.baf_alt_vendor_id)
        alt_line = order.order_line.filtered('baf_alt_vendor_id')
        self.assertFalse(default_line.purchase_vendor_id)
        self.assertEqual(alt_line.purchase_vendor_id, self.vendor)

    def test_backend_order_keeps_auto_selection(self):
        order = self.env['sale.order'].create(
            {'partner_id': self.customer.id})
        line = self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': self.product.id,
            'product_uom_qty': 1.0,
        })
        self.assertEqual(line.purchase_vendor_id, self.vendor)
