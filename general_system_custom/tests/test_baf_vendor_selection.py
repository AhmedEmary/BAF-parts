from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBafVendorSelection(TransactionCase):

    def setUp(self):
        super().setUp()
        Brand = self.env['product.brand']
        Partner = self.env['res.partner']
        Tmpl = self.env['product.template']
        self.Disc = self.env['baf.discount.line']

        self.brand_bmw = Brand.create({'name': 'BMW'})
        self.brand_jag = Brand.create({'name': 'Jaguar'})

        self.prod = Tmpl.create({
            'name': 'BMW part', 'list_price': 100.0, 'brand': self.brand_bmw.id,
            'baf_discount_code': '10', 'baf_type_code': 1,  # BMW_T12
        }).product_variant_id

        # Three matrix vendors supplying BMW, distinct discounts
        def mk(name, pct):
            v = Partner.create({
                'name': name, 'baf_purchase_method': 'matrix',
                'baf_brand_ids': [(6, 0, [self.brand_bmw.id])],
            })
            self.Disc.create({
                'table_type': 'purchase', 'column_key': 'BMW_T12',
                'discount_code': '10', 'discount_pct': pct,
                'partner_id': v.id,
            })
            return v

        self.v_cheap = mk('Cheap', 25.0)   # -> 75
        self.v_mid = mk('Mid', 20.0)       # -> 80
        self.v_wrong_brand = Partner.create({
            'name': 'JLR only', 'baf_purchase_method': 'matrix',
            'baf_brand_ids': [(6, 0, [self.brand_jag.id])],
        })
        self.v_no_method = Partner.create({
            'name': 'No method',
            'baf_brand_ids': [(6, 0, [self.brand_bmw.id])],
        })

    def test_eligible_excludes_wrong_brand_and_no_method(self):
        eligible = self.prod._baf_eligible_vendors()
        self.assertIn(self.v_cheap, eligible)
        self.assertIn(self.v_mid, eligible)
        self.assertNotIn(self.v_wrong_brand, eligible)
        self.assertNotIn(self.v_no_method, eligible)

    def test_cheapest_wins(self):
        best = self.prod.baf_get_best_vendor()
        self.assertEqual(best['vendor'], self.v_cheap)
        self.assertEqual(best['price'], 75.0)

    def test_tie_breaks_by_id(self):
        # id order and name order deliberately diverge: the lower-id vendor
        # ("Zeta...", created first) sorts alphabetically AFTER the higher-id
        # one ("Alpha..."). res.partner._order is name-first, so an impl that
        # dropped the explicit id tie-break key would pick "Alpha" and fail.
        # Both price the cheapest (tie at 60), so only the id key can decide.
        Partner = self.env['res.partner']

        def mk_tie(name):
            v = Partner.create({
                'name': name, 'baf_purchase_method': 'matrix',
                'baf_brand_ids': [(6, 0, [self.brand_bmw.id])],
            })
            self.Disc.create({
                'table_type': 'purchase', 'column_key': 'BMW_T12',
                'discount_code': '10', 'discount_pct': 40.0,  # -> 60, cheapest
                'partner_id': v.id,
            })
            return v

        v_low_id = mk_tie('Zeta Tie Vendor')    # created first  -> lower id
        v_high_id = mk_tie('Alpha Tie Vendor')  # created second -> higher id
        self.assertLess(v_low_id.id, v_high_id.id)

        best = self.prod.baf_get_best_vendor()
        self.assertEqual(best['price'], 60.0)
        self.assertEqual(best['vendor'], v_low_id)       # lower id wins
        self.assertNotEqual(best['vendor'], v_high_id)   # not alphabetical order

    def test_shorter_delivery_beats_cheaper_price(self):
        # v_cheap is 75 (blank delivery), v_mid is 80. Give v_mid a 1-week
        # delivery: shortest delivery wins over price.
        self.v_mid.baf_delivery_weeks = 1
        best = self.prod.baf_get_best_vendor()
        self.assertEqual(best['vendor'], self.v_mid)
        self.assertEqual(best['price'], 80.0)

    def test_same_delivery_breaks_by_price(self):
        # Equal delivery period -> cheaper wins.
        self.v_cheap.baf_delivery_weeks = 2
        self.v_mid.baf_delivery_weeks = 2
        best = self.prod.baf_get_best_vendor()
        self.assertEqual(best['vendor'], self.v_cheap)
        self.assertEqual(best['price'], 75.0)

    def test_blank_delivery_ranks_last(self):
        # v_cheap (75) has no delivery, v_mid (80) has a 3-week delivery.
        # Any set period beats an unset one, so v_mid wins despite costing more.
        self.v_mid.baf_delivery_weeks = 3
        best = self.prod.baf_get_best_vendor()
        self.assertEqual(best['vendor'], self.v_mid)

    def test_all_blank_delivery_falls_back_to_price(self):
        # Neither vendor has a delivery period -> pure price ranking (legacy).
        best = self.prod.baf_get_best_vendor()
        self.assertEqual(best['vendor'], self.v_cheap)
        self.assertEqual(best['price'], 75.0)

    def test_delivery_upper_bound(self):
        self.v_cheap.baf_delivery_weeks = 2
        self.assertEqual(self.v_cheap.baf_delivery_weeks_upper, 3)
        self.v_cheap.baf_delivery_weeks = 0
        self.assertEqual(self.v_cheap.baf_delivery_weeks_upper, 0)

    def test_no_priceable_vendor(self):
        # An eligible vendor exists (brand matches) but has no matching
        # discount row, so it cannot price the product.
        jag_prod = self.env['product.template'].create({
            'name': 'JLR part', 'list_price': 100.0, 'brand': self.brand_jag.id,
            'baf_discount_code': 'ZZ',
        }).product_variant_id
        self.assertIn(self.v_wrong_brand, jag_prod._baf_eligible_vendors())
        best = jag_prod.baf_get_best_vendor()
        self.assertFalse(best['vendor'])

    def test_no_eligible_vendor(self):
        # A brand that NO vendor lists -> the eligible set itself is empty,
        # exercising the `if not eligible: return empty` branch and its reason.
        nobody_brand = self.env['product.brand'].create({'name': 'ZZNobody'})
        prod = self.env['product.template'].create({
            'name': 'Nobody part', 'list_price': 100.0, 'brand': nobody_brand.id,
            'baf_discount_code': '10',
        }).product_variant_id
        self.assertFalse(prod._baf_eligible_vendors())
        best = prod.baf_get_best_vendor()
        self.assertFalse(best['vendor'])
        self.assertTrue(best['reason'])
