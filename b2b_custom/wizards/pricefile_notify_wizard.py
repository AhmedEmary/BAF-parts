from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PricefileNotifyWizard(models.TransientModel):
    _name = 'pricefile.notify.wizard'
    _description = "Notify B2B customers of a price update"

    brand_ids = fields.Many2many(
        'product.brand',
        string='Brands',
        required=True,
        help="Customers with access to any of these brands will be notified.",
    )
    recipient_count = fields.Integer(
        string='Recipients',
        compute='_compute_recipient_count',
        help="How many customers (deduplicated per company) will receive an email.",
    )

    @api.depends('brand_ids')
    def _compute_recipient_count(self):
        for wizard in self:
            wizard.recipient_count = len(
                wizard.brand_ids._baf_notify_price_update_recipients())

    def action_send(self):
        self.ensure_one()
        if not self.brand_ids:
            raise UserError(_("Select at least one brand."))
        recipients = self.brand_ids._baf_notify_price_update()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Price update notification"),
                'message': _(
                    "Email queued for %(count)d customer(s) across brands: %(brands)s",
                ) % {
                    'count': len(recipients),
                    'brands': ", ".join(b.display_name for b in self.brand_ids),
                },
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
