from odoo import models, fields, api
from odoo.fields import Domain


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    replaced_by_id = fields.Many2one(
        'product.template',
        string='Replaced by',
        help="Select the product that replaces this product",
    )
    unit_of_sales = fields.Integer(
        string='Unit of Sales',
        help="Minimum number of units for a product to be sold",
    )

    default_code = fields.Char(
        compute='_compute_internal_reference',
        store=True,
        readonly=False,
    )

    @api.depends('brand', 'sku')
    def _compute_internal_reference(self):
        for rec in self:
            if rec.sku:
                if rec.brand and rec.brand.name:
                    prefix = rec.brand.name[:3].upper() if len(rec.brand.name) >= 3 else rec.brand.name.upper()
                    rec.default_code = f"{prefix}_{rec.sku}"

    # ── Website search ────────────────────────────────────────────────────────

    def _search_render_results(self, fetch_fields, mapping, icon, limit):
        """Inject sku + brand into the global website search results dict."""
        results_data = super()._search_render_results(fetch_fields, mapping, icon, limit)
        for product, data in zip(self, results_data):
            data['sku'] = product.sku or ''
            data['brand'] = product.brand.name if product.brand else ''
        return results_data

    @api.model
    def _search_fetch(self, search_detail, search, limit, order):
        """
        Strict exact SKU lookup — optimised for large catalogues via B-Tree index.
        """
        base_domain = search_detail.get('base_domain', [])

        if not search:
            results = self.search(base_domain, limit=limit, order=order)
            count = self.search_count(base_domain, limit=50000)
            return results, count

        search_terms = [search, search.upper(), search.lower()]
        exact_domain = Domain.AND([
            base_domain,
            ['|', ('sku', 'in', search_terms), ('default_code', 'in', search_terms)],
        ])
        exact_results = self.search(exact_domain, limit=limit, order=order)
        return exact_results, len(exact_results)

    # ── Pricing helpers ───────────────────────────────────────────────────────

    def _baf_final_price_for_partner(self, product_or_template, partner):
        """
        Central helper for _get_combination_info and _get_sales_prices.
        Delegates entirely to baf_get_sales_price on the product.
        Returns float: the final price the customer should see.
        """
        return product_or_template.baf_get_sales_price(partner=partner)

    # ── Product page (single product) ────────────────────────────────────────

    def _get_combination_info(
        self,
        combination=False,
        product_id=False,
        add_qty=1.0,
        uom_id=False,
        only_template=False,
    ):
        res = super()._get_combination_info(
            combination, product_id, add_qty, uom_id, only_template
        )

        partner = self.env.user.partner_id

        product = (
            self.env['product.product'].browse(res.get('product_id'))
            if res.get('product_id')
            else self
        )

        final_price = self._baf_final_price_for_partner(product, partner)

        res['price'] = final_price
        res['list_price'] = final_price
        res['has_discounted_price'] = False

        return res

    # ── Shop catalogue (product grid) ────────────────────────────────────────

    def _get_sales_prices(self, *args, **kwargs):
        """
        Override batch price fetching for the /shop catalogue grid so every
        product tile shows the correct customer-specific price.
        """
        prices = super()._get_sales_prices(*args, **kwargs)
        partner = self.env.user.partner_id

        for template in self:
            final_price = self._baf_final_price_for_partner(template, partner)
            if template.id in prices:
                prices[template.id]['price_reduce'] = final_price
                prices[template.id]['list_price'] = final_price
                prices[template.id]['base_price'] = final_price

        return prices
