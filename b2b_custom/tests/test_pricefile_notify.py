from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPricefileNotify(TransactionCase):
    """Task #50 — customers with access to a brand get a "new prices"
    email when staff triggers the notification for that brand."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Brand = cls.env['product.brand']
        Partner = cls.env['res.partner']
        cls.brand_bmw = Brand.create({'name': 'PN BMW'})
        cls.brand_jlr = Brand.create({'name': 'PN JLR'})
        cls.brand_public = Brand.create({'name': 'PN Public', 'is_public': True})

        # Company with BMW visibility + a child contact that inherits from it.
        cls.company_a = Partner.create({
            'name': 'PN Company A',
            'is_company': True,
            'email': 'a@example.com',
            'baf_b2b_state': 'approved',
            'visible_brand_ids': [(6, 0, [cls.brand_bmw.id])],
        })
        cls.contact_a = Partner.create({
            'name': 'PN Contact A',
            'email': 'contact-a@example.com',
            'parent_id': cls.company_a.id,
            'baf_b2b_state': 'approved',
        })
        # Company with JLR only.
        cls.company_b = Partner.create({
            'name': 'PN Company B',
            'is_company': True,
            'email': 'b@example.com',
            'baf_b2b_state': 'approved',
            'visible_brand_ids': [(6, 0, [cls.brand_jlr.id])],
        })
        # An approved B2B customer with no brands, useful for the is_public
        # check (public brands must still reach them).
        cls.company_c = Partner.create({
            'name': 'PN Company C',
            'is_company': True,
            'email': 'c@example.com',
            'baf_b2b_state': 'approved',
        })
        # A pending applicant that must NOT receive anything.
        cls.company_pending = Partner.create({
            'name': 'PN Company Pending',
            'is_company': True,
            'email': 'pending@example.com',
            'baf_b2b_state': 'pending',
            'visible_brand_ids': [(6, 0, [cls.brand_bmw.id])],
        })
        # Approved but no email -> silently skipped (nothing to send to).
        cls.company_noemail = Partner.create({
            'name': 'PN Company No Email',
            'is_company': True,
            'baf_b2b_state': 'approved',
            'visible_brand_ids': [(6, 0, [cls.brand_bmw.id])],
        })

    def _test_partner_ids(self):
        return {
            self.company_a.id, self.contact_a.id, self.company_b.id,
            self.company_c.id, self.company_pending.id, self.company_noemail.id,
        }

    def test_recipients_include_visible_brand_holders(self):
        recipients = self.brand_bmw._baf_notify_price_update_recipients()
        recipient_ids = set(recipients.ids) & self._test_partner_ids()
        self.assertIn(self.company_a.id, recipient_ids)

    def test_recipients_dedup_to_commercial_partner(self):
        # company_a and contact_a both have baf_b2b_state='approved' and share
        # a commercial partner (company_a). The exporter must not queue two
        # separate emails.
        recipients = self.brand_bmw._baf_notify_price_update_recipients()
        recipient_ids = set(recipients.ids) & self._test_partner_ids()
        self.assertIn(self.company_a.id, recipient_ids)
        self.assertNotIn(self.contact_a.id, recipient_ids)

    def test_recipients_exclude_unrelated_brand_holders(self):
        recipients = self.brand_bmw._baf_notify_price_update_recipients()
        recipient_ids = set(recipients.ids) & self._test_partner_ids()
        self.assertNotIn(self.company_b.id, recipient_ids)

    def test_recipients_exclude_pending_and_missing_email(self):
        recipients = self.brand_bmw._baf_notify_price_update_recipients()
        recipient_ids = set(recipients.ids) & self._test_partner_ids()
        self.assertNotIn(self.company_pending.id, recipient_ids)
        self.assertNotIn(self.company_noemail.id, recipient_ids)

    def test_public_brand_reaches_every_approved_b2b_customer(self):
        recipients = self.brand_public._baf_notify_price_update_recipients()
        recipient_ids = set(recipients.ids) & self._test_partner_ids()
        self.assertEqual(
            recipient_ids,
            {self.company_a.id, self.company_b.id, self.company_c.id},
        )

    def test_send_queues_one_mail_per_recipient(self):
        # `send_mail(..., force_send=False)` creates one `mail.mail`
        # per recipient, ready to be dispatched by the mail queue cron.
        # Task #50 requires that each affected customer gets an email once —
        # this test pins that count against the recipient set.
        Mail = self.env['mail.mail']
        before = Mail.search_count([])
        recipients = self.brand_bmw._baf_notify_price_update()
        after = Mail.search_count([])
        self.assertGreater(len(recipients), 0)
        self.assertEqual(after - before, len(recipients))
        # Every generated mail was addressed to a recipient's email.
        recipient_emails = {p.email for p in recipients}
        recent = Mail.search([], order='id desc', limit=len(recipients))
        for mail in recent:
            self.assertIn(mail.email_to, recipient_emails)

    def test_action_opens_wizard_with_brand_preselected(self):
        action = self.brand_bmw.action_notify_price_update()
        self.assertEqual(action['res_model'], 'pricefile.notify.wizard')
        preselected = action['context']['default_brand_ids']
        self.assertEqual(preselected, [(6, 0, [self.brand_bmw.id])])

    def test_wizard_recipient_count_matches_helper(self):
        wizard = self.env['pricefile.notify.wizard'].create({
            'brand_ids': [(6, 0, [self.brand_bmw.id])],
        })
        expected = len(self.brand_bmw._baf_notify_price_update_recipients())
        self.assertEqual(wizard.recipient_count, expected)

    def test_wizard_action_send_queues_mails(self):
        wizard = self.env['pricefile.notify.wizard'].create({
            'brand_ids': [(6, 0, [self.brand_bmw.id])],
        })
        Mail = self.env['mail.mail']
        before = Mail.search_count([])
        wizard.action_send()
        after = Mail.search_count([])
        self.assertGreater(after, before)
