import json

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductMassUpdate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.MassUpdate = cls.env['product.mass.update']
        cls.Product = cls.env['product.template']

        cls.brand_a = cls.env['product.brand'].create({'name': 'Brand A'})
        cls.brand_b = cls.env['product.brand'].create({'name': 'Brand B'})

        cls.category_old = cls.env['product.category'].create({'name': 'Old Cat'})
        cls.category_new = cls.env['product.category'].create({'name': 'New Cat'})

        cls.tax_sale = cls.env['account.tax'].create({
            'name': 'Sale 22%', 'amount': 22.0, 'type_tax_use': 'sale',
        })
        cls.tax_purchase = cls.env['account.tax'].create({
            'name': 'Purchase 22%', 'amount': 22.0, 'type_tax_use': 'purchase',
        })

        cls.account_income = cls.env['account.account'].search(
            [('account_type', '=', 'income'), ('company_ids', 'in', cls.env.company.id)], limit=1)
        cls.account_expense = cls.env['account.account'].search(
            [('account_type', '=', 'expense'), ('company_ids', 'in', cls.env.company.id)], limit=1)

        # 6 products: 4 of brand A, 2 of brand B, all in old category
        cls.products_a = cls.Product.create([
            {'name': f'A-{i}', 'brand': cls.brand_a.id, 'categ_id': cls.category_old.id,
             'list_price': 100.0 + i, 'type': 'consu'}
            for i in range(4)
        ])
        cls.products_b = cls.Product.create([
            {'name': f'B-{i}', 'brand': cls.brand_b.id, 'categ_id': cls.category_old.id,
             'list_price': 50.0 + i, 'type': 'consu'}
            for i in range(2)
        ])
        cls.all_products = cls.products_a + cls.products_b

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _new_job(self, **vals):
        return self.MassUpdate.create({'name': 'Test Job', **vals})

    def _drain(self, job, max_iter=20):
        """Process the job batch by batch until it finishes. We call
        ``_process_one_batch`` directly because the cron entry point commits,
        which is forbidden inside a TransactionCase."""
        for _ in range(max_iter):
            if job.state in ('done', 'failed', 'cancelled'):
                return
            job._process_one_batch()
            job.invalidate_recordset()
        self.fail("Job did not finish within %d batches (state=%s)" % (max_iter, job.state))

    def _stage_job_pending(self, job):
        """Stage the job in 'pending' state so ``_process_one_batch`` can be
        driven step by step. We bypass ``action_launch`` here because it drains
        the whole job synchronously, which collides with tests that need to
        observe partial-progress / resumption / cancel-mid-run behaviour."""
        job.write({
            'state': 'pending',
            'vals_json': json.dumps(job._collect_vals()),
            'domain_json': json.dumps(job._build_domain()),
            'processed_count': 0,
            'last_processed_id': 0,
        })

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_launch_without_any_field_raises(self):
        job = self._new_job(brand_ids=[Command.set(self.brand_a.ids)])
        with self.assertRaises(ValidationError):
            job.action_launch()

    def test_invalid_extra_domain_raises(self):
        job = self._new_job(
            apply_categ_id=True, categ_id=self.category_new.id,
            extra_domain="not a list at all",
        )
        with self.assertRaises(ValidationError):
            job.action_launch()

    def test_extra_domain_must_be_list(self):
        job = self._new_job(
            apply_categ_id=True, categ_id=self.category_new.id,
            extra_domain="{'a': 1}",
        )
        with self.assertRaises(ValidationError):
            job.action_launch()

    def test_custom_field_requires_field_id(self):
        job = self._new_job(
            apply_custom_field=True,
            custom_value_char='something',
        )
        with self.assertRaises(ValidationError):
            job.action_launch()

    def test_custom_field_duplicate_with_main_field_raises(self):
        categ_field = self.env['ir.model.fields']._get('product.template', 'categ_id')
        job = self._new_job(
            apply_categ_id=True, categ_id=self.category_new.id,
            apply_custom_field=True,
            custom_field_id=categ_field.id,
            custom_value_reference=f'product.category,{self.category_old.id}',
        )
        with self.assertRaises(ValidationError):
            job.action_launch()

    def test_launch_twice_raises(self):
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_categ_id=True, categ_id=self.category_new.id,
        )
        job.action_launch()
        with self.assertRaises(UserError):
            job.action_launch()

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------
    def test_dry_run_counts_and_does_not_write(self):
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_categ_id=True, categ_id=self.category_new.id,
        )
        job.action_dry_run()
        self.assertTrue(job.dry_run_done)
        self.assertEqual(job.dry_run_count, 4)
        self.assertEqual(len(job.dry_run_sample_ids), 4)
        # Nothing was written.
        self.assertTrue(all(p.categ_id == self.category_old for p in self.products_a))
        # Job stayed in draft.
        self.assertEqual(job.state, 'draft')

    def test_dry_run_invalidated_when_input_changes(self):
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_categ_id=True, categ_id=self.category_new.id,
        )
        job.action_dry_run()
        self.assertTrue(job.dry_run_done)
        # Onchange runs in Form view; simulate by triggering and writing.
        job.brand_ids = [Command.set(self.brand_b.ids)]
        job._onchange_invalidate_dry_run()
        self.assertFalse(job.dry_run_done)
        self.assertFalse(job.dry_run_count)

    # ------------------------------------------------------------------
    # Brand + domain filtering, single batch
    # ------------------------------------------------------------------
    def test_filter_by_brand_only_updates_matching(self):
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_categ_id=True, categ_id=self.category_new.id,
        )
        job.action_launch()
        self._drain(job)

        self.assertEqual(job.state, 'done')
        self.assertEqual(job.processed_count, 4)
        self.assertEqual(job.total_count, 4)

        for p in self.products_a:
            self.assertEqual(p.categ_id, self.category_new)
        for p in self.products_b:
            self.assertEqual(p.categ_id, self.category_old)

    def test_filter_by_extra_domain(self):
        # Combine brand filter with extra_domain to keep the search scoped to
        # our test products (the DB may already contain unrelated products).
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_categ_id=True, categ_id=self.category_new.id,
            extra_domain="[('list_price', '>=', 102)]",
        )
        job.action_launch()
        self._drain(job)

        # A-2 (102) and A-3 (103) match; A-0 (100) and A-1 (101) do not.
        updated = self.products_a.filtered(lambda p: p.categ_id == self.category_new)
        self.assertEqual(len(updated), 2)
        self.assertEqual(set(updated.mapped('list_price')), {102.0, 103.0})
        for p in self.products_b:
            self.assertEqual(p.categ_id, self.category_old)

    def test_filter_by_specific_products_overrides_brand(self):
        # product_tmpl_ids takes precedence over brand/domain
        targets = self.products_b
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],   # ignored
            extra_domain="[('list_price', '>', 9999)]",  # ignored
            product_tmpl_ids=[Command.set(targets.ids)],
            apply_categ_id=True, categ_id=self.category_new.id,
        )
        job.action_launch()
        self._drain(job)
        for p in targets:
            self.assertEqual(p.categ_id, self.category_new)
        for p in self.products_a:
            self.assertEqual(p.categ_id, self.category_old)

    # ------------------------------------------------------------------
    # Field types
    # ------------------------------------------------------------------
    def test_writes_taxes_m2m(self):
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_taxes_id=True, taxes_id=[Command.set(self.tax_sale.ids)],
            apply_supplier_taxes_id=True, supplier_taxes_id=[Command.set(self.tax_purchase.ids)],
        )
        job.action_launch()
        self._drain(job)
        for p in self.products_a:
            self.assertIn(self.tax_sale, p.taxes_id)
            self.assertIn(self.tax_purchase, p.supplier_taxes_id)

    def test_writes_company_dependent_income_account(self):
        if not self.account_income:
            self.skipTest("No income account in test company")
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_property_account_income_id=True,
            property_account_income_id=self.account_income.id,
        )
        job.action_launch()
        self._drain(job)
        for p in self.products_a:
            self.assertEqual(
                p.with_company(self.env.company).property_account_income_id,
                self.account_income,
            )

    def test_writes_boolean_field(self):
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_is_published=True, is_published=True,
        )
        job.action_launch()
        self._drain(job)
        for p in self.products_a:
            self.assertTrue(p.is_published)

    def test_writes_is_storable_boolean(self):
        for p in self.products_a:
            p.is_storable = False
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_is_storable=True, is_storable=True,
        )
        job.action_launch()
        self._drain(job)
        for p in self.products_a:
            self.assertTrue(p.is_storable)

    # ------------------------------------------------------------------
    # Custom field
    # ------------------------------------------------------------------
    def test_custom_field_many2one_via_reference(self):
        categ_field = self.env['ir.model.fields']._get('product.template', 'categ_id')
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_custom_field=True,
            custom_field_id=categ_field.id,
            custom_value_reference=f'product.category,{self.category_new.id}',
        )
        job.action_launch()
        self._drain(job)
        for p in self.products_a:
            self.assertEqual(p.categ_id, self.category_new)

    def test_custom_field_reference_wrong_model_raises(self):
        categ_field = self.env['ir.model.fields']._get('product.template', 'categ_id')
        # Pick a record on the wrong model (a product instead of a category)
        job = self._new_job(
            apply_custom_field=True,
            custom_field_id=categ_field.id,
            custom_value_reference=f'product.template,{self.products_a[0].id}',
        )
        with self.assertRaises(ValidationError):
            job.action_launch()

    def test_custom_field_invalid_selection_key_raises(self):
        # `tracking` is a Selection field on product.template, so we can use it
        # through the custom-field selector to verify selection-key validation.
        tracking_field = self.env['ir.model.fields']._get('product.template', 'tracking')
        job = self._new_job(
            apply_custom_field=True,
            custom_field_id=tracking_field.id,
            custom_value_selection_key='not_a_real_key',
        )
        with self.assertRaises(ValidationError):
            job.action_launch()

    def test_custom_field_char_value(self):
        # Set a plain char field (the product name) on every brand-A product.
        name_field = self.env['ir.model.fields']._get('product.template', 'name')
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_custom_field=True,
            custom_field_id=name_field.id,
            custom_value_char='Renamed Part',
        )
        job.action_launch()
        self._drain(job)
        for p in self.products_a:
            self.assertEqual(p.name, 'Renamed Part')

    # ------------------------------------------------------------------
    # Worker / cron behaviour
    # ------------------------------------------------------------------
    def test_resumes_across_multiple_batches(self):
        # Force tiny batch size so 4 products take >1 batch
        from odoo.addons.b2b_custom.models import product_mass_update
        original = product_mass_update.BATCH_SIZE
        product_mass_update.BATCH_SIZE = 2
        try:
            job = self._new_job(
                brand_ids=[Command.set(self.brand_a.ids)],
                apply_categ_id=True, categ_id=self.category_new.id,
            )
            self._stage_job_pending(job)

            # First call: pending->processing, total set, AND first batch written.
            more = job._process_one_batch()
            self.assertTrue(more)
            self.assertEqual(job.state, 'processing')
            self.assertEqual(job.total_count, 4)
            self.assertEqual(job.processed_count, 2)

            # Second call: writes batch 2 (last 2 products).
            more = job._process_one_batch()
            self.assertTrue(more)
            self.assertEqual(job.processed_count, 4)
            self.assertEqual(job.state, 'processing')

            # Third call: nothing left to process -> done.
            more = job._process_one_batch()
            self.assertFalse(more)
            self.assertEqual(job.state, 'done')

            for p in self.products_a:
                self.assertEqual(p.categ_id, self.category_new)
        finally:
            product_mass_update.BATCH_SIZE = original

    def test_empty_match_finishes_immediately(self):
        job = self._new_job(
            extra_domain="[('name', '=', '__no_such_product__')]",
            apply_categ_id=True, categ_id=self.category_new.id,
        )
        job.action_launch()
        self._drain(job)
        self.assertEqual(job.state, 'done')
        self.assertEqual(job.total_count, 0)
        self.assertEqual(job.processed_count, 0)

    def test_cancel_stops_further_processing(self):
        from odoo.addons.b2b_custom.models import product_mass_update
        original = product_mass_update.BATCH_SIZE
        product_mass_update.BATCH_SIZE = 2
        try:
            job = self._new_job(
                brand_ids=[Command.set(self.brand_a.ids)],
                apply_categ_id=True, categ_id=self.category_new.id,
            )
            self._stage_job_pending(job)
            job._process_one_batch()  # init + batch 1 (2 products)
            self.assertEqual(job.processed_count, 2)

            job.action_cancel()
            self.assertEqual(job.state, 'cancelled')

            # The cron queries by state, so a cancelled job is never picked up.
            updated = self.products_a.filtered(lambda p: p.categ_id == self.category_new)
            self.assertEqual(len(updated), 2)
            self.assertEqual(
                self.MassUpdate.search_count([('state', 'in', ('pending', 'processing'))]),
                0,
            )
        finally:
            product_mass_update.BATCH_SIZE = original

    def test_launch_freezes_vals_and_domain(self):
        job = self._new_job(
            brand_ids=[Command.set(self.brand_a.ids)],
            apply_categ_id=True, categ_id=self.category_new.id,
        )
        job.action_launch()
        self.assertTrue(job.vals_json)
        self.assertTrue(job.domain_json)
        vals = json.loads(job.vals_json)
        self.assertEqual(vals, {'categ_id': self.category_new.id})
        domain = json.loads(job.domain_json)
        self.assertEqual(domain, [['brand', 'in', self.brand_a.ids]])

    def test_server_action_helper_prefills_products(self):
        """The product-list server action opens a job pre-targeted on the selection."""
        action = self.products_b.action_open_mass_update_wizard()
        job = self.MassUpdate.browse(action['res_id'])
        self.assertEqual(job.product_tmpl_ids, self.products_b)
