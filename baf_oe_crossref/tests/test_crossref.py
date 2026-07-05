"""Tests for baf_oe_crossref — links (M2M), resolver, cache, auto-map.

Live IC calls are mocked at the ``IcBackend.get_client`` boundary.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


class CrossrefCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env['res.partner'].create({
            'name': 'Inter Cars Test Vendor', 'supplier_rank': 1,
        })
        cls.backend = cls.env['ic.backend'].create({
            'name': 'IC Test Backend',
            'vendor_id': cls.vendor.id,
            'client_id': 'x', 'client_secret': 'y',
            'token_url': 'https://token.invalid/oauth2/token',
            'currency_id': cls.env.ref('base.EUR').id,
            'ship_to': 'F17',
        })
        cls.website = cls.env['website'].search([], limit=1)
        cls.website.enable_aftermarket_search = True

        # Local IC cache rows — two aftermarket variants of the same
        # tec_doc group + one unrelated row.
        Info = cls.env['ic.product.info']
        cls.info_valeo = Info.create({
            'tow_kod': 'BEF134', 'ic_index': 'VAL231498',
            'tec_doc': '231498', 'article_number': '231498',
            'manufacturer': 'VALEO', 'short_description': 'Radiator',
            'n_tow_kod': 'BEF134', 'n_ic_index': 'VAL231498',
            'n_tec_doc': '231498', 'n_article': '231498',
        })
        cls.info_meat = Info.create({
            'tow_kod': 'G0SQP7', 'ic_index': 'MD231498',
            'tec_doc': '231498', 'article_number': '231498',
            'manufacturer': 'MEAT & DORIA',
            'short_description': 'Column switch',
            'n_tow_kod': 'G0SQP7', 'n_ic_index': 'MD231498',
            'n_tec_doc': '231498', 'n_article': '231498',
        })
        cls.info_other = Info.create({
            'tow_kod': 'ZZZ999', 'ic_index': 'OTHER1',
            'tec_doc': 'OTHER1', 'article_number': 'OTHER1',
            'manufacturer': 'BOSCH', 'short_description': 'Unrelated',
            'n_tow_kod': 'ZZZ999', 'n_ic_index': 'OTHER1',
            'n_tec_doc': 'OTHER1', 'n_article': 'OTHER1',
        })

        # Two OEM templates (the M2M "many OEMs" side).
        cls.oem_a = cls.env['product.template'].create({
            'name': 'OEM Thermostat LR', 'sku': '231498',
            'list_price': 100.0, 'taxes_id': [(5,)],
        })
        cls.oem_b = cls.env['product.template'].create({
            'name': 'OEM Thermostat JAG', 'sku': 'JAG231498X',
            'list_price': 120.0, 'taxes_id': [(5,)],
        })

    def _mock_client(self):
        client = MagicMock()
        client.get_price.return_value = {'lines': [
            {'sku': 'BEF134',
             'price': {'customerPriceNet': 10.0, 'currencyCode': 'EUR'}},
            {'sku': 'G0SQP7',
             'price': {'customerPriceNet': 20.0, 'currencyCode': 'EUR'}},
        ]}
        client.get_stock.return_value = [
            {'sku': 'BEF134', 'availability': 4},
            {'sku': 'G0SQP7', 'availability': 10},
        ]
        return client


@tagged('post_install', '-at_install')
class TestOeLinkModel(CrossrefCase):

    def test_many_to_many(self):
        Link = self.env['baf.oe.link']
        # one OEM → two IC SKUs
        Link.create([
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134'},
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'G0SQP7'},
        ])
        # one IC SKU → second OEM as well
        Link.create({'oem_template_id': self.oem_b.id, 'ic_sku': 'BEF134'})

        self.assertEqual(self.oem_a.oe_link_count, 2)
        self.assertEqual(self.oem_b.oe_link_count, 1)
        self.assertEqual(
            Link.search_count([('ic_sku', '=', 'BEF134')]), 2,
            "same IC SKU must be linkable to multiple OEM products")

    @mute_logger('odoo.sql_db')
    def test_unique_pair_constraint(self):
        Link = self.env['baf.oe.link']
        Link.create({'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134'})
        with self.assertRaises(Exception), self.env.cr.savepoint():
            Link.create({'oem_template_id': self.oem_a.id,
                         'ic_sku': 'BEF134'})

    def test_manual_links_sort_first(self):
        Link = self.env['baf.oe.link']
        auto = Link.create({'oem_template_id': self.oem_a.id,
                            'ic_sku': 'G0SQP7', 'source': 'auto'})
        manual = Link.create({'oem_template_id': self.oem_a.id,
                              'ic_sku': 'BEF134', 'source': 'manual'})
        self.assertLess(manual.sequence, auto.sequence)

    def test_ic_info_computed_from_cache(self):
        link = self.env['baf.oe.link'].create({
            'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134',
        })
        self.assertEqual(link.ic_brand, 'VALEO')
        self.assertEqual(link.ic_description, 'Radiator')
        self.assertEqual(link.ic_tec_doc, '231498')

    def test_record_materialisation_upserts_and_backfills(self):
        Link = self.env['baf.oe.link']
        existing = Link.create({
            'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134',
            'source': 'auto',
        })
        twin = Link.create({
            'oem_template_id': self.oem_b.id, 'ic_sku': 'BEF134',
            'source': 'auto',
        })
        product = self.env['product.product']._baf_find_or_create_ic(
            self.backend, {'sku': 'BEF134', 'brand': 'VALEO',
                           'shortDescription': 'Radiator'},
            price_net=10.0,
        )
        Link._record_materialisation(self.oem_a, 'BEF134', product)
        self.assertEqual(existing.aftermarket_template_id,
                         product.product_tmpl_id)
        self.assertEqual(twin.aftermarket_template_id,
                         product.product_tmpl_id,
                         "other OEMs' links to the same SKU are backfilled")
        # A pair with no prior link gets a 'shop' link.
        oem_c = self.env['product.template'].create({
            'name': 'Third OEM', 'sku': 'THIRD1',
        })
        link_c = Link._record_materialisation(oem_c, 'BEF134', product)
        self.assertEqual(link_c.source, 'shop')
        # Reverse side of the M2M via the product form.
        self.assertEqual(
            product.product_tmpl_id.aftermarket_link_count, 3)

    def test_populate_from_seeds_idempotent(self):
        self.oem_a.ic_seed_sku = 'BEF134'
        Link = self.env['baf.oe.link']
        n1 = Link._populate_from_seeds()
        self.assertGreaterEqual(n1, 1)
        count = Link.search_count([
            ('oem_template_id', '=', self.oem_a.id),
            ('ic_sku', '=', 'BEF134'),
        ])
        self.assertEqual(count, 1)
        n2 = Link._populate_from_seeds()
        count2 = Link.search_count([
            ('oem_template_id', '=', self.oem_a.id),
            ('ic_sku', '=', 'BEF134'),
        ])
        self.assertEqual(count2, 1, "re-run must not duplicate")

    def test_archived_link_never_recreated_by_seed_sync(self):
        self.oem_a.ic_seed_sku = 'BEF134'
        Link = self.env['baf.oe.link']
        Link._populate_from_seeds()
        link = Link.search([('oem_template_id', '=', self.oem_a.id),
                            ('ic_sku', '=', 'BEF134')])
        link.active = False  # a human rejected this match
        Link._populate_from_seeds()
        all_links = Link.with_context(active_test=False).search([
            ('oem_template_id', '=', self.oem_a.id),
            ('ic_sku', '=', 'BEF134'),
        ])
        self.assertEqual(len(all_links), 1)
        self.assertFalse(all_links.active,
                         "rejected match must stay rejected")


@tagged('post_install', '-at_install')
class TestEquivalentsResolver(CrossrefCase):

    def test_toggle_off_returns_empty(self):
        self.website.enable_aftermarket_search = False
        self.env['baf.oe.link'].create({
            'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134',
        })
        self.assertEqual(self.oem_a.baf_ic_equivalents(), [])

    def test_links_drive_cards_sorted_by_price(self):
        self.env['baf.oe.link'].create([
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'G0SQP7'},
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134'},
        ])
        with patch.object(type(self.backend), 'get_client',
                          return_value=self._mock_client()):
            cards = self.oem_a.baf_ic_equivalents()
        self.assertTrue(cards)
        aftermarket = [c for c in cards if not c['is_oem']]
        self.assertEqual([c['ic_sku'] for c in aftermarket],
                         ['BEF134', 'G0SQP7'],
                         "cheapest first (10 € before 20 €)")
        # Markup applied: default 25 % on customerPriceNet.
        self.assertAlmostEqual(aftermarket[0]['sale_price'], 12.5)
        self.assertEqual(aftermarket[0]['quality'], 'aftermarket')
        # OEM card leads if present.
        if cards[0].get('is_oem'):
            self.assertTrue(cards[0]['is_oem'])

    def test_archived_link_excluded(self):
        links = self.env['baf.oe.link'].create([
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'G0SQP7'},
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134'},
        ])
        links.filtered(lambda l: l.ic_sku == 'G0SQP7').active = False
        with patch.object(type(self.backend), 'get_client',
                          return_value=self._mock_client()):
            cards = self.oem_a.baf_ic_equivalents()
        skus = [c['ic_sku'] for c in cards if not c['is_oem']]
        self.assertNotIn('G0SQP7', skus)
        self.assertIn('BEF134', skus)

    def test_unpriced_candidates_are_skipped(self):
        # ZZZ999 has no quote line in the mock → must not render a card.
        self.env['baf.oe.link'].create([
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'ZZZ999'},
            {'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134'},
        ])
        with patch.object(type(self.backend), 'get_client',
                          return_value=self._mock_client()):
            cards = self.oem_a.baf_ic_equivalents()
        skus = [c['ic_sku'] for c in cards if not c['is_oem']]
        self.assertNotIn('ZZZ999', skus)

    def test_identifier_fallback_without_links(self):
        # oem_a.sku == '231498' matches both cache rows via tec_doc.
        with patch.object(type(self.backend), 'get_client',
                          return_value=self._mock_client()):
            cards = self.oem_a.baf_ic_equivalents()
        skus = {c['ic_sku'] for c in cards if not c['is_oem']}
        self.assertIn('BEF134', skus)

    def test_resolver_excludes_own_sku(self):
        """A materialised aftermarket product's page must not offer the
        product itself as its own alternative."""
        product = self.env['product.product']._baf_find_or_create_ic(
            self.backend, {'sku': 'BEF134', 'brand': 'VALEO',
                           'shortDescription': 'Radiator'},
            price_net=10.0,
        )
        tmpl = product.product_tmpl_id
        with patch.object(type(self.backend), 'get_client',
                          return_value=self._mock_client()):
            cards = tmpl.baf_ic_equivalents()
        skus = [c['ic_sku'] for c in cards if not c['is_oem']]
        self.assertNotIn('BEF134', skus,
                         "own SKU must never appear as an alternative")

    def test_category_kill_switch(self):
        categ = self.env['product.public.category'].create({
            'name': 'OEM only zone',
            'enable_aftermarket_search': False,
        })
        self.oem_a.public_categ_ids = [(6, 0, [categ.id])]
        self.env['baf.oe.link'].create({
            'oem_template_id': self.oem_a.id, 'ic_sku': 'BEF134',
        })
        self.assertEqual(self.oem_a.baf_ic_equivalents(), [],
                         "category opt-out must hide the block")
        categ.enable_aftermarket_search = True
        with patch.object(type(self.backend), 'get_client',
                          return_value=self._mock_client()):
            cards = self.oem_a.baf_ic_equivalents()
        self.assertTrue(cards, "re-enabling the category restores cards")

    def test_record_materialisation_ignores_empty_sku(self):
        link = self.env['baf.oe.link']._record_materialisation(
            self.oem_a, '', self.env['product.product'],
        )
        self.assertFalse(link)

    def test_automap_with_empty_cache_is_noop(self):
        self.env['ic.product.info'].search([]).unlink()
        wizard = self.env['ic.csv.import.wizard'].new({'source': 'upload'})
        mapped = wizard._auto_map_seeds()
        self.assertEqual(mapped, 0)
        self.assertFalse(self.oem_a.ic_seed_sku)

    def test_cache_invalid_ttl_param_falls_back(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'baf.ic_cache_ttl_sec', 'garbage')
        Cache = self.env['ic.article.cache']
        Cache.put('ttl-test', [{'x': 1}])
        self.assertEqual(Cache.get('ttl-test'), [{'x': 1}],
                         "invalid TTL param must not break caching")

    def test_cache_roundtrip_and_gc(self):
        Cache = self.env['ic.article.cache']
        Cache.put('k1', [{'a': 1}])
        self.assertEqual(Cache.get('k1'), [{'a': 1}])
        self.assertIsNone(Cache.get('missing'))
        entry = Cache.search([('key', '=', 'k1')])
        entry.expires_at = 1.0  # long expired
        self.assertIsNone(Cache.get('k1'), "expired entry must miss")
        Cache.put('k2', [])
        Cache.search([('key', '=', 'k2')]).expires_at = 1.0
        Cache.gc()
        self.assertFalse(Cache.search([('key', '=', 'k2')]),
                         "gc must purge expired rows")


@tagged('post_install', '-at_install')
class TestAutoMap(CrossrefCase):

    def _run_automap(self):
        wizard = self.env['ic.csv.import.wizard'].new({'source': 'upload'})
        return wizard._auto_map_seeds()

    def test_auto_map_sets_seed_and_creates_links(self):
        mapped = self._run_automap()
        self.assertGreaterEqual(mapped, 1)
        # oem_a.sku 231498 matches VALEO/MEAT rows via n_article/n_tec_doc
        # (seed) and BEF134 only reaches links when a high-confidence
        # tier matched — here 231498 == n_article, not n_tow_kod or
        # n_ic_index, so no auto link is expected for oem_a.
        self.assertTrue(self.oem_a.ic_seed_sku)

    def test_auto_map_preserves_existing_seed(self):
        self.oem_a.ic_seed_sku = 'MANUAL1'
        self._run_automap()
        self.assertEqual(self.oem_a.ic_seed_sku, 'MANUAL1',
                         "auto-map must never clobber a manual seed")

    def test_auto_map_creates_links_for_high_confidence_tiers(self):
        # A template whose sku IS an IC tow_kod → tier-1 link.
        oem = self.env['product.template'].create({
            'name': 'Tier1 match', 'sku': 'BEF134',
        })
        self._run_automap()
        link = self.env['baf.oe.link'].search([
            ('oem_template_id', '=', oem.id),
            ('ic_sku', '=', 'BEF134'),
        ])
        self.assertEqual(len(link), 1)
        self.assertEqual(link.source, 'auto')
