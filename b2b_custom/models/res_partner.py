from odoo import _, api, fields, models
from odoo.exceptions import UserError


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

    def _baf_b2b_groups(self):
        portal = self.env.ref('base.group_portal', raise_if_not_found=False)
        b2b = self.env.ref('b2b_custom.group_b2b_customer', raise_if_not_found=False)
        return portal, b2b

    def _baf_b2b_ensure_portal_user(self):
        """Create or upgrade a portal user for this partner and put them in
        the B2B group. Idempotent: safe to call multiple times."""
        self.ensure_one()
        if not self.email:
            raise UserError(_("Cannot grant B2B access without an email on the partner."))
        portal_group, b2b_group = self._baf_b2b_groups()
        if not portal_group or not b2b_group:
            raise UserError(_("Portal or B2B group missing — module install incomplete."))

        User = self.env['res.users'].sudo()
        user = self.user_ids[:1]
        if not user:
            user = User.with_context(no_reset_password=True).create({
                'login': self.email,
                'partner_id': self.id,
                'groups_id': [(6, 0, [portal_group.id, b2b_group.id])],
            })
        else:
            user.sudo().write({
                'groups_id': [(4, portal_group.id), (4, b2b_group.id)],
            })
        return user

    def action_baf_b2b_approve(self):
        """Approve the application: create portal user, add to B2B group,
        generate a signup token, send the invitation email."""
        for partner in self:
            if partner.baf_b2b_state == 'approved':
                continue
            partner._baf_b2b_ensure_portal_user()
            # auth_signup hook: generates signup_token / signup_expiration
            partner.sudo().signup_prepare()
            partner.write({
                'baf_b2b_state': 'approved',
                'baf_b2b_approved_at': fields.Datetime.now(),
                'baf_b2b_approved_by_id': self.env.user.id,
                'baf_b2b_rejection_reason': False,
            })
            template = self.env.ref(
                'b2b_custom.mail_template_baf_b2b_approved',
                raise_if_not_found=False,
            )
            if template:
                template.sudo().send_mail(partner.id, force_send=True)

    def action_baf_b2b_reject(self):
        """Mark application rejected. Revokes B2B group (and optionally
        deactivates the portal user) but keeps the contact for the record."""
        for partner in self:
            partner.write({
                'baf_b2b_state': 'rejected',
                'baf_b2b_approved_at': False,
                'baf_b2b_approved_by_id': False,
            })
            _, b2b_group = partner._baf_b2b_groups()
            if b2b_group and partner.user_ids:
                partner.user_ids.sudo().write({'groups_id': [(3, b2b_group.id)]})

    def action_baf_b2b_reset_to_pending(self):
        """Push an approved or rejected application back to pending review."""
        for partner in self:
            _, b2b_group = partner._baf_b2b_groups()
            if b2b_group and partner.user_ids:
                partner.user_ids.sudo().write({'groups_id': [(3, b2b_group.id)]})
            partner.write({
                'baf_b2b_state': 'pending',
                'baf_b2b_approved_at': False,
                'baf_b2b_approved_by_id': False,
                'baf_b2b_rejection_reason': False,
            })

    def action_baf_b2b_resend_invitation(self):
        """Re-send the signup email for an already-approved application."""
        for partner in self:
            if partner.baf_b2b_state != 'approved':
                raise UserError(_("Only approved applications can be re-sent."))
            partner._baf_b2b_ensure_portal_user()
            partner.sudo().signup_prepare()
            template = self.env.ref(
                'b2b_custom.mail_template_baf_b2b_approved',
                raise_if_not_found=False,
            )
            if template:
                template.sudo().send_mail(partner.id, force_send=True)

    @api.model
    def baf_b2b_create_application(self, vals):
        """Public-facing factory used by the /b2b/apply controller.
        Always runs sudo() because the request is unauthenticated."""
        partner_vals = {
            'name': (vals.get('company_name') or vals.get('contact_name') or '').strip(),
            'email': (vals.get('email') or '').strip(),
            'phone': (vals.get('phone') or '').strip(),
            'street': (vals.get('street') or '').strip(),
            'city': (vals.get('city') or '').strip(),
            'zip': (vals.get('zip') or '').strip(),
            'vat': (vals.get('vat') or '').strip(),
            'company_type': 'company' if vals.get('company_name') else 'person',
            'is_company': bool(vals.get('company_name')),
            'baf_b2b_state': 'pending',
            'baf_b2b_applied_at': fields.Datetime.now(),
            'baf_b2b_application_note': (vals.get('note') or '').strip(),
            'customer_rank': 1,
        }
        contact_name = (vals.get('contact_name') or '').strip()
        partner = self.sudo().create(partner_vals)
        if contact_name and partner_vals['company_type'] == 'company':
            self.sudo().create({
                'name': contact_name,
                'parent_id': partner.id,
                'email': partner_vals['email'],
                'phone': partner_vals['phone'],
                'type': 'contact',
                'function': (vals.get('function') or '').strip(),
            })
        return partner