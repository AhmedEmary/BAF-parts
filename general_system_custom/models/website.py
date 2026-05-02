from odoo import models
from odoo.fields import Domain

class Website(models.Model):
    _inherit = 'website'

    def sale_product_domain(self):
        # 1. Get the standard Odoo domain
        domain = super().sale_product_domain()

        # 2. Get the current user viewing the website
        user = self.env.user
        partner = user.partner_id

        # 3. Base rule: Show products where the brand is Public OR the product has NO brand at all
        brand_domain = ['|', ('brand.is_public', '=', True), ('brand', '=', False)]

        # 4. If the user is logged in and has specific brands assigned
        if not user._is_public() and partner.visible_brand_ids:
            brand_domain = [
                '|', '|',
                ('brand.is_public', '=', True),
                ('brand', '=', False),
                ('brand.id', 'in', partner.visible_brand_ids.ids)
            ]

        # 5. Safely combine the domains using Odoo's expression utility
        return Domain.AND([domain, brand_domain])
