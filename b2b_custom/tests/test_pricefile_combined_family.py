import base64
import csv
import gzip
import io
from datetime import date
from urllib.parse import urlparse

from psycopg2 import IntegrityError

from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

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
    def _skus_for_family(self, family):
        sql, params = pricefile_query_for_family(self.company, family, 'en_US')
        self.assertIsNotNone(sql)
        self.env.flush_all()
        self.env.cr.execute(sql, params)
        cols = [d.name for d in self.env.cr.description]
        sku_idx = cols.index('SKU')
        return {row[sku_idx] for row in self.env.cr.fetchall()}

    def test_family_query_covers_every_brand_in_the_family(self):
        skus = self._skus_for_family(self.fam_bmw)
        self.assertIn('C47-BMW-1', skus)
        self.assertIn('C47-MINI-1', skus)

    def test_family_query_excludes_brands_outside_family(self):
        skus = self._skus_for_family(self.fam_jlr)
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

    def test_etk_current_is_per_family(self):
        Etk = self.env['baf.etk.file']
        self.assertFalse(Etk._baf_current(self.fam_bmw))
        bmw = Etk.create({
            'name': 'BMW ETK', 'file_name': 'bmw.bin',
            'family_id': self.fam_bmw.id,
            'file_data': base64.b64encode(b'bmw-payload'),
        })
        jlr = Etk.create({
            'name': 'JLR ETK', 'file_name': 'jlr.bin',
            'family_id': self.fam_jlr.id,
            'file_data': base64.b64encode(b'jlr-payload'),
        })
        self.assertEqual(Etk._baf_current(self.fam_bmw), bmw)
        self.assertEqual(Etk._baf_current(self.fam_jlr), jlr)
        bmw.active = False
        self.assertFalse(Etk._baf_current(self.fam_bmw))
        self.assertFalse(Etk._baf_current(self.env['baf.brand.family']))

    def test_etk_one_file_per_family(self):
        Etk = self.env['baf.etk.file']
        Etk.create({
            'name': 'BMW ETK', 'file_name': 'bmw.bin',
            'family_id': self.fam_bmw.id,
            'file_data': base64.b64encode(b'bmw-payload'),
        })
        with self.assertRaises(IntegrityError):
            with mute_logger('odoo.sql_db'):
                Etk.create({
                    'name': 'BMW ETK 2', 'file_name': 'bmw2.bin',
                    'family_id': self.fam_bmw.id,
                    'file_data': base64.b64encode(b'bmw-payload-2'),
                })
                self.env.flush_all()

    def _download_etk(self, family_id):
        self.authenticate('c47_pricefile_user', 'c47_pricefile_user')
        return self.url_open(
            '/pricefile/etk?family_id=%s' % family_id, allow_redirects=False)

    def test_etk_download_serves_the_family_file(self):
        etk = self.env['baf.etk.file'].create({
            'name': 'BMW ETK',
            'file_name': 'bmw-etk.bin',
            'family_id': self.fam_bmw.id,
            'file_data': base64.b64encode(b'bmw-etk-payload'),
        })
        response = self._download_etk(self.fam_bmw.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'etk_c47_bmw_mini.bin', response.headers['Content-Disposition'])
        self.assertEqual(response.content, b'bmw-etk-payload')
        etk.unlink()

    def test_etk_download_redirects_when_family_has_no_file(self):
        self.env['baf.etk.file'].sudo().search([]).unlink()
        response = self._download_etk(self.fam_bmw.id)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            urlparse(response.headers['Location']).path.endswith('/pricefile'))

    def test_etk_download_rejects_family_the_partner_cant_see(self):
        other_family = self.env['baf.brand.family'].create(
            {'name': 'C47 ETK Other'})
        self.env['product.brand'].create(
            {'name': 'C47-ETK-Other', 'family_id': other_family.id})
        self.env['baf.etk.file'].create({
            'name': 'Other ETK', 'file_name': 'other.bin',
            'family_id': other_family.id,
            'file_data': base64.b64encode(b'other-payload'),
        })
        response = self._download_etk(other_family.id)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(
            urlparse(response.headers['Location']).path.endswith('/pricefile'))

    def test_etk_download_rejects_garbage_id(self):
        response = self._download_etk('not-a-number')
        self.assertEqual(response.status_code, 303)

    def _etk_form_options(self, body):
        """The <option>s of the ETK form only — the combined-pricefile form
        above it lists every visible family, ETK file or not."""
        start = body.find('action="/pricefile/etk"')
        if start == -1:
            return ''
        return body[start:body.index('</form>', start)]

    def test_pricefile_page_lists_only_families_with_a_file(self):
        self.env['baf.etk.file'].sudo().search([]).unlink()
        self.authenticate('c47_pricefile_user', 'c47_pricefile_user')
        body = self.url_open('/pricefile').content.decode('utf-8')
        self.assertNotIn('action="/pricefile/etk"', body)

        self.env['baf.etk.file'].create({
            'name': 'BMW ETK', 'file_name': 'bmw-etk.bin',
            'family_id': self.fam_bmw.id,
            'file_data': base64.b64encode(b'bmw-etk-payload'),
        })
        options = self._etk_form_options(
            self.url_open('/pricefile').content.decode('utf-8'))
        self.assertIn(
            '<option value="%d">%s</option>' % (self.fam_bmw.id, self.fam_bmw.name),
            options)
        self.assertNotIn(
            '<option value="%d">%s</option>' % (self.fam_jlr.id, self.fam_jlr.name),
            options)

        self.env['baf.etk.file'].create({
            'name': 'JLR ETK', 'file_name': 'jlr-etk.bin',
            'family_id': self.fam_jlr.id,
            'file_data': base64.b64encode(b'jlr-etk-payload'),
        })
        options = self._etk_form_options(
            self.url_open('/pricefile').content.decode('utf-8'))
        self.assertIn(
            '<option value="%d">%s</option>' % (self.fam_bmw.id, self.fam_bmw.name),
            options)
        self.assertIn(
            '<option value="%d">%s</option>' % (self.fam_jlr.id, self.fam_jlr.name),
            options)

    def test_etk_file_is_renamed_after_its_family(self):
        etk = self.env['baf.etk.file'].create({
            'name': 'BMW ETK',
            'file_name': 'Katalog 2026.zip',
            'family_id': self.fam_bmw.id,
            'file_data': base64.b64encode(b'bmw-etk-payload'),
        })
        # 'C47 BMW/MINI' -> every run of non-alphanumerics becomes one '_'.
        self.assertEqual(etk.file_name, 'etk_c47_bmw_mini.zip')

        etk.write({'family_id': self.fam_jlr.id})
        self.assertEqual(etk.file_name, 'etk_c47_jlr.zip')

        etk.write({'file_name': 'Neuer Katalog.7z'})
        self.assertEqual(etk.file_name, 'etk_c47_jlr.7z')

    def test_etk_download_uses_the_renamed_file(self):
        self.env['baf.etk.file'].create({
            'name': 'BMW ETK',
            'file_name': 'Katalog 2026.zip',
            'family_id': self.fam_bmw.id,
            'file_data': base64.b64encode(b'bmw-etk-payload'),
        })
        response = self._download_etk(self.fam_bmw.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'etk_c47_bmw_mini.zip', response.headers['Content-Disposition'])
