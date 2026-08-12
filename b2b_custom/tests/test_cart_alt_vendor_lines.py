from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestCartAltVendorLines(HttpCase):
    """The same product must be able to sit in the cart twice — once as the
    default add (no alternative vendor) and once per chosen alternative
    vendor — without the lines merging into each other."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env['res.partner']
        cls.website = cls.env['website'].get_current_website()

        cls.brand = cls.env['product.brand'].create(
            {'name': 'AVL-BRAND', 'is_public': True})
        cls.product_tmpl = cls.env['product.template'].create({
            'name': 'AVL Test Part',
            'sku': 'AVL-001',
            'brand': cls.brand.id,
            'list_price': 100.0,
            'sale_ok': True,
        })
        cls.product = cls.product_tmpl.product_variant_id

        cls.vendor = Partner.create({
            'name': 'AVL Axio Vendor',
            'baf_is_vendor': True,
            'baf_purchase_method': 'direct',
            'baf_brand_ids': [(6, 0, [cls.brand.id])],
            'baf_delivery_weeks': 3,
            'baf_direct_sale_markup_pct': 10.0,
        })
        cls.env['product.supplierinfo'].create({
            'partner_id': cls.vendor.id,
            'product_tmpl_id': cls.product_tmpl.id,
            'price': 50.0,
        })

        cls.customer = Partner.create({
            'name': 'AVL B2B Customer',
            'is_company': True,
            'email': 'avl@example.com',
        })
        cls.user = cls.env['res.users'].create({
            'name': 'AVL Cart User',
            'login': 'avl_cart_user',
            'password': 'avl_cart_user',
            'partner_id': cls.customer.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_portal').id,
                cls.env.ref('b2b_custom.group_b2b_customer').id,
            ])],
        })

    def _new_order(self):
        return self.env['sale.order'].create({
            'partner_id': self.customer.id,
            'website_id': self.website.id,
        })

    def _line_pairs(self, order):
        return [
            (line.baf_alt_vendor_id.id or 0, line.product_uom_qty)
            for line in order.order_line
        ]

    # ── ORM level: default add then alt-vendor add ─────────────────────────
    def test_default_then_alt_vendor_makes_two_lines(self):
        order = self._new_order()
        order._cart_add(product_id=self.product.id, quantity=1)
        order._cart_add(
            product_id=self.product.id, quantity=1,
            baf_alt_vendor_id=self.vendor.id)
        self.assertEqual(len(order.order_line), 2)
        self.assertCountEqual(
            self._line_pairs(order), [(0, 1.0), (self.vendor.id, 1.0)])

    def test_alt_vendor_then_default_makes_two_lines(self):
        order = self._new_order()
        order._cart_add(
            product_id=self.product.id, quantity=1,
            baf_alt_vendor_id=self.vendor.id)
        order._cart_add(product_id=self.product.id, quantity=1)
        self.assertEqual(len(order.order_line), 2)
        self.assertCountEqual(
            self._line_pairs(order), [(self.vendor.id, 1.0), (0, 1.0)])

    def test_same_alt_vendor_add_merges_into_its_own_line(self):
        order = self._new_order()
        order._cart_add(
            product_id=self.product.id, quantity=1,
            baf_alt_vendor_id=self.vendor.id)
        order._cart_add(
            product_id=self.product.id, quantity=2,
            baf_alt_vendor_id=self.vendor.id)
        self.assertEqual(len(order.order_line), 1)
        self.assertEqual(order.order_line.product_uom_qty, 3.0)
        self.assertEqual(order.order_line.baf_alt_vendor_id, self.vendor)

    def test_lines_price_independently(self):
        order = self._new_order()
        order._cart_add(product_id=self.product.id, quantity=1)
        order._cart_add(
            product_id=self.product.id, quantity=1,
            baf_alt_vendor_id=self.vendor.id)
        default_line = order.order_line.filtered(
            lambda l: not l.baf_alt_vendor_id)
        alt_line = order.order_line.filtered('baf_alt_vendor_id')
        alt_price = self.product._baf_alt_vendor_unit_price(self.vendor)
        self.assertEqual(alt_line.price_unit, alt_price)
        self.assertNotEqual(default_line.price_unit, alt_line.price_unit)

    # ── HTTP level: the /bestellsystem cart endpoint ───────────────────────
    def _cart_add_http(self, items):
        payload = {
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'items': items}, 'id': 1,
        }
        response = self.opener.post(
            self.base_url() + '/bestellsystem/cart/add',
            json=payload, timeout=30,
        )
        response.raise_for_status()
        return response.json().get('result') or {}

    def _last_cart(self):
        return self.env['sale.order'].search(
            [('partner_id', '=', self.customer.id),
             ('state', '=', 'draft')],
            order='id desc', limit=1)

    def test_bestellsystem_default_and_alt_stay_separate(self):
        self.authenticate('avl_cart_user', 'avl_cart_user')
        result = self._cart_add_http([
            {'product_id': self.product.id, 'quantity': 1},
            {'product_id': self.product.id, 'quantity': 1,
             'alt_vendor_id': self.vendor.id},
        ])
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result.get('added'), 2)
        order = self._last_cart()
        self.assertEqual(len(order.order_line), 2)
        self.assertCountEqual(
            self._line_pairs(order), [(0, 1.0), (self.vendor.id, 1.0)])

    def test_cart_page_marks_alt_vendor_line(self):
        self.authenticate('avl_cart_user', 'avl_cart_user')
        self._cart_add_http([
            {'product_id': self.product.id, 'quantity': 1},
            {'product_id': self.product.id, 'quantity': 1,
             'alt_vendor_id': self.vendor.id},
        ])
        response = self.url_open('/shop/cart')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8')
        self.assertIn('Direktbezug', body)
        # Vendor name must stay internal — only the delivery frame is shown.
        self.assertNotIn('AVL Axio Vendor', body)

    def test_bestellsystem_repeat_alt_add_does_not_duplicate(self):
        self.authenticate('avl_cart_user', 'avl_cart_user')
        self._cart_add_http([
            {'product_id': self.product.id, 'quantity': 1,
             'alt_vendor_id': self.vendor.id},
        ])
        self._cart_add_http([
            {'product_id': self.product.id, 'quantity': 2,
             'alt_vendor_id': self.vendor.id},
        ])
        order = self._last_cart()
        alt_lines = order.order_line.filtered(
            lambda l: l.baf_alt_vendor_id == self.vendor)
        self.assertEqual(len(alt_lines), 1)
        self.assertEqual(alt_lines.product_uom_qty, 3.0)
