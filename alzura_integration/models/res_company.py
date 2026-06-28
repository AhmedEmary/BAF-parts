from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    alzura_token = fields.Char(
        string="Alzura Auth Token",
        groups="base.group_system",
        readonly=True,
    )
    alzura_token_expiry = fields.Datetime(
        string="Alzura Token Expiry",
        groups="base.group_system",
        readonly=True,
    )
    alzura_country = fields.Char(
        string="Alzura Country",
        default="de",
        help="ISO 3166-1 alpha-2 lowercase country code sent in the request header.",
    )

    def _alzura_request_headers(self):
        """Authenticated headers for Alzura endpoints that require X-AUTH-TOKEN."""
        self.ensure_one()
        return {
            "X-AUTH-TOKEN": self.sudo().alzura_token or "",
            "Accept": "application/vnd.saitowag.api+json;version=1.0",
            "Content-Type": "application/json",
            "country": (self.alzura_country or "de").lower(),
        }
