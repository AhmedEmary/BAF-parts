import gzip
import io
import csv
from datetime import date
from urllib.parse import urlparse

from odoo.tests import HttpCase, tagged
from odoo.tools import float_round

from odoo.addons.b2b_custom.controllers.pricefile import pricefile_query, visible_brands


@tagged('post_install', '-at_install')
class TestPriceFile(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # product.brand now has unique(name) and every brand belongs to a
        # baf.brand.family; group-to-product matching is by family_id record.
        # Names are QA-prefixed to stay unique against real staging brands, but
        # still contain BMW/Jaguar/Mercedes so resolve_baf_brand_info classifies
        # them (the name-based moto-split and EU-VAT rules depend on that).
        Family = cls.env['baf.brand.family']
        cls.fam_bmw = Family.create({'name': 'QA BMW/MINI'})
        cls.fam_jlr = Family.create({'name': 'QA JLR'})

        Brand = cls.env['product.brand']
        # Hyphenated (not spaced): resolve_baf_brand_info normalizes '-' to a
        # space so 'QA-BMW' still classifies as bmw_mini, while a hyphen stays
        # literal in the Content-Disposition filename (a space would be
        # percent-encoded, breaking the download filename assertion).
        cls.brand_public = Brand.create({
            'name': 'QA-BMW', 'is_public': True, 'family_id': cls.fam_bmw.id})
        cls.brand_company = Brand.create({
            'name': 'QA-Jaguar', 'is_public': False, 'family_id': cls.fam_jlr.id})
        cls.brand_child = Brand.create({'name': 'QA-Mercedes-Benz', 'is_public': False})
        cls.brand_hidden = Brand.create({'name': 'QA-Bosal', 'is_public': False})

        cls.company_partner = cls.env['res.partner'].create({
            'name': 'B2B Company',
            'is_company': True,
            'visible_brand_ids': [(6, 0, cls.brand_company.ids)],
        })
        cls.child_partner = cls.env['res.partner'].create({
            'name': 'B2B Child Contact',
            'parent_id': cls.company_partner.id,
            'visible_brand_ids': [(6, 0, cls.brand_child.ids)],
        })

        SalesGroup = cls.env['baf.sales.group']
        DiscountLine = cls.env['baf.discount.line']

        cls.group_bmw_gr1 = SalesGroup.create({
            'name': 'BMW GR1',
            'family_id': cls.fam_bmw.id,
            'pricing_method': 'table_lookup',
            'group_column_suffix': 'GR1',
        })
        cls.group_bmw_moto = SalesGroup.create({
            'name': 'BMW MOTO',
            'family_id': cls.fam_bmw.id,
            'pricing_method': 'table_lookup',
            'group_column_suffix': 'MOTO',
        })
        cls.group_jlr_markup = SalesGroup.create({
            'name': 'JLR markup',
            'family_id': cls.fam_jlr.id,
            'pricing_method': 'markup_pct',
            'markup_pct': 15.0,
        })

        Template = cls.env['product.template']
        cls.bmw_car = Template.create({
            'name': 'BMW Brake Pad',
            'sku': 'BMW-001',
            'brand': cls.brand_public.id,
            'list_price': 100.0,
            'surcharge': 5.0,
            'baf_discount_code': 'TESTQA1',
            'baf_type_code': 1,
            'baf_mod': 'car',
        })
        # The discount-column base is now the brand's own name (refactor: no more
        # hardcoded BMW/MINI base), so key the sales rows off the product's
        # actual computed baf_sales_column_key (family-based, e.g.
        # 'QA_BMW_MINI_T12') instead of a literal.
        cls.bmw_col = cls.bmw_car.baf_sales_column_key
        # Sales rows are global: partner_id is False. discount_code 'TESTQA1' is
        # deliberately not a plain digit string so it can't collide with the real
        # staging discount rows (which cover the numeric codes 0-60).
        DiscountLine.create([
            {'table_type': 'sales', 'column_key': '%s_GR1' % cls.bmw_col,
             'discount_code': 'TESTQA1', 'discount_pct': 20.0},
            {'table_type': 'sales', 'column_key': '%s_MOTO' % cls.bmw_col,
             'discount_code': 'TESTQA1', 'discount_pct': 35.0},
        ])
        cls.bmw_moto = Template.create({
            'name': 'BMW Moto Chain',
            'sku': 'BMW-002',
            'brand': cls.brand_public.id,
            'list_price': 200.0,
            'baf_discount_code': 'TESTQA1',
            'baf_type_code': 1,
            'baf_mod': 'motorcycle',
        })
        cls.bmw_uncoded = Template.create({
            'name': 'BMW Odd Part',
            'sku': 'BMW-003',
            'brand': cls.brand_public.id,
            'list_price': 50.0,
            'baf_discount_code': 'NOPE',
            'baf_type_code': 1,
            'baf_mod': 'car',
        })
        cls.jlr_part = Template.create({
            'name': 'Jaguar Filter',
            'sku': 'JAG-001',
            'brand': cls.brand_company.id,
            'list_price': 80.0,
            'baf_discount_code': '1A',
        })

        cls.user = cls.env['res.users'].create({
            'name': 'B2B Portal User',
            'login': 'pricefile_user',
            'password': 'pricefile_user',
            'partner_id': cls.company_partner.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_portal').id,
                cls.env.ref('b2b_custom.group_b2b_customer').id,
            ])],
        })

    # visible_brands() also returns any is_public brand in the DB, so compare
    # only within this test's own fixture brands (the staging DB has real
    # public brands that would otherwise leak into an exact-set assertion).
    @property
    def _fixture_brand_ids(self):
        return {
            self.brand_public.id, self.brand_company.id,
            self.brand_child.id, self.brand_hidden.id,
        }

    def test_visible_brands_parent_takes_own_brands_only(self):
        brands = visible_brands(self.env, self.company_partner)
        self.assertEqual(
            set(brands.ids) & self._fixture_brand_ids,
            {self.brand_public.id, self.brand_company.id},
        )

    def test_visible_brands_child_unions_parent_brands(self):
        brands = visible_brands(self.env, self.child_partner)
        self.assertEqual(
            set(brands.ids) & self._fixture_brand_ids,
            {self.brand_public.id, self.brand_company.id, self.brand_child.id},
        )

    def test_visible_brands_excludes_unrelated_private_brand(self):
        brands = visible_brands(self.env, self.child_partner)
        self.assertNotIn(self.brand_hidden.id, brands.ids)

    def _rows(self, partner, brand):
        """Run the price-file SELECT and return {sku: (description, price)}."""
        self.env.flush_all()
        sql, params = pricefile_query(partner, brand, 'en_US')
        self.env.cr.execute(sql, params)
        return {r[0]: (r[1], float(r[2])) for r in self.env.cr.fetchall()}

    def _assert_matches_engine(self, partner, brand, templates):
        rows = self._rows(partner, brand)
        for template in templates:
            # float_round (half-up), not Python round() (half-to-even): Postgres
            # round(x::numeric, 2) rounds half away from zero, matching how
            # Odoo bills sale.order.line.price_unit. On exact half-cent ties
            # Python's round() would disagree with both.
            expected = float_round(
                template.baf_get_sales_price(partner=partner), precision_digits=2,
            )
            self.assertIn(template.sku, rows)
            description, price = rows[template.sku]
            self.assertEqual(description, template.name)
            self.assertEqual(
                price, expected,
                "%s: CSV price %s != engine price %s" % (template.sku, price, expected),
            )

    def test_price_no_group_is_full_upe(self):
        # 100 list + 5 surcharge, no discount
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertEqual(rows['BMW-001'][1], 105.0)
        self._assert_matches_engine(
            self.company_partner, self.brand_public,
            self.bmw_car + self.bmw_moto + self.bmw_uncoded,
        )

    def test_price_table_lookup_group(self):
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]
        # 100 * (1 - 20%) + 5 surcharge
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertEqual(rows['BMW-001'][1], 85.0)
        # No table row for code NOPE -> full UPE
        self.assertEqual(rows['BMW-003'][1], 50.0)
        self._assert_matches_engine(
            self.company_partner, self.brand_public,
            self.bmw_car + self.bmw_uncoded,
        )

    def test_price_moto_tier_overrides_car_group(self):
        self.company_partner.sales_group_ids = [
            (6, 0, (self.group_bmw_gr1 + self.group_bmw_moto).ids)
        ]
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertEqual(rows['BMW-001'][1], 85.0)    # car: 20% off + surcharge
        self.assertEqual(rows['BMW-002'][1], 130.0)   # moto: 35% off 200
        self._assert_matches_engine(
            self.company_partner, self.brand_public,
            self.bmw_car + self.bmw_moto,
        )

    def test_price_markup_group(self):
        self.company_partner.sales_group_ids = [(6, 0, self.group_jlr_markup.ids)]
        # 80 * 1.15
        rows = self._rows(self.company_partner, self.brand_company)
        self.assertEqual(rows['JAG-001'][1], 92.0)
        self._assert_matches_engine(
            self.company_partner, self.brand_company, self.jlr_part,
        )

    def test_price_child_contact_inherits_company_group(self):
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]
        self.child_partner.sales_group_ids = [(6, 0, [])]
        rows = self._rows(self.child_partner, self.brand_public)
        self.assertEqual(rows['BMW-001'][1], 85.0)
        self._assert_matches_engine(
            self.child_partner, self.brand_public, self.bmw_car,
        )

    def test_price_b2b_eu_vat_jlr_discount(self):
        self.company_partner.sales_group_ids = [(6, 0, [])]
        self.company_partner.write({
            'vat': 'DE123456788',
            'country_id': self.env.ref('base.de').id,
        })
        self.assertTrue(self.company_partner.is_b2b_eu_vat)
        # 80 * 0.95, no surcharge on this product
        rows = self._rows(self.company_partner, self.brand_company)
        self.assertEqual(rows['JAG-001'][1], 76.0)
        self._assert_matches_engine(
            self.company_partner, self.brand_company, self.jlr_part,
        )

    def test_query_only_returns_the_requested_brand(self):
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertNotIn('JAG-001', rows)

    def test_half_cent_rounding_matches_sql_half_up(self):
        # 100.25 * 50% = 50.125 exactly (both operands exact in binary).
        # Postgres round(numeric, 2) is half-away-from-zero -> 50.13.
        # Python's round() is half-to-even -> would give 50.12. The SQL (and
        # therefore the price file) must land on 50.13 to match how Odoo
        # bills sale.order.line.price_unit via float_round.
        self.env['baf.discount.line'].create({
            'table_type': 'sales', 'column_key': '%s_GR1' % self.bmw_col,
            'discount_code': 'TESTQA2', 'discount_pct': 50.0,
        })
        product = self.env['product.template'].create({
            'name': 'BMW Half Cent Tie',
            'sku': 'BMW-HALFCENT',
            'brand': self.brand_public.id,
            'list_price': 100.25,
            'surcharge': 0.0,
            'baf_discount_code': 'TESTQA2',
            'baf_type_code': 1,
            'baf_mod': 'car',
        })
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertEqual(rows['BMW-HALFCENT'][1], 50.13)
        self._assert_matches_engine(self.company_partner, self.brand_public, product)

    def test_duplicate_discount_line_lowest_id_wins_no_fanout(self):
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]
        # Same (table_type, column_key, discount_code) as the TESTQA1 fixture
        # row, created after it -> higher id -> must lose, and must not
        # duplicate the product row in the result.
        self.env['baf.discount.line'].create({
            'table_type': 'sales', 'column_key': '%s_GR1' % self.bmw_col,
            'discount_code': 'TESTQA1', 'discount_pct': 99.0,
        })
        self.env.flush_all()
        sql, params = pricefile_query(self.company_partner, self.brand_public, 'en_US')
        self.env.cr.execute(sql, params)
        all_rows = self.env.cr.fetchall()
        matching = [r for r in all_rows if r[0] == 'BMW-001']
        self.assertEqual(len(matching), 1, "duplicate discount line fanned out the product row")
        self.assertEqual(float(matching[0][2]), 85.0, "price must come from the lowest-id row (20%%), not 99%%")
        self._assert_matches_engine(self.company_partner, self.brand_public, self.bmw_car)

    def test_wildcard_group_prices_unmatched_family(self):
        wildcard_group = self.env['baf.sales.group'].create({
            'name': 'All Brands GR',
            'family_id': False,  # no family = wildcard fallback (was brand_family 'all')
            'pricing_method': 'markup_pct',
            'markup_pct': 10.0,
        })
        self.company_partner.sales_group_ids = [(6, 0, wildcard_group.ids)]

        # JLR product, partner has no jlr group -> wildcard prices it.
        rows = self._rows(self.company_partner, self.brand_company)
        self.assertEqual(rows['JAG-001'][1], 88.0)
        self._assert_matches_engine(self.company_partner, self.brand_company, self.jlr_part)

        # Subtle moto-fallback case: BMW motorcycle product, partner holds
        # ONLY the 'all' group (no bmw_mini group at all) -> wildcard prices
        # it too.
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertEqual(rows['BMW-002'][1], 220.0)
        self._assert_matches_engine(self.company_partner, self.brand_public, self.bmw_moto)

    def test_inactive_group_ignored_full_upe(self):
        # NOTE: Odoo's many2many read already drops archived records, so
        # partner.sales_group_ids never contains the archived group here -
        # this test does not pin pricefile_query's own `.filtered(lambda g:
        # g.active)` call (deleting it would leave this test green). It's
        # kept as an engine-parity check: pricefile_query must agree with
        # baf_get_sales_price on how an archived group is handled.
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]
        self.group_bmw_gr1.active = False
        self.assertFalse(
            self.company_partner._baf_effective_sales_groups().filtered(lambda g: g.active),
        )
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertEqual(rows['BMW-001'][1], 105.0)  # full UPE: 100 + 5 surcharge
        self._assert_matches_engine(self.company_partner, self.brand_public, self.bmw_car)

    def test_family_mismatch_group_ignored_full_upe(self):
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]
        rows = self._rows(self.company_partner, self.brand_company)
        self.assertEqual(rows['JAG-001'][1], 80.0)  # full UPE, bmw_mini group doesn't cover JLR
        self._assert_matches_engine(self.company_partner, self.brand_company, self.jlr_part)

    def test_eu_vat_precedence_group_wins_over_flat_discount(self):
        self.company_partner.sales_group_ids = [(6, 0, self.group_jlr_markup.ids)]
        self.company_partner.write({
            'vat': 'DE123456788',
            'country_id': self.env.ref('base.de').id,
        })
        self.assertTrue(self.company_partner.is_b2b_eu_vat)
        # Group (80 * 1.15 = 92) must win over the -5% EU-VAT tier (80 * 0.95 = 76).
        rows = self._rows(self.company_partner, self.brand_company)
        self.assertEqual(rows['JAG-001'][1], 92.0)
        self._assert_matches_engine(self.company_partner, self.brand_company, self.jlr_part)

    def test_default_gr1_suffix_when_blank(self):
        blank_suffix_group = self.env['baf.sales.group'].create({
            'name': 'BMW blank suffix',
            'family_id': self.fam_bmw.id,
            'pricing_method': 'table_lookup',
            'group_column_suffix': False,
        })
        self.company_partner.sales_group_ids = [(6, 0, blank_suffix_group.ids)]
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertEqual(rows['BMW-001'][1], 85.0)  # looked up via BMW_T12_GR1
        self._assert_matches_engine(self.company_partner, self.brand_public, self.bmw_car)

    def test_inactive_and_non_sellable_products_excluded(self):
        Template = self.env['product.template']
        inactive_product = Template.create({
            'name': 'BMW Inactive',
            'sku': 'BMW-INACTIVE',
            'brand': self.brand_public.id,
            'list_price': 10.0,
            'active': False,
        })
        not_sellable = Template.create({
            'name': 'BMW Not Sellable',
            'sku': 'BMW-NOSELL',
            'brand': self.brand_public.id,
            'list_price': 10.0,
            'sale_ok': False,
        })
        rows = self._rows(self.company_partner, self.brand_public)
        self.assertNotIn('BMW-INACTIVE', rows)
        self.assertNotIn('BMW-NOSELL', rows)
        # sanity: filtering didn't also drop a normal, active/sellable product
        self.assertIn('BMW-001', rows)
        self._assert_matches_engine(self.company_partner, self.brand_public, self.bmw_car)

    def test_price_list_shares_family_column_but_splits_bmw_mini(self):
        """End-to-end price-list check of the column-key rule: brands in a
        non-type-split family share ONE sales discount line, while BMW/MINI
        (type-split) keep separate per-brand columns."""
        Brand = self.env['product.brand']
        Template = self.env['product.template']
        DiscountLine = self.env['baf.discount.line']

        # (a) Two JLR-family brands (Jaguar + Land Rover) share one column.
        land_rover = Brand.create({
            'name': 'QA-LandRover', 'is_public': True, 'family_id': self.fam_jlr.id})
        jlr_group = self.env['baf.sales.group'].create({
            'name': 'JLR GR1', 'family_id': self.fam_jlr.id,
            'pricing_method': 'table_lookup', 'group_column_suffix': 'GR1'})
        jag = Template.create({
            'name': 'Jaguar Belt', 'sku': 'JAG-777', 'brand': self.brand_company.id,
            'list_price': 100.0, 'baf_discount_code': 'TESTQAJ'})
        lr = Template.create({
            'name': 'LandRover Hose', 'sku': 'LR-777', 'brand': land_rover.id,
            'list_price': 100.0, 'baf_discount_code': 'TESTQAJ'})
        # Both brands resolve to the same family-based sales column...
        self.assertEqual(jag.baf_sales_column_key, lr.baf_sales_column_key)
        # ...so a SINGLE discount line covers both brands.
        DiscountLine.create({
            'table_type': 'sales', 'column_key': '%s_GR1' % jag.baf_sales_column_key,
            'discount_code': 'TESTQAJ', 'discount_pct': 10.0})
        self.company_partner.sales_group_ids = [(6, 0, jlr_group.ids)]
        self.assertEqual(self._rows(self.company_partner, self.brand_company)['JAG-777'][1], 90.0)
        self.assertEqual(self._rows(self.company_partner, land_rover)['LR-777'][1], 90.0)
        self._assert_matches_engine(self.company_partner, self.brand_company, jag)
        self._assert_matches_engine(self.company_partner, land_rover, lr)

        # (b) BMW and MINI share the bmw_mini family but keep SEPARATE columns.
        mini_brand = Brand.create({
            'name': 'QA-MINI', 'is_public': True, 'family_id': self.fam_bmw.id})
        mini = Template.create({
            'name': 'MINI Pad', 'sku': 'MINI-777', 'brand': mini_brand.id,
            'list_price': 100.0, 'baf_discount_code': 'TESTQA1',
            'baf_type_code': 1, 'baf_mod': 'car'})
        self.assertNotEqual(self.bmw_car.baf_sales_column_key, mini.baf_sales_column_key)
        # TESTQA1/GR1 has a line only for BMW's column, so with a BMW GR1 group
        # the BMW part is discounted while the MINI part (own column, no line)
        # stays at full price.
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]
        self.assertEqual(self._rows(self.company_partner, self.brand_public)['BMW-001'][1], 85.0)
        self.assertEqual(self._rows(self.company_partner, mini_brand)['MINI-777'][1], 100.0)
        self._assert_matches_engine(self.company_partner, mini_brand, mini)

    def test_price_list_valid_across_contacts_and_children(self):
        """The whole price list stays engine-correct across several contacts:
        a company with a group, a child inheriting it, a child with its own
        group, and a standalone contact with no group."""
        Partner = self.env['res.partner']
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]

        child_inherit = self.child_partner          # no own group -> inherits company's
        child_inherit.sales_group_ids = [(6, 0, [])]
        child_own = Partner.create({                 # own groups override the company's
            'name': 'Child Own Group', 'parent_id': self.company_partner.id,
            'sales_group_ids': [(6, 0, (self.group_bmw_gr1 + self.group_bmw_moto).ids)]})
        standalone = Partner.create({'name': 'No Group Co', 'is_company': True})

        bmw_products = self.bmw_car + self.bmw_moto + self.bmw_uncoded
        for partner in (self.company_partner, child_inherit, child_own, standalone):
            self._assert_matches_engine(partner, self.brand_public, bmw_products)

        # Concrete anchors so engine and SQL can't silently drift together:
        self.assertEqual(self._rows(self.company_partner, self.brand_public)['BMW-001'][1], 85.0)
        self.assertEqual(self._rows(child_inherit, self.brand_public)['BMW-001'][1], 85.0)   # inherited
        self.assertEqual(self._rows(child_own, self.brand_public)['BMW-002'][1], 130.0)      # own moto group
        self.assertEqual(self._rows(standalone, self.brand_public)['BMW-001'][1], 105.0)     # no group -> full UPE

    def _download(self, brand_id):
        self.authenticate('pricefile_user', 'pricefile_user')
        return self.url_open(
            '/pricefile/download?brand_id=%s' % brand_id, allow_redirects=False
        )

    def _csv_rows(self, response):
        # requests (the client behind url_open) auto-decodes Content-Encoding:
        # gzip, same as a browser would, so response.content is already the
        # plain CSV bytes here, not the compressed wire payload.
        raw = response.content.decode('utf-8-sig')
        return list(csv.DictReader(io.StringIO(raw)))

    def test_download_returns_csv_for_visible_brand(self):
        # Pin the route -> pricefile_query -> engine wiring end to end: with
        # no group, full-UPE (105.0) would also result from passing the
        # route the wrong partner (None / public user / commercial_partner_id
        # instead of the actual one), so a real sales group is required to
        # make the price prove the correct partner was threaded through.
        self.company_partner.sales_group_ids = [(6, 0, self.group_bmw_gr1.ids)]

        response = self._download(self.brand_public.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response.headers['Content-Type'])
        self.assertEqual(response.headers['Content-Encoding'], 'gzip')

        expected_name = 'PriceList_%s_%s.csv' % (
            self.brand_public.name, date.today().isoformat())
        self.assertIn(expected_name, response.headers['Content-Disposition'])

        # response.content is normally already gunzipped by requests (see
        # _csv_rows), but gzip magic bytes (1f 8b) prove that either way here,
        # instead of assuming it.
        content = response.content
        if content[:2] == b'\x1f\x8b':
            content = gzip.decompress(content)
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'), 'missing utf-8-sig BOM')

        rows = self._csv_rows(response)
        self.assertEqual(
            list(rows[0].keys()), ['SKU', 'Description', 'Discounted Price']
        )
        by_sku = {r['SKU']: r for r in rows}
        # 100 * (1 - 20%) + 5 surcharge = 85.0, not the 105.0 full-UPE price.
        self.assertEqual(float(by_sku['BMW-001']['Discounted Price']), 85.0)
        self.assertEqual(by_sku['BMW-001']['Description'], 'BMW Brake Pad')

    def test_download_non_latin1_brand_name(self):
        # A brand name outside latin-1 (e.g. containing accented characters)
        # must not blow up werkzeug when building Content-Disposition.
        brand = self.env['product.brand'].create({'name': 'Škoda', 'is_public': True})
        self.env['product.template'].create({
            'name': 'Skoda Wiper Blade',
            'sku': 'SKO-001',
            'brand': brand.id,
            'list_price': 20.0,
        })
        response = self._download(brand.id)
        self.assertEqual(response.status_code, 200)

    def _assert_redirects_to_pricefile(self, response):
        # '/pricefile' is a substring of '/pricefile/download' too, so a bare
        # assertIn can't tell "authorization rejected the brand" apart from a
        # framework redirect back to the download URL itself. endswith (not
        # equality) still tolerates a lang-prefixed path like /de/pricefile.
        self.assertEqual(response.status_code, 303)
        location = response.headers['Location']
        self.assertTrue(urlparse(location).path.endswith('/pricefile'), location)
        self.assertNotIn('brand_id', location)

    def test_download_rejects_brand_the_partner_cannot_see(self):
        response = self._download(self.brand_hidden.id)
        self._assert_redirects_to_pricefile(response)

    def test_download_rejects_garbage_brand_id(self):
        response = self._download('not-a-number')
        self._assert_redirects_to_pricefile(response)

    def test_download_rejects_missing_brand_id(self):
        # No brand_id at all -> int(None) -> TypeError arm of the except.
        self.authenticate('pricefile_user', 'pricefile_user')
        response = self.url_open('/pricefile/download', allow_redirects=False)
        self._assert_redirects_to_pricefile(response)

    def test_download_rejects_negative_brand_id(self):
        response = self._download(-1)
        self._assert_redirects_to_pricefile(response)

    def test_pricefile_page_renders_form_and_visible_brands(self):
        # Pins the template-to-route seam: the view/template id, the form's
        # action, the select's name and the brand options are otherwise
        # untested, so any of them could be renamed or removed with all
        # download tests still green.
        self.authenticate('pricefile_user', 'pricefile_user')
        response = self.url_open('/pricefile')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8')
        self.assertIn('action="/pricefile/download"', body)
        self.assertIn('name="brand_id"', body)
        self.assertIn(
            '<option value="%d">%s</option>' % (self.brand_company.id, self.brand_company.name),
            body,
        )
        self.assertNotIn('value="%d"' % self.brand_hidden.id, body)
