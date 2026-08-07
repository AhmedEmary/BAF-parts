from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReplacementAndNlaFlow(TransactionCase):
    """Rules under test:

    - NLA (own SKU == 'NLA'): shown but not orderable.
    - Replaced (has replaced_by_id and chain does not end in NLA): shown but
      not orderable; a "select replacement" flow adds the successor.
    - Chain ends in NLA (some successor down the line has SKU 'NLA', current
      is not NLA itself): filtered out of search results — no viable
      purchase path.
    """

    def setUp(self):
        super().setUp()
        Brand = self.env['product.brand']
        Template = self.env['product.template']
        self.brand = Brand.create({'name': 'RPL-FLOW'})

        self.nla = Template.create({
            'name': 'NLA marker', 'sku': 'NLA',
            'brand': self.brand.id, 'list_price': 0.0,
            'sale_ok': True, 'active': True,
        })
        self.replaced_by_nla = Template.create({
            'name': 'Chain terminates in NLA', 'sku': 'RPL-CHAIN-NLA',
            'brand': self.brand.id, 'list_price': 100.0,
            'sale_ok': True, 'active': True,
            'replaced_by_id': self.nla.id,
        })
        self.successor_live = Template.create({
            'name': 'Live successor', 'sku': 'RPL-SUCC',
            'brand': self.brand.id, 'list_price': 100.0,
            'sale_ok': True, 'active': True,
        })
        self.replaced_live = Template.create({
            'name': 'Replaced, successor live', 'sku': 'RPL-OLD',
            'brand': self.brand.id, 'list_price': 100.0,
            'sale_ok': True, 'active': True,
            'replaced_by_id': self.successor_live.id,
        })

    def test_nla_marker_flags_are_correct(self):
        self.assertTrue(self.nla._baf_is_nla())
        self.assertFalse(self.nla._baf_chain_ends_in_nla())
        self.assertTrue(self.nla._baf_is_order_blocked())

    def test_chain_ending_in_nla_is_flagged_but_not_own_nla(self):
        self.assertFalse(self.replaced_by_nla._baf_is_nla())
        self.assertTrue(self.replaced_by_nla._baf_chain_ends_in_nla())
        self.assertTrue(self.replaced_by_nla._baf_is_order_blocked())

    def test_replaced_with_live_successor_is_not_nla(self):
        self.assertFalse(self.replaced_live._baf_is_nla())
        self.assertFalse(self.replaced_live._baf_chain_ends_in_nla())
        self.assertTrue(self.replaced_live._baf_is_order_blocked())

    def test_nla_and_replaced_products_cannot_be_added_to_cart(self):
        # Both NLA and replaced products raise a UserError when a SO line
        # tries to book them — customers can only order the live successor.
        partner = self.env['res.partner'].create({'name': 'RPL Customer'})
        order = self.env['sale.order'].create({'partner_id': partner.id})
        SaleOrderLine = self.env['sale.order.line']
        with self.assertRaises(UserError):
            SaleOrderLine.create({
                'order_id': order.id,
                'product_id': self.nla.product_variant_id.id,
                'product_uom_qty': 1.0,
            })
        with self.assertRaises(UserError):
            SaleOrderLine.create({
                'order_id': order.id,
                'product_id': self.replaced_live.product_variant_id.id,
                'product_uom_qty': 1.0,
            })

    def test_live_successor_can_be_added_to_cart(self):
        partner = self.env['res.partner'].create({'name': 'RPL Customer 2'})
        order = self.env['sale.order'].create({'partner_id': partner.id})
        line = self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': self.successor_live.product_variant_id.id,
            'product_uom_qty': 1.0,
        })
        self.assertTrue(line.id)
