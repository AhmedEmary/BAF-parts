from odoo import api, models


class MailComposeMessage(models.TransientModel):
    _inherit = 'mail.compose.message'

    @api.depends('composition_mode', 'email_from', 'model',
                 'res_domain', 'res_ids', 'template_id')
    def _compute_authorship(self):
        """PO mails go out of the vendor's "PO Sender Email". The base compute
        lets the template win over default_email_from, and falls back to the
        current user when the template has no sender (the standard purchase
        templates), so the vendor's mailbox is applied on top."""
        super()._compute_authorship()
        email_from = self.env.context.get('baf_po_email_from')
        if not email_from:
            return
        for composer in self.filtered(lambda c: c.model == 'purchase.order'):
            composer.email_from = email_from
