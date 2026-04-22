from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_trusted_vendor = fields.Boolean(
        string="Trusted Vendor", 
        help="If checked, the Customer Name column will be included in the PO Excel export sent to this vendor."
    )
    sales_group_ids = fields.Many2many(
        'baf.sales.group',
        'baf_sales_group_partner_rel',
        'partner_id',
        'group_id',
        string='Sales Pricing Groups',
        help=(
            "Controls which pricing method and discount table columns "
            "apply to this customer. "
            "Leave empty → customer sees full UPE (MSRP = guest price). "
            "Assign GR1/GR2/GR3/GR4 for BMW/MINI table customers, "
            "or a markup-% group for LR / Mercedes / EU-supplier brands. "
            "If multiple groups are set, the first one is used for pricing."
        ),
    )
