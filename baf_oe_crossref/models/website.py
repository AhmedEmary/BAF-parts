from odoo import fields, models


class Website(models.Model):
    _inherit = 'website'

    enable_aftermarket_search = fields.Boolean(
        string="Show Inter Cars Aftermarket Alternatives",
        default=False,
        help="When ON, product pages fetch aftermarket equivalents from "
             "Inter Cars live and offer them on the OEM product page. "
             "Toggle OFF to return to OEM-only browsing.",
    )
