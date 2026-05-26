from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    shipping_account_ids = fields.One2many(
        related='company_id.shipping_account_ids', readonly=False,
    )
