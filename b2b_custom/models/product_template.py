from odoo import models, fields, api
from odoo.fields import Domain


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    replaced_by_id = fields.Many2one('product.template', string='replaced by', help="Select the product that replaces this product")
    unit_of_sales = fields.Integer(string='Unit of Sales', help="Unit of Sales is the minimum number for a product to be sold")
    
    default_code = fields.Char(compute='_compute_internal_reference', store=True, readonly=False)
    
    @api.depends('brand', 'sku')
    def _compute_internal_reference(self):
        for rec in self:
            if rec.sku:
                if rec.brand and rec.brand.name:
                    if len(rec.brand.name) >= 3:
                        rec.default_code = f"{rec.brand.name[:3].upper()}_{rec.sku}"
                    else:
                        rec.default_code = f"{rec.brand.name.upper()}_{rec.sku}"

    def _search_render_results(self, fetch_fields, mapping, icon, limit):
        """ 
        Override to inject custom fields (sku, brand) into the 
        global website search results dictionary.
        """
        # 1. Get the standard list of dictionaries from Odoo (contains name, price, image, etc.)
        results_data = super()._search_render_results(fetch_fields, mapping, icon, limit)
        
        # 2. Loop through both the Odoo records (self) and the dictionaries (results_data)
        for product, data in zip(self, results_data):
            # 3. Inject our custom fields into the dictionary for the frontend
            data['sku'] = product.sku or ''
            data['brand'] = product.brand.name if product.brand else ''
            
        return results_data

    @api.model
    def _search_fetch(self, search_detail, search, limit, order):
        """
        Override website search to be a STRICT EXACT SKU LOOKUP ONLY.
        Optimized for large databases using B-Tree index matching.
        """
        if not search:
            base_domain = search_detail.get('base_domain', [])
            
            results = self.search(base_domain, limit=limit, order=order)
            
            count = self.search_count(base_domain, limit=50000)
            
            return results, count
            
        base_domain = search_detail.get('base_domain', [])
        search_terms = [search, search.upper(), search.lower()]
        
        exact_domain = Domain.AND([
            base_domain,
            ['|', ('sku', 'in', search_terms), ('default_code', 'in', search_terms)]
        ])
        
        exact_results = self.search(exact_domain, limit=limit, order=order)
        
        return exact_results, len(exact_results)
                              
    def _get_combination_info(self, combination=False, product_id=False, add_qty=1.0, uom_id=False, only_template=False):
        res = super()._get_combination_info(combination, product_id, add_qty, uom_id, only_template)
        
        partner = self.env.user.partner_id
        
        product = self.env['product.product'].browse(res.get('product_id')) if res.get('product_id') else self
        
        def get_percentage(disc_code_record):
            if not disc_code_record or not partner:
                return 0.0
            value_line = disc_code_record.value_ids.filtered(lambda v: v.partner_id == partner)
            if value_line:
                return value_line[0].percentage
            return 0.0

        d1_pct = get_percentage(getattr(product, 'disc_code_1', False))
        d2_pct = get_percentage(getattr(product, 'disc_code_2', False))
        surcharge = getattr(product, 'surcharge', 0.0)
        retail_price = product.list_price

        price_after_d1 = retail_price * (1 - (d1_pct / 100.0))
        price_after_d2 = price_after_d1 * (1 - (d2_pct / 100.0))
        final_price = price_after_d2 + surcharge

        res['price'] = final_price
        res['list_price'] = final_price 
        res['has_discounted_price'] = False # Prevents Odoo from showing a strike-through discount
        
        return res

    def _get_sales_prices(self, *args, **kwargs):
        """ 
        Overrides the batch price fetching for the main /shop catalog page 
        so that products in the grid also show the fully discounted B2B price.
        """
        # 1. Fetch the standard dictionary of prices from Odoo
        prices = super()._get_sales_prices(*args, **kwargs)
        
        # 2. Identify the logged-in customer
        partner = self.env.user.partner_id
        
        # 3. Loop through all the products currently displayed on the shop page
        for template in self:
            
            def get_percentage(disc_code_record):
                if not disc_code_record or not partner:
                    return 0.0
                # NOTE: It is much safer to compare .id to avoid <NewId> bugs!
                value_line = disc_code_record.value_ids.filtered(lambda v: v.partner_id.id == partner.id)
                if value_line:
                    return value_line[0].percentage
                return 0.0

            # 4. Fetch the customer-specific discount values
            d1_pct = get_percentage(template.disc_code_1)
            d2_pct = get_percentage(template.disc_code_2)
            surcharge = template.surcharge or 0.0
            retail_price = template.list_price

            # 5. Apply the Custom Formula
            price_after_d1 = retail_price * (1 - (d1_pct / 100.0))
            price_after_d2 = price_after_d1 * (1 - (d2_pct / 100.0))
            final_price = price_after_d2 + surcharge

            # 6. OVERRIDE Website Grid Prices
            if template.id in prices:
                # We overwrite all price keys to ensure the original retail price 
                # is completely hidden and no strike-through discount is shown.
                prices[template.id]['price_reduce'] = final_price
                prices[template.id]['list_price'] = final_price
                prices[template.id]['base_price'] = final_price

        return prices

    