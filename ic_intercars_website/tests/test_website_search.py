"""Tests for the customer-facing IC catalog search + materialisation.

Same offline strategy as ic_intercars' own suite: all IC HTTP traffic
is mocked at the client boundary.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

_SAMPLE_CSV = (
    "TOW_KOD;IC_INDEX;TEC_DOC;TEC_DOC_PROD;ARTICLE_NUMBER;MANUFACTURER;"
    "SHORT_DESCRIPTION;DESCRIPTION;BARCODES;PACKAGE_WEIGHT;PACKAGE_LENGTH;"
    "PACKAGE_WIDTH;PACKAGE_HEIGHT;CUSTOM_CODE;BLOCKED_RETURN\n"
    "ADDFFF;OP 520;OP 520;256;OP 520;FILTRON;Oil filter;"
    "Oil filter long descr;5904608005205;0,378;15,0;10,0;10,0;84212300;false\n"
    "G14740;61 13 1 359 287;61131359287;9999;61131359287;OE BMW;"
    "Battery clamp;Battery clamp for BMW;4010858481162;0,1;5,0;5,0;5,0;85369010;false\n"
    "BEF134;VAL231498;231498;21;231498;VALEO;Radiator;"
    "Engine cooling radiator;;;;;;87089135;true\n"
).encode()


@tagged('post_install', '-at_install')
class TestIcWebsiteSearch(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env['res.partner'].create({
            'name': 'IC Web Test Vendor',
            'supplier_rank': 1,
        })
        cls.backend = cls.env['ic.backend'].create({
            'name': 'IC Web Test Backend',
            'vendor_id': cls.vendor.id,
            'client_id': 'test-client',
            'client_secret': 'test-secret',
            'token_url': 'https://token.invalid/oauth2/token',
            'currency_id': cls.env.ref('base.EUR').id,
            'ship_to': 'F17',
            'market': 'de',
        })
        cls.Info = cls.env['ic.product.info']
        cls.Info.bulk_load_csv(_SAMPLE_CSV, replace=True)

    def _mock_client(self, price_lines=None):
        client = MagicMock()
        client.get_price.return_value = {'lines': price_lines or []}
        return client

    # ── normalised keys ─────────────────────────────────────────────────
    def test_norm_keys_populated_after_bulk_load(self):
        row = self.Info.search([('tow_kod', '=', 'G14740')])
        self.assertEqual(row.norm_index, '61131359287')
        self.assertEqual(row.norm_article, '61131359287')
        row2 = self.Info.search([('tow_kod', '=', 'ADDFFF')])
        self.assertEqual(row2.norm_index, 'OP520')
        self.assertEqual(row2.norm_barcodes, '5904608005205')

    # ── search tiers ────────────────────────────────────────────────────
    def test_search_ignores_spacing_and_case(self):
        for query in ('OP 520', 'op520', 'op-520'):
            rows = self.Info._baf_website_search(query)
            self.assertIn('ADDFFF', rows.mapped('tow_kod'),
                          "query %r must hit the OP 520 row" % query)

    def test_search_spaced_oe_number(self):
        rows = self.Info._baf_website_search('61 13 1 359 287')
        self.assertEqual(rows.mapped('tow_kod'), ['G14740'])

    def test_search_by_ean(self):
        rows = self.Info._baf_website_search('4010858481162')
        self.assertEqual(rows.mapped('tow_kod'), ['G14740'])

    def test_search_prefix(self):
        rows = self.Info._baf_website_search('VAL231')
        self.assertEqual(rows.mapped('tow_kod'), ['BEF134'])

    def test_search_description_fallback(self):
        rows = self.Info._baf_website_search('Radiator')
        self.assertIn('BEF134', rows.mapped('tow_kod'))

    def test_search_short_queries_return_nothing(self):
        self.assertFalse(self.Info._baf_website_search('OP'))
        self.assertFalse(self.Info._baf_website_search(''))
        self.assertFalse(self.Info._baf_website_search(None))

    # ── materialisation ─────────────────────────────────────────────────
    def test_materialize_creates_published_product(self):
        row = self.Info.search([('tow_kod', '=', 'ADDFFF')])
        client = self._mock_client([{
            'sku': 'ADDFFF',
            'price': {'customerPriceNet': 3.05, 'currencyCode': 'EUR'},
        }])
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            variant = row._baf_materialize_for_website()
        template = variant.product_tmpl_id
        self.assertEqual(template.ic_sku, 'ADDFFF')
        self.assertTrue(template.is_published,
                        "a website-materialised product must be visible")
        self.assertGreater(template.list_price, 3.05)

    def test_materialize_republishes_existing(self):
        row = self.Info.search([('tow_kod', '=', 'ADDFFF')])
        client = self._mock_client([{
            'sku': 'ADDFFF',
            'price': {'customerPriceNet': 3.05, 'currencyCode': 'EUR'},
        }])
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            first = row._baf_materialize_for_website()
            first.product_tmpl_id.is_published = False
            second = row._baf_materialize_for_website()
        self.assertEqual(first, second, "same SKU must reuse the product")
        self.assertTrue(second.product_tmpl_id.is_published)

    def test_materialize_unquotable_raises_and_creates_nothing(self):
        row = self.Info.search([('tow_kod', '=', 'BEF134')])
        client = self._mock_client([])  # IC declines to quote
        with patch.object(type(self.backend), 'get_client',
                          return_value=client):
            with self.assertRaises(UserError):
                row._baf_materialize_for_website()
        self.assertFalse(
            self.env['product.product'].with_context(
                active_test=False).search([('ic_sku', '=', 'BEF134')]),
            "no product may exist for an unquotable SKU")
