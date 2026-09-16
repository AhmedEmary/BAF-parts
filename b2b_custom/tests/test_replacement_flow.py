from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReplacementAndNlaFlow(TransactionCase):
    """Rules under test:

    - NLA (own SKU == 'NLA'): shown but not orderable.
    - Replaced (has replaced_by_id and chain does not end in NLA): shown but
      not orderable; a "select replacement" flow adds the successor.
    - Chain ends in NLA (some successor down the line has SKU 'NLA', current
      is not NLA itself): shown as an NLA row — same customer-facing state
      as own-SKU NLA, since there's no viable successor to route to.
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

    def test_search_marks_chain_ends_in_nla_row_as_nla(self):
        """The B2B /bestellsystem search must surface a chain-ends-in-NLA part
        as a visible NLA row (not "not found") so customers see why they
        can't order it."""
        from odoo.addons.b2b_custom.controllers.baf_b2b import _product_to_dict
        # replaced_by_nla → its chain terminates in the NLA marker
        partner = self.env['res.partner'].create({'name': 'RPL Search'})
        row = _product_to_dict(self.replaced_by_nla.product_variant_id, partner)
        self.assertTrue(row['is_nla'], "Chain-ends-in-NLA must expose is_nla=True")
        self.assertFalse(row['orderable'], "NLA row must not be orderable")
        self.assertEqual(row['availability'], 'NLA')
        self.assertEqual(row['availability_type'], 'nla')
        # No 'select replacement' button on a dead-end chain — otherwise the
        # customer clicks it and lands on the NLA placeholder.
        self.assertFalse(row['replacement_url'])
        self.assertFalse(row['replacement_product_id'])

    def test_search_marks_own_nla_row_as_nla(self):
        """A part whose own SKU is 'NLA' also renders as an NLA row."""
        from odoo.addons.b2b_custom.controllers.baf_b2b import _product_to_dict
        partner = self.env['res.partner'].create({'name': 'RPL Search 2'})
        row = _product_to_dict(self.nla.product_variant_id, partner)
        self.assertTrue(row['is_nla'])
        self.assertFalse(row['orderable'])
        self.assertEqual(row['availability_type'], 'nla')


@tagged('post_install', '-at_install')
class TestNlaPlaceholderSeeder(TransactionCase):
    """`_baf_ensure_nla_placeholders` seeds one NLA marker per brand."""

    def setUp(self):
        super().setUp()
        Brand = self.env['product.brand']
        self.brand_a = Brand.create({'name': 'SEED-A'})  # prefix "SEE"
        self.brand_b = Brand.create({'name': 'XX'})       # prefix "XX" (<3 chars)

    def test_seeder_creates_one_placeholder_per_brand(self):
        self.env['product.template']._baf_ensure_nla_placeholders()
        placeholder_a = self.env['product.template'].search([
            ('default_code', '=', 'SEE_NLA'),
        ], limit=1)
        placeholder_b = self.env['product.template'].search([
            ('default_code', '=', 'XX_NLA'),
        ], limit=1)
        self.assertTrue(placeholder_a, "SEED-A placeholder must exist")
        self.assertTrue(placeholder_b, "XX placeholder must exist")
        self.assertEqual(placeholder_a.sku, 'NLA')
        self.assertEqual(placeholder_b.sku, 'NLA')
        # Not surfaced to customers via /shop or /bestellsystem search.
        self.assertFalse(placeholder_a.sale_ok)
        self.assertFalse(placeholder_b.sale_ok)

    def test_seeder_is_idempotent(self):
        self.env['product.template']._baf_ensure_nla_placeholders()
        before = self.env['product.template'].search_count([('sku', '=', 'NLA')])
        self.env['product.template']._baf_ensure_nla_placeholders()
        after = self.env['product.template'].search_count([('sku', '=', 'NLA')])
        self.assertEqual(before, after, "Re-running the seeder must not duplicate rows")

    def test_seeder_normalizes_existing_sale_ok_true_placeholders(self):
        """Placeholders created by the mass importer default to sale_ok=True;
        the seed step must flip them so customers searching 'NLA' don't hit
        them."""
        Template = self.env['product.template']
        rogue = Template.create({
            'name': 'SEED-A NLA (rogue)',
            'sku': 'NLA', 'brand': self.brand_a.id,
            'sale_ok': True, 'purchase_ok': True, 'is_published': True,
            'active': True, 'list_price': 0.0, 'type': 'consu',
        })
        # Re-run the seed
        self.env['product.template']._baf_ensure_nla_placeholders()
        rogue.invalidate_recordset()
        self.assertFalse(rogue.sale_ok)
        self.assertFalse(rogue.purchase_ok)
        self.assertFalse(rogue.is_published)
