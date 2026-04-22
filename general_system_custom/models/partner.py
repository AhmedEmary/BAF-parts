from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_trusted_vendor = fields.Boolean(
        string="Trusted Vendor", 
        help="If checked, the Customer Name column will be included in the PO Excel export sent to this vendor."
    )
    sales_group_id = fields.Many2one(
        'baf.sales.group',
        string='Sales Pricing Group',
        help=(
            "Controls which pricing method and discount table column "
            "applies to this customer. "
            "Leave empty → customer sees full UPE (MSRP = guest price). "
            "Assign GR1/GR2/GR3/GR4 for BMW/MINI table customers, "
            "or a markup-% group for LR / Mercedes / EU-supplier brands."
        ),
        index=True,
    )
