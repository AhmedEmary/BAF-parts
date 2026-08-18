import re

from odoo.tests import TransactionCase, tagged

_SI_TABLE_RE = re.compile(r'\bproduct_supplierinfo\b', re.IGNORECASE)


@tagged('post_install', '-at_install')
class TestVendorDirectPricesButton(TransactionCase):
    """The vendor form must show a count, never load the supplierinfo rows."""

    def setUp(self):
        super().setUp()
        Partner = self.env['res.partner']
        Tmpl = self.env['product.template']
        self.brand = self.env['product.brand'].create({'name': 'ZZBRAND'})
        self.vendor = Partner.create({
            'name': 'ZZ Direct Vendor',
            'baf_is_vendor': True,
            'baf_purchase_method': 'direct',
        })
        self.other_vendor = Partner.create({
            'name': 'ZZ Other Vendor',
            'baf_is_vendor': True,
            'baf_purchase_method': 'direct',
        })
        self.templates = Tmpl.create([{
            'name': 'ZZ Part %d' % i,
            'default_code': 'ZZDP_%03d' % i,
            'sku': 'ZZSKU%03d' % i,
            'brand': self.brand.id,
            'list_price': 100.0,
        } for i in range(12)])
        self.env['product.supplierinfo'].create([{
            'partner_id': self.vendor.id,
            'product_tmpl_id': tmpl.id,
            'price': 50.0 + i,
        } for i, tmpl in enumerate(self.templates)])
        self.env['product.supplierinfo'].create({
            'partner_id': self.other_vendor.id,
            'product_tmpl_id': self.templates[0].id,
            'price': 999.0,
        })

    def _form_arch(self):
        view = self.env.ref(
            'general_system_custom.view_partner_form_inherit_trusted_vendor')
        return self.env['res.partner'].get_view(view.id, 'form')['arch']

    def test_inline_field_removed(self):
        self.assertNotIn(
            'baf_supplierinfo_ids', self.env['res.partner']._fields)

    def test_form_arch_shows_button_not_list(self):
        arch = self._form_arch()
        self.assertNotIn('baf_supplierinfo_ids', arch)
        self.assertIn('baf_direct_price_count', arch)
        self.assertIn('action_baf_view_direct_prices', arch)

    def _form_fields(self):
        """Every res.partner field the contact form asks the server for."""
        arch = self._form_arch()
        Partner = self.env['res.partner']
        return sorted({
            name for name in re.findall(r'name="([a-z0-9_]+)"', arch)
            if name in Partner._fields
        })

    def _supplierinfo_queries_on_read(self, partner, form_fields):
        """Return the SQL that hit the supplierinfo table while reading."""
        self.env.flush_all()
        self.env.invalidate_all()

        cr = self.env.cr
        original_execute = cr.execute
        queries = []

        def spy(query, params=None, log_exceptions=True):
            queries.append(str(query))
            return original_execute(query, params, log_exceptions)

        self.patch(cr, 'execute', spy)
        partner.read(form_fields)
        return [q for q in queries if _SI_TABLE_RE.search(q)]

    def test_opening_form_does_not_read_supplierinfo_rows(self):
        """Only the badge aggregate may touch the table, never a row SELECT."""
        form_fields = self._form_fields()
        self.assertIn('baf_direct_price_count', form_fields)

        touching = self._supplierinfo_queries_on_read(self.vendor, form_fields)
        self.assertTrue(
            touching, "the badge count should still issue its aggregate query")
        for query in touching:
            self.assertIn(
                'COUNT(', query.upper(),
                "vendor form pulled supplierinfo rows: %s" % query)

    def test_non_direct_contact_form_issues_no_supplierinfo_query(self):
        """Every contact reads the badge; non-direct ones must not pay for it."""
        form_fields = self._form_fields()
        customer = self.env['res.partner'].create({'name': 'ZZ Customer'})
        matrix_vendor = self.env['res.partner'].create({
            'name': 'ZZ Matrix Vendor',
            'baf_is_vendor': True,
            'baf_purchase_method': 'matrix',
        })
        for partner in (customer, matrix_vendor):
            touching = self._supplierinfo_queries_on_read(partner, form_fields)
            self.assertFalse(
                touching,
                "%s is not a direct vendor but still queried supplierinfo: %s"
                % (partner.name, touching))
            self.assertEqual(partner.baf_direct_price_count, 0)

    def test_switching_method_to_direct_refreshes_the_badge(self):
        """Flipping the method must invalidate the badge, not keep a stale 0."""
        vendor = self.env['res.partner'].create({
            'name': 'ZZ Switching Vendor',
            'baf_is_vendor': True,
            'baf_purchase_method': 'matrix',
        })
        self.env['product.supplierinfo'].create({
            'partner_id': vendor.id,
            'product_tmpl_id': self.templates[0].id,
            'price': 7.0,
        })
        self.assertEqual(vendor.baf_direct_price_count, 0)
        vendor.baf_purchase_method = 'direct'
        self.assertEqual(vendor.baf_direct_price_count, 1)

    def test_count_is_scoped_per_vendor(self):
        self.assertEqual(self.vendor.baf_direct_price_count, 12)
        self.assertEqual(self.other_vendor.baf_direct_price_count, 1)
        self.assertEqual(
            self.env['res.partner'].create(
                {'name': 'ZZ No Prices'}).baf_direct_price_count, 0)

    def test_count_batches_over_multiple_partners(self):
        partners = self.vendor | self.other_vendor
        partners.invalidate_recordset(['baf_direct_price_count'])
        self.assertEqual(
            [p.baf_direct_price_count for p in partners], [12, 1])

    def test_action_returns_scoped_list_view(self):
        action = self.vendor.action_baf_view_direct_prices()
        self.assertEqual(action['res_model'], 'product.supplierinfo')
        self.assertEqual(action['domain'], [('partner_id', '=', self.vendor.id)])
        self.assertEqual(action['context']['default_partner_id'], self.vendor.id)
        list_view = self.env.ref(
            'general_system_custom.view_baf_vendor_supplierinfo_list')
        self.assertEqual(action['views'][0], (list_view.id, 'list'))

        rows = self.env['product.supplierinfo'].with_context(
            action['context']).search(action['domain'])
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows.partner_id, self.vendor)

    def test_row_created_from_action_context_links_vendor(self):
        action = self.vendor.action_baf_view_direct_prices()
        tmpl = self.env['product.template'].create({
            'name': 'ZZ New Part', 'default_code': 'ZZDP_NEW',
            'sku': 'ZZSKUNEW', 'brand': self.brand.id, 'list_price': 100.0,
        })
        Sup = self.env['product.supplierinfo'].with_context(action['context'])
        row = Sup.create(Sup.default_get(['partner_id', 'currency_id', 'delay'])
                         | {'product_tmpl_id': tmpl.id, 'price': 42.0})
        self.assertEqual(row.partner_id, self.vendor)
        self.assertEqual(row.product_tmpl_id, tmpl)
        self.assertEqual(self.vendor.baf_direct_price_count, 13)
        # No regression in the lookup used by the pricing engine.
        self.assertEqual(
            tmpl.baf_get_purchase_price_details(self.vendor)['price'], 42.0)

    def test_editing_price_from_list_persists_and_reprices(self):
        tmpl = self.templates[3]
        row = self.env['product.supplierinfo'].search([
            ('partner_id', '=', self.vendor.id),
            ('product_tmpl_id', '=', tmpl.id),
        ])
        row.write({'price': 12.5})
        tmpl.invalidate_recordset(['seller_ids'])
        details = tmpl.baf_get_purchase_price_details(self.vendor)
        self.assertEqual(details['pricing_method'], 'direct')
        self.assertEqual(details['price'], 12.5)

    def test_sku_and_brand_exposed_on_supplierinfo(self):
        row = self.env['product.supplierinfo'].search(
            [('partner_id', '=', self.vendor.id)], limit=1)
        self.assertEqual(row.baf_sku, row.product_tmpl_id.sku)
        self.assertEqual(row.baf_brand_id, self.brand)
        self.assertEqual(row.baf_product_name, row.product_tmpl_id.name)

    def test_product_name_is_searchable_and_sortable(self):
        Sup = self.env['product.supplierinfo']
        domain = [('partner_id', '=', self.vendor.id)]
        rows = Sup.search(domain + [('baf_product_name', 'ilike', 'ZZ Part 7')])
        self.assertEqual(rows.product_tmpl_id, self.templates[7])
        self.assertEqual(
            Sup.search(domain, order='baf_product_name desc')[0].baf_product_name,
            'ZZ Part 9')

    def test_sku_and_brand_are_searchable(self):
        Sup = self.env['product.supplierinfo']
        rows = Sup.search([
            ('partner_id', '=', self.vendor.id), ('baf_sku', '=', 'ZZSKU005')])
        self.assertEqual(rows.product_tmpl_id, self.templates[5])
        self.assertEqual(
            Sup.search_count([('partner_id', '=', self.vendor.id),
                              ('baf_brand_id', '=', self.brand.id)]), 12)

    def test_sku_and_brand_are_sortable_and_groupable(self):
        # Odoo resolves both through a JOIN, despite the fields not being stored.
        Sup = self.env['product.supplierinfo']
        domain = [('partner_id', '=', self.vendor.id)]
        self.assertEqual(
            Sup.search(domain, order='baf_sku desc')[0].baf_sku, 'ZZSKU011')
        self.assertEqual(
            Sup.search(domain, order='baf_sku asc')[0].baf_sku, 'ZZSKU000')
        self.assertEqual(
            Sup._read_group(domain, ['baf_brand_id'], ['__count']),
            [(self.brand, 12)])

    def test_list_view_columns(self):
        arch = self.env.ref(
            'general_system_custom.view_baf_vendor_supplierinfo_list').arch
        for name in ('baf_sku', 'baf_product_name', 'baf_brand_id', 'price',
                     'product_tmpl_id', 'product_name'):
            self.assertIn('name="%s"' % name, arch)

    def test_search_view_has_sku_and_brand(self):
        arch = self.env['product.supplierinfo'].get_view(
            self.env.ref('product.product_supplierinfo_search_view').id,
            'search')['arch']
        for name in ('baf_sku', 'baf_product_name', 'baf_brand_id', 'price'):
            self.assertIn('name="%s"' % name, arch)
        self.assertIn('name="groupby_brand"', arch)
