import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    baf_b2b_state = fields.Selection(
        selection=[
            ('not_b2b', 'Not a B2B applicant'),
            ('pending', 'Pending Review'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
        ],
        string='B2B Status',
        default='not_b2b',
        copy=False,
        tracking=True,
        index=True,
    )
    baf_b2b_applied_at = fields.Datetime(string='Applied On', copy=False, readonly=True)
    baf_b2b_approved_at = fields.Datetime(string='Approved On', copy=False, readonly=True)
    baf_b2b_approved_by_id = fields.Many2one('res.users', string='Approved By', copy=False, readonly=True)
    baf_b2b_rejection_reason = fields.Text(string='Rejection Reason', copy=False)
    baf_b2b_application_note = fields.Text(
        string='Application Note',
        copy=False,
        help="Free-text message submitted by the customer on the application form.",
    )
    baf_trade_license = fields.Binary(
        string='Trade License (Gewerbeschein)',
        copy=False,
        attachment=True,
    )
    baf_trade_license_filename = fields.Char(
        string='Trade License Filename',
        copy=False,
    )

    def _baf_b2b_groups(self):
        portal = self.env.ref('base.group_portal', raise_if_not_found=False)
        b2b = self.env.ref('b2b_custom.group_b2b_customer', raise_if_not_found=False)
        return portal, b2b

    def _baf_b2b_user_has_password(self, user):
        """``res.users.password`` is computed and always reads empty for
        security. To know if a password has actually been set, we have to
        peek at the underlying column directly."""
        if not user:
            return False
        self.env.cr.execute(
            "SELECT password IS NOT NULL AND password != '' FROM res_users WHERE id = %s",
            (user.id,),
        )
        row = self.env.cr.fetchone()
        return bool(row and row[0])

    def _baf_b2b_ensure_portal_user(self, password=None, active=True, add_b2b_group=True):
        """Create or upgrade a portal user for this partner.

        ``password`` — if given, set on the user (only on create, or when the
        existing user has no usable password yet).
        ``active`` — if False, create the user disabled (used during pending
        registration so the account exists but cannot log in until approval).
        ``add_b2b_group`` — when False the user is only put in the portal
        group; the B2B group is added later during approval.
        """
        self.ensure_one()
        if not self.email:
            raise UserError(_("Cannot grant B2B access without an email on the partner."))
        portal_group, b2b_group = self._baf_b2b_groups()
        if not portal_group or not b2b_group:
            raise UserError(_("Portal or B2B group missing — module install incomplete."))

        User = self.env['res.users'].sudo()
        user = self.user_ids[:1]
        if not user:
            group_ids = [portal_group.id]
            if add_b2b_group:
                group_ids.append(b2b_group.id)
            vals = {
                'login': self.email,
                'partner_id': self.id,
                'group_ids': [(6, 0, group_ids)],
                'active': bool(active),
            }
            if password:
                vals['password'] = password
            user = User.with_context(no_reset_password=True).create(vals)
        else:
            write_vals = {
                'group_ids': [(4, portal_group.id)] + (
                    [(4, b2b_group.id)] if add_b2b_group else []
                ),
            }
            if active:
                write_vals['active'] = True
            user.sudo().write(write_vals)
        return user

    def action_baf_b2b_approve(self):
        """Approve the application: ensure a portal user exists and is in the
        B2B group, then notify the applicant.

        Applicants who set their password on /b2b/register already have an
        inactive user — we just activate it and send an "account activated"
        email. Legacy /b2b/apply applicants (no password) still get a signup
        invitation link.
        """
        for partner in self:
            if partner.baf_b2b_state == 'approved':
                continue
            user = partner._baf_b2b_ensure_portal_user(active=True)
            has_password = partner._baf_b2b_user_has_password(user)
            if not has_password:
                # auth_signup hook: generates signup_token / signup_expiration
                partner.sudo().signup_prepare()
            partner.write({
                'baf_b2b_state': 'approved',
                'baf_b2b_approved_at': fields.Datetime.now(),
                'baf_b2b_approved_by_id': self.env.user.id,
                'baf_b2b_rejection_reason': False,
            })
            if has_password:
                template = self.env.ref(
                    'b2b_custom.mail_template_baf_b2b_activated',
                    raise_if_not_found=False,
                )
                log_prefix = _("B2B activation email sent to")
            else:
                template = self.env.ref(
                    'b2b_custom.mail_template_baf_b2b_approved',
                    raise_if_not_found=False,
                )
                log_prefix = _("B2B signup invitation sent to")
            if template:
                template.sudo().send_mail(partner.id, force_send=True)
                partner._baf_b2b_log_invitation(template, prefix=log_prefix)

    def action_baf_b2b_reject(self):
        """Mark application rejected. Revokes B2B group, archives the
        portal user (so a pre-set password can't be used to log in), but
        keeps the contact."""
        for partner in self:
            partner.write({
                'baf_b2b_state': 'rejected',
                'baf_b2b_approved_at': False,
                'baf_b2b_approved_by_id': False,
            })
            _, b2b_group = partner._baf_b2b_groups()
            if partner.user_ids:
                user_vals = {'active': False}
                if b2b_group:
                    user_vals['group_ids'] = [(3, b2b_group.id)]
                partner.user_ids.sudo().write(user_vals)

    def action_baf_b2b_reset_to_pending(self):
        """Push an approved or rejected application back to pending review."""
        for partner in self:
            _, b2b_group = partner._baf_b2b_groups()
            if b2b_group and partner.user_ids:
                partner.user_ids.sudo().write({'group_ids': [(3, b2b_group.id)]})
            partner.write({
                'baf_b2b_state': 'pending',
                'baf_b2b_approved_at': False,
                'baf_b2b_approved_by_id': False,
                'baf_b2b_rejection_reason': False,
            })

    def action_baf_b2b_resend_invitation(self):
        """Re-send the welcome email for an already-approved application.

        If the user has a password (set on /b2b/register), we re-send the
        "account activated" notice; otherwise we re-issue the signup link.
        """
        for partner in self:
            if partner.baf_b2b_state != 'approved':
                raise UserError(_("Only approved applications can be re-sent."))
            user = partner._baf_b2b_ensure_portal_user(active=True)
            has_password = partner._baf_b2b_user_has_password(user)
            if has_password:
                template = self.env.ref(
                    'b2b_custom.mail_template_baf_b2b_activated',
                    raise_if_not_found=False,
                )
                log_prefix = _("B2B activation email re-sent to")
            else:
                partner.sudo().signup_prepare()
                template = self.env.ref(
                    'b2b_custom.mail_template_baf_b2b_approved',
                    raise_if_not_found=False,
                )
                log_prefix = _("B2B signup invitation re-sent to")
            if template:
                template.sudo().send_mail(partner.id, force_send=True)
                partner._baf_b2b_log_invitation(template, prefix=log_prefix)

    def _baf_b2b_log_invitation(self, template, prefix):
        """Post an internal note on the partner chatter with the recipient,
        the activation link (if any, so staff can copy it if mail delivery
        fails), and the full rendered email body for reference."""
        self.ensure_one()
        signup_url = ''
        try:
            signup_url = self.sudo()._get_signup_url() or ''
        except Exception:
            signup_url = ''
        try:
            rendered = template.sudo()._render_field('body_html', [self.id])
        except Exception:
            rendered = {}
        body_html = rendered.get(self.id, '') if isinstance(rendered, dict) else (rendered or '')
        if signup_url:
            note = Markup(
                "<p>%s <strong>%s</strong>.</p>"
                "<p><strong>Activation link:</strong><br/>"
                "<a href=\"%s\" style=\"word-break:break-all;\">%s</a></p>"
                "<hr/>"
                "<p><em>Email preview:</em></p>"
                "%s"
            ) % (prefix, self.email or '', signup_url, signup_url, body_html)
        else:
            note = Markup(
                "<p>%s <strong>%s</strong>.</p>"
                "<hr/>"
                "<p><em>Email preview:</em></p>"
                "%s"
            ) % (prefix, self.email or '', body_html)
        self.message_post(body=note, subtype_xmlid='mail.mt_note')

    @api.model
    def baf_b2b_create_application(self, vals):
        """Public-facing factory used by the /b2b/apply and /b2b/register
        controllers. Always runs sudo() because the request is unauthenticated.

        If ``vals['password']`` is provided, an inactive portal user is
        created so the applicant's password is already in place. The user is
        switched to ``active=True`` and added to the B2B group on approval.
        """
        partner_vals = {
            'name': (vals.get('company_name') or vals.get('contact_name') or '').strip(),
            'email': (vals.get('email') or '').strip(),
            'phone': (vals.get('phone') or '').strip(),
            'street': (vals.get('street') or '').strip(),
            'street2': (vals.get('street2') or '').strip(),
            'city': (vals.get('city') or '').strip(),
            'zip': (vals.get('zip') or '').strip(),
            'vat': (vals.get('vat') or '').strip(),
            'country_id': vals.get('country_id') or False,
            'state_id': vals.get('state_id') or False,
            'company_type': 'company' if vals.get('company_name') else 'person',
            'is_company': bool(vals.get('company_name')),
            'baf_b2b_state': 'pending',
            'baf_b2b_applied_at': fields.Datetime.now(),
            'baf_b2b_application_note': (vals.get('note') or '').strip(),
            'customer_rank': 1,
        }
        brand_ids = vals.get('brand_ids') or []
        if brand_ids:
            partner_vals['visible_brand_ids'] = [(6, 0, list(brand_ids))]
        contact_name = (vals.get('contact_name') or '').strip()
        if contact_name and partner_vals['company_type'] == 'company':
            function = (vals.get('function') or '').strip()
            contact_line = "Kontaktperson: %s" % contact_name
            if function:
                contact_line += " (%s)" % function
            existing_note = partner_vals.get('baf_b2b_application_note') or ''
            partner_vals['baf_b2b_application_note'] = (
                "%s\n%s" % (contact_line, existing_note) if existing_note else contact_line
            )
        partner = self.sudo().create(partner_vals)
        password = vals.get('password')
        if password and partner.email:
            partner._baf_b2b_ensure_portal_user(
                password=password,
                active=False,
                add_b2b_group=False,
            )
            # Odoo res.users.create() forces partner.active = user.active,
            # which would archive the application and hide it from the
            # pending list. Re-activate the partner so staff can review it.
            if not partner.active:
                partner.sudo().write({'active': True})
        return partner
