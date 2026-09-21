from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestCartQtyLayout(HttpCase):
    """After an AJAX quantity change, the B2B cart-line fragment must keep the
    B2B layout: no product image, SKU shown.

    Guards the /shop/cart/update re-render path. The regression was that the
    fragment came back with the stock layout (product image back, SKU gone)
    because core renders it without the website context, skipping the B2B
    cart_lines_pagination customization. This drives the real endpoint and
    exercises the WebsiteSalePagination._get_updated_cart_page_values override.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.website = cls.env['website'].get_current_website()
        cls.brand = cls.env['product.brand'].create(
            {'name': 'CQL-BRAND', 'is_public': True})
        cls.template = cls.env['product.template'].create({
            'name': 'CQL Test Part',
            'sku': 'CQL-XYZ-001',
            'brand': cls.brand.id,
            'list_price': 100.0,
            'sale_ok': True,
            'is_published': True,
        })
        cls.product = cls.template.product_variant_id
        cls.customer = cls.env['res.partner'].create({
            'name': 'CQL Customer',
            'is_company': True,
            'email': 'cql@example.com',
        })
        cls.user = cls.env['res.users'].create({
            'name': 'CQL User',
            'login': 'cql_user',
            'password': 'cql_user',
            'partner_id': cls.customer.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_portal').id,
                cls.env.ref('b2b_custom.group_b2b_customer').id,
            ])],
        })

    def _post(self, path, params):
        response = self.opener.post(
            self.base_url() + path,
            json={'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 1},
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get('result') or {}

    def test_qty_update_keeps_b2b_layout(self):
        self.authenticate('cql_user', 'cql_user')
        self._post('/shop/cart/add', {
            'product_template_id': self.template.id,
            'product_id': self.product.id,
            'quantity': 1,
        })
        order = self.env['sale.order'].search(
            [('partner_id', '=', self.customer.id), ('state', '=', 'draft')],
            order='id desc', limit=1)
        line = order.order_line[:1]
        self.assertTrue(line, "cart line was not created")

        result = self._post('/shop/cart/update', {
            'line_id': line.id,
            'product_id': self.product.id,
            'quantity': 3,
        })
        html = result.get('website_sale.cart_lines') or ''
        self.assertTrue(html, "no cart_lines fragment returned")
        self.assertNotIn(
            'o_cart_product_image', html,
            "AJAX cart re-render must keep the B2B layout (no product image)")
        self.assertIn(
            'CQL-XYZ-001', html,
            "AJAX cart re-render must keep the SKU visible")
