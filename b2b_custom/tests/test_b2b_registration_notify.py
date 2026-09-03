from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestB2BRegistrationNotify(TransactionCase):
    """A new B2B application on /b2b/apply must email the internal
    recipient(s) — recipients come from the system parameter
    (b2b_custom.registration_notification_email), and the From address
    must never be blank (public user has no email, so it falls back to
    the company's formatted email)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env['res.partner']
        cls.ICP = cls.env['ir.config_parameter'].sudo()
        cls.company = cls.env.company
        cls.company.write({'email': 'company@example.com'})
        cls.notify_param = 'b2b_custom.registration_notification_email'

    def _make_application(self, **overrides):
        vals = {
            'company_name': 'AgenticBrains',
            'contact_name': 'Ahmed Elemary',
            'email': 'ahmed@example.com',
            'phone': '+123456',
            'street': 'Musterstr. 1',
            'zip': '10115',
            'city': 'Berlin',
            'country_id': self.env.ref('base.de').id,
            'vat': 'DE123456789',
            'note': 'Interessiert an BMW-Teilen.',
        }
        vals.update(overrides)
        return self.Partner.baf_b2b_create_application(vals)

    def _sent_notifications(self, partner):
        """Every mail.mail row queued by baf_b2b_create_application for
        this partner. `mass_mailing_res_id` isn't set on these, so use
        the direct res_model/res_id link on mail.mail."""
        return self.env['mail.mail'].sudo().search([
            ('model', '=', 'res.partner'),
            ('res_id', '=', partner.id),
        ], order='id desc')

    def test_creates_pending_partner(self):
        # Baseline: the factory should always produce a pending partner.
        partner = self._make_application()
        self.assertEqual(partner.baf_b2b_state, 'pending')
        self.assertTrue(partner.baf_b2b_applied_at)

    def test_notification_uses_system_parameter_recipients(self):
        self.ICP.set_param(
            self.notify_param, 'sales@example.com, admin@example.com')
        partner = self._make_application()
        mails = self._sent_notifications(partner)
        self.assertTrue(mails, "New-application notification was not queued")
        # Most recent mail is the new-application notification. The
        # approve/reject templates fire only on approval — not here.
        mail = mails[0]
        self.assertEqual(mail.email_to, 'sales@example.com,admin@example.com')

    def test_notification_falls_back_to_company_email(self):
        self.ICP.set_param(self.notify_param, '')  # clear
        partner = self._make_application()
        mails = self._sent_notifications(partner)
        self.assertTrue(mails, "New-application notification was not queued")
        self.assertEqual(mails[0].email_to, 'company@example.com')

    def test_notification_from_is_never_blank(self):
        """The public-user path historically left From empty. The template
        + Python override must fall back to the company's formatted email."""
        self.ICP.set_param(self.notify_param, 'sales@example.com')
        partner = self._make_application()
        mails = self._sent_notifications(partner)
        self.assertTrue(mails)
        email_from = (mails[0].email_from or '').strip()
        self.assertTrue(
            email_from,
            "email_from must not be blank — public user has no email so "
            "the template/override must fall back to the company address.")
        self.assertIn('company@example.com', email_from)

    def test_notification_recipients_deduplicate_whitespace(self):
        """Whitespace / empty entries around commas must be tolerated."""
        self.ICP.set_param(
            self.notify_param,
            ' one@example.com , , two@example.com ,',
        )
        partner = self._make_application()
        mails = self._sent_notifications(partner)
        self.assertTrue(mails)
        self.assertEqual(mails[0].email_to, 'one@example.com,two@example.com')

    def test_missing_template_silently_skipped(self):
        """If the template is missing (e.g. partial upgrade), the form
        must NOT 500 — the partner is still created and no mail queued."""
        # Break the reference by unlinking the ir.model.data pointer.
        imd = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'b2b_custom'),
            ('name', '=', 'mail_template_baf_b2b_new_application'),
        ], limit=1)
        template_id = imd.res_id if imd else 0
        if imd:
            imd.unlink()
        if template_id:
            self.env['mail.template'].sudo().browse(template_id).unlink()
        # Still creates the partner — no exception propagated.
        partner = self._make_application(email='no-template@example.com')
        self.assertEqual(partner.baf_b2b_state, 'pending')

    def test_no_recipient_no_mail_queued(self):
        """No system parameter AND no company email = warn + skip.
        Partner must still be created."""
        self.ICP.set_param(self.notify_param, '')
        self.company.write({'email': False})
        partner = self._make_application(email='no-recipient@example.com')
        self.assertEqual(partner.baf_b2b_state, 'pending')
        mails = self._sent_notifications(partner)
        self.assertFalse(
            mails,
            "No recipient configured, no notification mail should exist.")

    def test_res_config_settings_field_writes_param(self):
        """The Settings UI field is the friendly wrapper over the system
        parameter; writing it must update the parameter value."""
        Settings = self.env['res.config.settings']
        Settings.create({
            'baf_b2b_notify_emails': 'ops@example.com, cs@example.com',
        }).execute()
        self.assertEqual(
            self.ICP.get_param(self.notify_param),
            'ops@example.com, cs@example.com',
        )
