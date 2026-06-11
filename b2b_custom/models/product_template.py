from odoo import models, fields, api
from odoo.fields import Domain
from odoo.http import request
from odoo.tools.sql import column_exists


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

    # ── Orderability ──────────────────────────────────────────────────────────

    NLA_SKU = 'NLA'

    def _baf_is_nla(self):
        """Return True when this template — or any product reached by walking
        the `replaced_by_id` chain — has SKU 'NLA'.

        A part is NLA both when:
          * its own SKU is 'NLA'; or
          * its replacement (or the replacement's replacement, …) is NLA.
        """
        self.ensure_one()
        seen = set()
        current = self
        while current and current.id not in seen:
            seen.add(current.id)
            sku = (current.sku or '').strip().upper()
            if sku == self.NLA_SKU:
                return True
            current = current.replaced_by_id
        return False

    def _baf_is_order_blocked(self):
        """Return True when this template must not be ordered.

        Two cases are blocked:
          * the part is NLA (own SKU or any successor in the chain); or
          * the part has been superseded — `replaced_by_id` is set, the
            customer must order the replacement instead.
        """
        self.ensure_one()
        if self._baf_is_nla():
            return True
        return bool(self.replaced_by_id)

    def _is_add_to_cart_possible(self, parent_combination=None):
        if self._baf_is_order_blocked():
            return False
        return super()._is_add_to_cart_possible(parent_combination=parent_combination)

    @api.model
    def _baf_enable_oos_orders(self):
        """One-shot SQL: allow buying every existing product when out-of-stock.

        Called from data XML on every module update. Idempotent.
        """
        if not column_exists(self.env.cr, 'product_template', 'allow_out_of_stock_order'):
            return
        self.env.cr.execute("""
            UPDATE product_template
               SET allow_out_of_stock_order = TRUE
             WHERE allow_out_of_stock_order IS DISTINCT FROM TRUE;
        """)

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
        # `base_domain` is a list of domains (one per shop filter), not a
        # single domain. Combine them before searching.
        base_domain = Domain.AND(search_detail.get('base_domain', []))

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
        Returns float: the tax-EXCLUDED final price the customer should see.
        The caller is responsible for applying the website's tax display.
        """
        return product_or_template.baf_get_sales_price(partner=partner)

    def baf_website_display_price(self):
        """Price to show on the website product page, already adjusted for the
        website's tax_included/excluded radio and the current partner's fiscal
        position. Used by the custom QWeb template instead of
        t-field='product.list_price' (which reads the raw column and ignores
        the tax-display setting)."""
        self.ensure_one()
        partner = self.env.user.partner_id
        price = self._baf_final_price_for_partner(self, partner)
        return self._baf_apply_website_tax(self, price)

    def _baf_apply_website_tax(self, template, price, website=None, fiscal_position=None):
        """Apply the website's tax_included/excluded display to a raw BAF price."""
        website = website or self.env['website'].get_current_website()
        currency = website.currency_id

        if fiscal_position is None:
            # Standard website_sale resolves fiscal position via request; fall back
            # to the website's session helper when called outside a request (cron,
            # tests, batch).
            if request and hasattr(request, 'fiscal_position'):
                fiscal_position = request.fiscal_position
            else:
                fiscal_position = website.sudo()._get_and_cache_current_fiscal_position() \
                    if request else self.env['account.fiscal.position'].sudo()

        product_taxes = template.sudo().taxes_id._filter_taxes_by_company(self.env.company)
        if not product_taxes:
            return price
        taxes = fiscal_position.map_tax(product_taxes) if fiscal_position else product_taxes
        return self._apply_taxes_to_price(
            price, currency, product_taxes, taxes, template, website=website,
        )

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
        final_price = self._baf_apply_website_tax(self, final_price)

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
            final_price = self._baf_apply_website_tax(template, final_price)
            if template.id in prices:
                prices[template.id]['price_reduce'] = final_price
                prices[template.id]['list_price'] = final_price
                prices[template.id]['base_price'] = final_price

        return prices
