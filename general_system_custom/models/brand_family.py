from odoo import models, fields, api, _


class BafBrandFamily(models.Model):
    """A group of product brands that share one sales discount table.

    Brands in the same family are priced together: their discount lines share a
    column key, and a customer's sales group is scoped to a family rather than to
    individual brands. Every brand belongs to exactly one family; a brand created
    on its own gets a family of its own (see product.brand.create). Merge brands
    by moving them onto a shared family (e.g. Jaguar + Land Rover -> one 'JLR'
    family)."""
    _name = 'baf.brand.family'
    _description = 'BAF Brand Family'
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)

    _name_uniq = models.Constraint(
        'unique(name)',
        'A brand family with this name already exists.',
    )
    brand_ids = fields.One2many('product.brand', 'family_id', string='Brands')
    brand_count = fields.Integer(compute='_compute_brand_count')
    note = fields.Char(
        string='Note',
        help="Free text, e.g. why these brands share one discount table.",
    )

    @api.depends('brand_ids')
    def _compute_brand_count(self):
        for family in self:
            family.brand_count = len(family.brand_ids)

    def write(self, vals):
        res = super().write(vals)
        if 'name' in vals:
            # Family name feeds baf_sales_column_key on every product of every
            # brand in this family (BMW alone backs ~650K products). Bulk-update
            # via SQL — see ProductTemplate._baf_bulk_recompute_sales_key_for_brands.
            brand_ids = self.mapped('brand_ids').ids
            if brand_ids:
                self.env['product.template']._baf_bulk_recompute_sales_key_for_brands(brand_ids)
        return res
