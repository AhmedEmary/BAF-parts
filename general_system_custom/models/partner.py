from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_trusted_vendor = fields.Boolean(
        string="Trusted Vendor", 
        help="If checked, the Customer Name column will be included in the PO Excel export sent to this vendor."
    )