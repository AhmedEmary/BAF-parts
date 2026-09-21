import logging

from odoo import models

_logger = logging.getLogger(__name__)


class BafIntegrationMixin(models.AbstractModel):
    """Helpers shared by the inbound/outbound order integrations.

    Reached as ``self.env['baf.integration.mixin']``; deliberately NOT mixed
    into ``sale.order``. Every method here takes what it needs as arguments and
    touches only ``self.env``, so widening ``sale.order``'s inheritance chain
    would buy nothing and put every integration change on a model the whole
    system inherits.

    Extracted from ``alzura_integration`` so ``lexcom_integration`` can reuse
    them; behaviour is unchanged from the Alzura originals.
    """

    _name = 'baf.integration.mixin'
    _description = 'BAF Integration Helpers'

    def _baf_tax_ids_for_rate(self, company, rate):
        """Resolve a VAT rate (0.19 = 19 %) to a sale tax of ``company``.

        Integrations state the rate they charged the buyer, so taking the tax
        from the payload keeps amount_total on the stated total instead of
        depending on whatever the matched product happens to carry. Returns an
        empty recordset when no matching tax exists, which leaves Odoo's
        product default in place; the rate is never created here, since that is
        accounting configuration.

        Callers that need an ORM command build it themselves, e.g.
        ``[(6, 0, taxes.ids)] if taxes else False``.
        """
        Tax = self.env['account.tax']
        if rate is None:
            return Tax
        percent = round(float(rate) * 100.0, 4)
        candidates = Tax.sudo().search([
            ('company_id', '=', company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount_type', '=', 'percent'),
            ('amount', '=', percent),
            ('price_include', '=', False),
        ])
        if not candidates:
            _logger.warning(
                "BAF integration: company %s has no %s%% sale tax; falling back "
                "to the product default, so the order total may not match the "
                "amount stated by the source system.",
                company.name,
                percent,
            )
            return Tax

        # Databases keep several taxes at the same rate (e.g. "19%" next to
        # "19% EU D"), and picking by search order would post to whichever
        # happens to sort first. The company's configured sale tax is the
        # deliberate choice, so it wins whenever it matches the rate.
        default = company.sudo().account_sale_tax_id
        tax = (candidates & default) or candidates[:1]
        if len(candidates) > 1:
            _logger.info(
                "BAF integration: %s taxes match %s%% for company %s; using %s.",
                len(candidates),
                percent,
                company.name,
                tax.display_name,
            )
        return tax

    def _baf_get_or_create_service_product(self, default_code, name):
        """Get-or-create the service product carrying ``default_code``."""
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', default_code)], limit=1)
        if not product:
            product = Product.create({
                'name': name,
                'default_code': default_code,
                'type': 'service',
                'purchase_ok': False,
                'list_price': 0.0,
            })
        return product

    def _baf_country_by_code(self, code):
        """Match a res.country by ISO alpha-2 code; empty recordset otherwise."""
        Country = self.env['res.country']
        if not code:
            return Country
        return Country.search([('code', '=ilike', code)], limit=1)
