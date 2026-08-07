from odoo.tests import HttpCase, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReplacedByRemoved(TransactionCase):
    """Task #52 removed every customer-facing use of `replaced_by_id`.
    The field itself stays in the schema for admin bookkeeping, but no
    orderability, search, or UI code path may consult it."""

    def setUp(self):
        super().setUp()
        Brand = self.env['product.brand']
        Template = self.env['product.template']
        self.brand = Brand.create({'name': 'RBOFF'})
        self.successor = Template.create({
            'name': 'Successor Part',
            'sku': 'RBSUCC',
            'brand': self.brand.id,
            'list_price': 100.0,
            'sale_ok': True,
            'active': True,
        })
        self.replaced = Template.create({
            'name': 'Old Part',
            'sku': 'RBOLD',
            'brand': self.brand.id,
            'list_price': 100.0,
            'sale_ok': True,
            'active': True,
            'replaced_by_id': self.successor.id,
        })
        self.nla = Template.create({
            'name': 'NLA Part',
            'sku': 'NLA',
            'brand': self.brand.id,
            'list_price': 100.0,
            'sale_ok': True,
            'active': True,
        })

    def test_replaced_product_is_not_nla(self):
        # NLA no longer walks the successor chain: a product is NLA only when
        # its own SKU is 'NLA'.
        self.assertFalse(self.replaced._baf_is_nla())
        self.assertTrue(self.nla._baf_is_nla())

    def test_replaced_product_is_orderable(self):
        # The single behavioural change customers actually see: replaced
        # products may be ordered normally.
        self.assertFalse(self.replaced._baf_is_order_blocked())
        # NLA still blocks.
        self.assertTrue(self.nla._baf_is_order_blocked())

    def test_replaced_product_is_add_to_cart_possible(self):
        # `_is_add_to_cart_possible` delegates to `_baf_is_order_blocked`.
        # Replaced → allowed; NLA → refused.
        product = self.replaced.product_variant_id
        self.assertTrue(self.replaced._is_add_to_cart_possible())
        self.assertFalse(self.nla._is_add_to_cart_possible())
        # A cart line for a replaced product must not raise UserError.
        SaleOrderLine = self.env['sale.order.line']
        SaleOrderLine._baf_assert_product_orderable(product)

    def test_replaced_product_still_shows_up_in_shop_search(self):
        # `_search_fetch` was already SKU-only and never filtered on the
        # successor link; assert once so any regression here is caught.
        results, _count = self.env['product.template']._search_fetch(
            {'base_domain': []}, 'RBOLD', 20, None,
        )
        self.assertIn(self.replaced.id, results.ids)

    def test_sales_order_line_accepts_replaced_product(self):
        # No UserError when creating a sale order line for a replaced product.
        partner = self.env['res.partner'].create({'name': 'RBOFF Customer'})
        order = self.env['sale.order'].create({'partner_id': partner.id})
        line = self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': self.replaced.product_variant_id.id,
            'product_uom_qty': 1.0,
        })
        self.assertTrue(line.id)

    def test_sales_order_line_still_rejects_nla_product(self):
        partner = self.env['res.partner'].create({'name': 'RBOFF NLA Customer'})
        order = self.env['sale.order'].create({'partner_id': partner.id})
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.env['sale.order.line'].create({
                'order_id': order.id,
                'product_id': self.nla.product_variant_id.id,
                'product_uom_qty': 1.0,
            })


@tagged('post_install', '-at_install')
class TestReplacedByRemovedInB2BSearch(HttpCase):
    """The B2B search JSON payload must not carry the successor fields
    anymore, and the endpoint must no longer accept the `product_ids`
    argument that used to power the "Nachfolger ansehen" flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Brand = cls.env['product.brand']
        Template = cls.env['product.template']
        # Prefix name with QA to keep unique against real staging brands.
        cls.brand = Brand.create({'name': 'QA-RBOFF-HTTP', 'is_public': True})
        cls.successor = Template.create({
            'name': 'RBOFF Successor', 'sku': 'RBHTTP-NEW',
            'brand': cls.brand.id, 'list_price': 100.0,
            'sale_ok': True, 'active': True,
        })
        cls.replaced = Template.create({
            'name': 'RBOFF Old', 'sku': 'RBHTTP-OLD',
            'brand': cls.brand.id, 'list_price': 100.0,
            'sale_ok': True, 'active': True,
            'replaced_by_id': cls.successor.id,
        })
        cls.user = cls.env['res.users'].create({
            'name': 'RBOFF Search User',
            'login': 'rboff_search_user',
            'password': 'rboff_search_user',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_portal').id,
                cls.env.ref('b2b_custom.group_b2b_customer').id,
            ])],
        })

    def _call_search(self, part_numbers=None, extra=None):
        self.authenticate('rboff_search_user', 'rboff_search_user')
        payload = {
            'jsonrpc': '2.0', 'method': 'call',
            'params': dict(extra or {}, part_numbers=part_numbers or []),
            'id': 1,
        }
        response = self.opener.post(
            self.base_url() + '/bestellsystem/part-search',
            json=payload, timeout=30,
        )
        response.raise_for_status()
        return response.json().get('result') or {}

    def test_payload_does_not_include_replacement_fields(self):
        # Every product dict has the same shape; assert none of the removed
        # successor keys are still emitted, even for a replaced product.
        result = self._call_search(part_numbers=['RBHTTP-OLD'])
        self.assertTrue(result['products'])
        entry = result['products'][0]
        for key in (
            'replacement_url', 'replacement_name',
            'replacement_part_number', 'replacement_product_id',
        ):
            self.assertNotIn(key, entry)

    def test_product_ids_argument_is_no_longer_accepted(self):
        # The endpoint dropped `product_ids`; passing it must be a silent
        # no-op that doesn't succeed at bypassing the SKU-only match.
        result = self._call_search(
            part_numbers=[],
            extra={'product_ids': [self.successor.product_variant_id.id]},
        )
        self.assertEqual(result['products'], [])
