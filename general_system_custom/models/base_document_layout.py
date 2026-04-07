from markupsafe import Markup
from odoo import api, fields, models, _

class BaseDocumentLayout(models.TransientModel):
    _inherit = 'base.document.layout'

    @api.model
    def _default_company_details(self):

        company_details = super()._default_company_details()
        company = self.env.company

        # 2. Append VAT if it exists
        if company.vat:
            company_details += Markup('<br/>Vat N. %s') % company.vat

        # 3. Append Contact Info (Email and/or Phone) on new lines
        contact_parts = []
        if company.email:
            contact_parts.append(company.email)
        if company.phone:
            contact_parts.append(company.phone)

        if contact_parts:
            # Join the email and phone using a line break instead of a space
            contact_str = Markup('<br/>').join(contact_parts)
            # Add <br/> after "Contact information:" so the email drops to the next line
            company_details += Markup('<br/>Contact information:<br/>%s') % contact_str

        # Force update the company record
        company.company_details = company_details

        return company_details

    company_details = fields.Html(default=_default_company_details)
