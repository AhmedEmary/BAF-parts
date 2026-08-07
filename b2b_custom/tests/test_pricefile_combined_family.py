import base64
import csv
import gzip
import io
from datetime import date
from urllib.parse import urlparse

from odoo.tests import HttpCase, tagged

from odoo.addons.b2b_custom.controllers.pricefile import (
    pricefile_query_for_family,
    visible_families,
)


@tagged('post_install', '-at_install')
class TestCombinedPricefileAndEtk(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Family = cls.env['baf.brand.family']
        cls.fam_bmw = Family.create({'name': 'C47 BMW/MINI'})
        cls.fam_jlr = Family.create({'name': 'C47 JLR'})

        Brand = cls.env['product.brand']
        cls.brand_bmw = Brand.create({
            'name': 'C47-BMW', 'is_public': True, 'family_id': cls.fam_bmw.id})
        cls.brand_mini = Brand.create({
            'name': 'C47-MINI', 'is_public': True, 'family_id': cls.fam_bmw.id})
        cls.brand_jaguar = Brand.create({
            'name': 'C47-Jaguar', 'is_public': False, 'family_id': cls.fam_jlr.id})
        cls.brand_lr = Brand.create({
            'name': 'C47-Land Rover', 'is_public': False, 'family_id': cls.fam_jlr.id})
        cls.brand_hidden = Brand.create({
            'name': 'C47-Hidden', 'is_public': False})

        cls.company = cls.env['res.partner'].create({
            'name': 'C47 Test B2B Company',
            'is_company': True,
            'visible_brand_ids': [(6, 0, [cls.brand_jaguar.id, cls.brand_lr.id])],
        })

        Template = cls.env['product.template']
        cls.bmw_part = Template.create({
            'name': 'C47 BMW Brake Pad',
            'sku': 'C47-BMW-1',
            'brand': cls.brand_bmw.id,
            'list_price': 100.0,
        })
        cls.mini_part = Template.create({
            'name': 'C47 MINI Clutch',
            'sku': 'C47-MINI-1',
            'brand': cls.brand_mini.id,
            'list_price': 120.0,
        })
        cls.jag_part = Template.create({
            'name': 'C47 Jaguar Filter',
            'sku': 'C47-JAG-1',
            'brand': cls.brand_jaguar.id,
            'list_price': 80.0,
        })
        cls.lr_part = Template.create({
            'name': 'C47 Land Rover Belt',
            'sku': 'C47-LR-1',
            'brand': cls.brand_lr.id,
            'list_price': 90.0,
        })

        cls.user = cls.env['res.users'].create({
            'name': 'C47 Pricefile User',
            'login': 'c47_pricefile_user',
            'password': 'c47_pricefile_user',
            'partner_id': cls.company.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_portal').id,
                cls.env.ref('b2b_custom.group_b2b_customer').id,
            ])],
        })

    def _visible_family_ids(self):
        return set(visible_families(self.env, self.company).ids)

    def test_visible_families_covers_public_and_gated(self):
        # BMW/MINI via the public brands; JLR via visible_brand_ids.
        family_ids = self._visible_family_ids()
        self.assertIn(self.fam_bmw.id, family_ids)
        self.assertIn(self.fam_jlr.id, family_ids)

    def test_visible_families_omits_families_with_no_visible_brand(self):
        family_ids = self._visible_family_ids()
        self.assertNotIn(self.brand_hidden.family_id.id, family_ids)

    # ── Combined query ─────────────────────────────────────────────────────
    def test_family_query_covers_every_brand_in_the_family(self):
        # A BMW/MINI download must produce rows for BOTH brands in one file.
        sql, params = pricefile_query_for_family(
            self.company, self.fam_bmw, 'en_US')
        self.assertIsNotNone(sql)
        self.env.flush_all()
        self.env.cr.execute(sql, params)
        skus = {row[0] for row in self.env.cr.fetchall()}
        self.assertIn('C47-BMW-1', skus)
        self.assertIn('C47-MINI-1', skus)

    def test_family_query_excludes_brands_outside_family(self):
        sql, params = pricefile_query_for_family(
            self.company, self.fam_jlr, 'en_US')
        self.env.flush_all()
        self.env.cr.execute(sql, params)
        skus = {row[0] for row in self.env.cr.fetchall()}
        self.assertIn('C47-JAG-1', skus)
        self.assertIn('C47-LR-1', skus)
        self.assertNotIn('C47-BMW-1', skus)

    def _download_family(self, family_id):
        self.authenticate('c47_pricefile_user', 'c47_pricefile_user')
        return self.url_open(
            '/pricefile/family/download?family_id=%s' % family_id,
            allow_redirects=False,
        )

    def test_family_download_returns_csv_with_expected_rows(self):
        response = self._download_family(self.fam_bmw.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response.headers['Content-Type'])
        # Content-Disposition percent-encodes `/` and spaces, so assert the
        # fixed prefix + today's date rather than the whole literal.
        cd = response.headers['Content-Disposition']
        self.assertIn('PriceList_', cd)
        self.assertIn(date.today().isoformat(), cd)
        content = response.content
        if content[:2] == b'\x1f\x8b':
            content = gzip.decompress(content)
        rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
        skus = {r['SKU'] for r in rows}
        self.assertIn('C47-BMW-1', skus)
        self.assertIn('C47-MINI-1', skus)

    def test_family_download_rejects_family_the_partner_cant_see(self):
        other_family = self.env['baf.brand.family'].create(
            {'name': 'C47 Other'})
        self.env['product.brand'].create(
            {'name': 'C47-Other', 'family_id': other_family.id})
        response = self._download_family(other_family.id)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            urlparse(response.headers['Location']).path.endswith('/pricefile'))

    def test_family_download_rejects_garbage_id(self):
        response = self._download_family('not-a-number')
        self.assertEqual(response.status_code, 303)

    def test_pricefile_page_lists_families(self):
        self.authenticate('c47_pricefile_user', 'c47_pricefile_user')
        response = self.url_open('/pricefile')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8')
        self.assertIn('action="/pricefile/family/download"', body)
        self.assertIn('name="family_id"', body)
        self.assertIn(
            '<option value="%d">%s</option>' % (self.fam_bmw.id, self.fam_bmw.name),
            body,
        )

    def test_etk_current_returns_latest_active(self):
        Etk = self.env['baf.etk.file']
        self.assertFalse(Etk._baf_current())
        older = Etk.create({
            'name': 'Old ETK', 'file_name': 'old.bin',
            'file_data': base64.b64encode(b'older-payload'),
        })
        newer = Etk.create({
            'name': 'New ETK', 'file_name': 'new.bin',
            'file_data': base64.b64encode(b'newer-payload'),
        })
        self.assertEqual(Etk._baf_current(), newer)
        newer.active = False
        self.assertEqual(Etk._baf_current(), older)

    def test_etk_download_serves_current_file(self):
        etk = self.env['baf.etk.file'].create({
            'name': 'Test ETK',
            'file_name': 'etk.bin',
            'file_data': base64.b64encode(b'etk-payload'),
        })
        self.authenticate('c47_pricefile_user', 'c47_pricefile_user')
        response = self.url_open('/pricefile/etk', allow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn('etk.bin', response.headers['Content-Disposition'])
        self.assertEqual(response.content, b'etk-payload')
        etk.unlink()

    def test_etk_download_redirects_when_no_file_available(self):
        self.env['baf.etk.file'].sudo().search([]).unlink()
        self.authenticate('c47_pricefile_user', 'c47_pricefile_user')
        response = self.url_open('/pricefile/etk', allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            urlparse(response.headers['Location']).path.endswith('/pricefile'))

    def test_pricefile_page_shows_etk_download_only_when_uploaded(self):
        self.env['baf.etk.file'].sudo().search([]).unlink()
        self.authenticate('c47_pricefile_user', 'c47_pricefile_user')
        response = self.url_open('/pricefile')
        self.assertNotIn('/pricefile/etk', response.content.decode('utf-8'))
        self.env['baf.etk.file'].create({
            'name': 'Test ETK', 'file_name': 'etk.bin',
            'file_data': base64.b64encode(b'etk-payload'),
        })
        response = self.url_open('/pricefile')
        self.assertIn('/pricefile/etk', response.content.decode('utf-8'))
