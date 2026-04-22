from odoo.tests import TransactionCase, tagged

from odoo.addons.general_system_custom.models.baf_product_pricing import resolve_baf_brand_info


@tagged('post_install', '-at_install')
class TestBafPricing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Brand = self.env['product.brand']
        self.Partner = self.env['res.partner']
        self.ProductTemplate = self.env['product.template']
        self.DiscountLine = self.env['baf.discount.line']
        self.SalesGroup = self.env['baf.sales.group']

        self.brand_bmw = self.Brand.create({'name': 'BMW'})
        self.brand_mini = self.Brand.create({'name': 'MINI'})
        self.brand_jaguar = self.Brand.create({'name': 'Jaguar'})
        self.brand_mercedes = self.Brand.create({'name': 'Mercedes-Benz'})
        self.brand_other = self.Brand.create({'name': 'Bosal'})

        self.group_bmw_gr1 = self.SalesGroup.create({
            'name': 'BMW/MINI GR1',
            'brand_family': 'bmw_mini',
            'pricing_method': 'table_lookup',
            'group_column_suffix': 'GR1',
        })
        self.group_bmw_default_suffix = self.SalesGroup.create({
            'name': 'BMW/MINI default suffix',
            'brand_family': 'bmw_mini',
            'pricing_method': 'table_lookup',
        })
        self.group_jlr_markup = self.SalesGroup.create({
            'name': 'JLR markup',
            'brand_family': 'jlr',
            'pricing_method': 'markup_pct',
            'markup_pct': 15.0,
        })
        self.group_all_markup = self.SalesGroup.create({
            'name': 'Wildcard markup',
            'brand_family': 'all',
            'pricing_method': 'markup_pct',
            'markup_pct': 20.0,
        })
        self.group_bmw_inactive = self.SalesGroup.create({
            'name': 'Inactive BMW group',
            'brand_family': 'bmw_mini',
            'pricing_method': 'table_lookup',
            'group_column_suffix': 'GR1',
            'active': False,
        })

        self.partner_bmw = self.Partner.create({
            'name': 'BMW Customer',
            'sales_group_ids': [(6, 0, [self.group_bmw_gr1.id])],
        })
        self.partner_bmw_default_suffix = self.Partner.create({
            'name': 'BMW Default Suffix Customer',
            'sales_group_ids': [(6, 0, [self.group_bmw_default_suffix.id])],
        })
        self.partner_jlr = self.Partner.create({
            'name': 'JLR Customer',
            'sales_group_ids': [(6, 0, [self.group_jlr_markup.id])],
        })
        self.partner_all = self.Partner.create({
            'name': 'Wildcard Customer',
            'sales_group_ids': [(6, 0, [self.group_all_markup.id])],
        })
        self.partner_unrelated = self.Partner.create({
            'name': 'Unrelated Customer',
            'sales_group_ids': [(6, 0, [self.group_jlr_markup.id])],
        })
        self.partner_inactive_exact_active_wildcard = self.Partner.create({
            'name': 'Inactive exact Active wildcard',
            'sales_group_ids': [(6, 0, [self.group_bmw_inactive.id, self.group_all_markup.id])],
        })

        discount_lines = [
            {'table_type': 'purchase', 'column_key': 'SUP1_BMW_T12', 'discount_code': '10', 'discount_pct': 20.0},
            {'table_type': 'purchase', 'column_key': 'SUP2_BMW_T12', 'discount_code': '10', 'discount_pct': 20.0},  # SUP2 gets same 20% — SB surcharge only applies for SUP1
            {'table_type': 'purchase', 'column_key': 'SUP1_BMW_T39', 'discount_code': '10', 'discount_pct': 30.0},
            {'table_type': 'purchase', 'column_key': 'SUP3_MOTO', 'discount_code': '10', 'discount_pct': 40.0},
            {'table_type': 'sales', 'column_key': 'BMW_T12_GR1', 'discount_code': '10', 'discount_pct': 5.0},
            {'table_type': 'sales', 'column_key': 'BMW_T39_GR1', 'discount_code': '10', 'discount_pct': 7.0},
            {'table_type': 'sales', 'column_key': 'BMW_T12_MOTO', 'discount_code': '10', 'discount_pct': 8.0},
        ]
        for vals in discount_lines:
            self.DiscountLine.create(vals)

    def _create_product(self, brand, sku, **extra_vals):
        vals = {
            'name': f'{brand.name} {sku}',
            'brand': brand.id,
            'sku': sku,
            'list_price': 100.0,
            'baf_discount_code': '10',
            'baf_type_code': 1,
            'baf_mod': 'car',
            'supplier_route': 'de_table',
            'surcharge': 0.0,
        }
        vals.update(extra_vals)
        return self.ProductTemplate.create(vals)

    def test_01_resolve_baf_brand_info_cases(self):
        self.assertEqual(resolve_baf_brand_info('', 0, 'car'), ('', 'other'))
        self.assertEqual(resolve_baf_brand_info('BMW', 1, 'car'), ('BMW_T12', 'bmw_mini'))
        self.assertEqual(resolve_baf_brand_info('MINI', 3, 'car'), ('MINI_T39', 'bmw_mini'))
        self.assertEqual(resolve_baf_brand_info('Land-Rover', 0, 'car'), ('JLR', 'jlr'))
        self.assertEqual(resolve_baf_brand_info('Mercedes-Benz', 0, 'car'), ('MERCEDES', 'mercedes'))
        self.assertEqual(resolve_baf_brand_info('Bosal', 0, 'car'), ('BOSAL', 'other'))

    def test_02_discount_lookup_returns_match_or_zero(self):
        self.assertEqual(
            self.DiscountLine.get_discount_pct('purchase', 'SUP1_BMW_T12', '10'),
            20.0,
        )
        self.assertEqual(
            self.DiscountLine.get_discount_pct('sales', 'BMW_T12_GR9', '10'),
            0.0,
        )

    def test_03_purchase_price_uses_t12_and_t39_tables(self):
        product_t12 = self._create_product(self.brand_bmw, 'P-T12', baf_type_code=1)
        product_t39 = self._create_product(self.brand_bmw, 'P-T39', baf_type_code=3)

        self.assertAlmostEqual(product_t12.baf_get_purchase_price('SUP1'), 80.0)
        self.assertAlmostEqual(product_t39.baf_get_purchase_price('SUP1'), 70.0)

    def test_04_purchase_price_motorcycle_uses_sup3_moto(self):
        product = self._create_product(self.brand_bmw, 'P-MOTO', baf_mod='motorcycle', baf_type_code=1)
        self.assertAlmostEqual(product.baf_get_purchase_price('SUP1'), 60.0)
        self.assertAlmostEqual(product.baf_get_purchase_price('SUP3'), 60.0)

    def test_05_purchase_price_sb_uses_default_and_override_surcharge(self):
        default_sb = self._create_product(self.brand_bmw, 'P-SB-DEFAULT', baf_mod='sb')
        override_sb = self._create_product(
            self.brand_bmw,
            'P-SB-OVERRIDE',
            baf_mod='sb',
            baf_sb_surcharge_override=10.0,
        )

        self.assertAlmostEqual(default_sb.baf_get_purchase_price('SUP1'), 75.84, places=2)
        self.assertAlmostEqual(override_sb.baf_get_purchase_price('SUP1'), 72.0, places=2)
        self.assertAlmostEqual(default_sb.baf_get_purchase_price('SUP2'), 80.0, places=2)

    def test_06_purchase_price_eu_direct_returns_none(self):
        product = self._create_product(self.brand_bmw, 'P-EU', supplier_route='eu_direct')
        self.assertIsNone(product.baf_get_purchase_price('SUP1'))

    def test_07_sales_price_guest_and_unrelated_group_fallback_to_upe(self):
        product = self._create_product(self.brand_bmw, 'S-GUEST', surcharge=3.0)

        guest_details = product.baf_get_sales_price_details(partner=None)
        unrelated_details = product.baf_get_sales_price_details(self.partner_unrelated)
        self.assertEqual(guest_details['pricing_method'], 'guest')
        self.assertEqual(unrelated_details['pricing_method'], 'guest')
        self.assertAlmostEqual(guest_details['price'], 103.0)
        self.assertAlmostEqual(unrelated_details['price'], 103.0)

    def test_08_sales_price_markup_exact_and_wildcard_groups(self):
        jlr_product = self._create_product(self.brand_jaguar, 'S-JLR', list_price=200.0, surcharge=4.0)
        other_product = self._create_product(self.brand_other, 'S-OTHER', list_price=50.0, surcharge=2.0)

        jlr_details = jlr_product.baf_get_sales_price_details(self.partner_jlr)
        wildcard_details = other_product.baf_get_sales_price_details(self.partner_all)

        self.assertEqual(jlr_details['pricing_method'], 'markup_pct')
        self.assertAlmostEqual(jlr_details['price'], 234.0)
        self.assertEqual(jlr_details['column_key'], '')
        self.assertEqual(jlr_details['discount_pct'], 15.0)

        self.assertEqual(wildcard_details['pricing_method'], 'markup_pct')
        self.assertAlmostEqual(wildcard_details['price'], 62.0)
        self.assertEqual(wildcard_details['column_key'], '')
        self.assertEqual(wildcard_details['discount_pct'], 20.0)

    def test_09_sales_price_table_lookup_and_motorcycle_suffix(self):
        car_product = self._create_product(self.brand_bmw, 'S-CAR', surcharge=2.0, baf_type_code=1)
        moto_product = self._create_product(
            self.brand_bmw,
            'S-MOTO',
            surcharge=1.0,
            baf_type_code=1,
            baf_mod='motorcycle',
        )

        car_details = car_product.baf_get_sales_price_details(self.partner_bmw)
        moto_details = moto_product.baf_get_sales_price_details(self.partner_bmw)

        self.assertEqual(car_details['pricing_method'], 'table_lookup')
        self.assertEqual(car_details['column_key'], 'BMW_T12_GR1')
        self.assertAlmostEqual(car_details['discount_pct'], 5.0)
        self.assertAlmostEqual(car_details['price'], 97.0)

        self.assertEqual(moto_details['pricing_method'], 'table_lookup')
        self.assertEqual(moto_details['column_key'], 'BMW_T12_MOTO')
        self.assertAlmostEqual(moto_details['discount_pct'], 8.0)
        self.assertAlmostEqual(moto_details['price'], 93.0)

    def test_10_sales_price_default_group_suffix_is_gr1(self):
        product = self._create_product(self.brand_bmw, 'S-GR1-DEFAULT', baf_type_code=3)
        details = product.baf_get_sales_price_details(self.partner_bmw_default_suffix)

        self.assertEqual(details['pricing_method'], 'table_lookup')
        self.assertEqual(details['column_key'], 'BMW_T39_GR1')
        self.assertAlmostEqual(details['discount_pct'], 7.0)
        self.assertAlmostEqual(details['price'], 93.0)

    def test_11_sales_price_ignores_inactive_exact_group_and_uses_active_wildcard(self):
        product = self._create_product(self.brand_bmw, 'S-INACTIVE-EXACT')
        details = product.baf_get_sales_price_details(self.partner_inactive_exact_active_wildcard)

        self.assertEqual(details['pricing_method'], 'markup_pct')
        self.assertAlmostEqual(details['price'], 120.0)
        self.assertEqual(details['discount_pct'], 20.0)

    def test_12_product_variant_wrappers_delegate_to_template(self):
        product_tmpl = self._create_product(self.brand_bmw, 'WRAP01', surcharge=2.0)
        variant = product_tmpl.product_variant_id

        self.assertAlmostEqual(
            variant.baf_get_purchase_price('SUP1'),
            product_tmpl.baf_get_purchase_price('SUP1'),
        )
        self.assertAlmostEqual(
            variant.baf_get_sales_price(self.partner_bmw),
            product_tmpl.baf_get_sales_price(self.partner_bmw),
        )
        self.assertEqual(
            variant.baf_get_sales_price_details(self.partner_bmw),
            product_tmpl.baf_get_sales_price_details(self.partner_bmw),
        )


