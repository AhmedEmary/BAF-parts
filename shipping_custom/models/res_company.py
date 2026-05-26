from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'

    shipping_account_ids = fields.One2many(
        'shipping.provider.account', 'company_id', string="Shipping Accounts",
    )
