from odoo import models, fields, api


class ProductBrand(models.Model):
    _name = 'product.brand'
    _description = 'Product Brand'

    name = fields.Char(string='Brand Name', required=True)
    description = fields.Text(string='Description')


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    sku = fields.Char(string='SKU', help="SKU of the product unique for each brand", index=True)
    brand = fields.Many2one('product.brand', string='Brand', help="Select the brand for this product")
    default_code = fields.Char(index=True)
    disc_code_1 = fields.Many2one(string='Disc Code 1', comodel_name='discount.code')
    disc_code_2 = fields.Many2one(string='Disc Code 2', comodel_name='discount.code')
    origin = fields.Many2one(string='Origin', comodel_name='res.country')
    hs_code = fields.Char(string='HS Code')
    surcharge = fields.Monetary(string='Surcharge')

    _default_code_uniq = models.Constraint(
        'unique(default_code)',
        'The Internal Reference (SKU Odoo) must be unique!'
    )

    def _compute_barcode_from_code(self, default_code):
        if not default_code or '_' not in default_code:
            return False

        parts = default_code.split('_', 1)
        number_part = parts[-1]

        prefix = default_code[:3].upper()

        if prefix in ['MAS', 'FER']:
            return number_part.zfill(9)
        else:
            return number_part

    @api.onchange('default_code')
    def _onchange_default_code(self):
        if self.default_code:
            barcode = self._compute_barcode_from_code(self.default_code)
            if barcode:
                self.barcode = barcode

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('default_code') and not vals.get('barcode'):
                barcode = self._compute_barcode_from_code(vals['default_code'])
                if barcode:
                    vals['barcode'] = barcode

        records = self.env['product.template']
        to_create = []

        codes = [v.get('default_code') for v in vals_list if v.get('default_code')]

        existing_map = {}
        if codes:
            domain = [('default_code', 'in', codes), ('active', 'in', [True, False])]
            existing_products = self.search(domain)
            for prod in existing_products:
                existing_map[prod.default_code] = prod

        for vals in vals_list:
            ref = vals.get('default_code')
            if ref and ref in existing_map:
                existing_rec = existing_map[ref]
                existing_rec.write(vals)
                records |= existing_rec
            else:
                to_create.append(vals)

        if to_create:
            created_records = super().create(to_create)
            records |= created_records

        return records

    @api.model
    def get_import_templates(self):
        return [{
            'label': self.env._('Import Template for Products'),
            'template': '/general_system_custom/static/xls/intelliwise_products_template_excel.xlsx'
        }]


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def _name_search(self, name='', domain=None, operator='ilike', limit=100, order=None):
        """Try exact SKU match first — instant due to B-tree index on default_code.
        Falls back to standard name/ilike search only when no exact match is found.
        This fixes the slow search on large product catalogs (1M+ records).
        """
        if name:
            exact_domain = (domain or []) + [('default_code', '=', name), ('sku', '=', name)]
            records = self.search(exact_domain, limit=limit, order=order)
            if records:
                return records
        return super()._name_search(
            name=name, domain=domain, operator=operator, limit=limit, order=order,
        )
