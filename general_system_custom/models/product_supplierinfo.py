from odoo import fields, models


class ProductSupplierinfo(models.Model):
    _inherit = 'product.supplierinfo'

    # Core leaves this unindexed; every direct-price lookup filters on it alone.
    partner_id = fields.Many2one(index=True)

    # Non-stored: searchable/sortable via a JOIN, no column on a 400k-row table.
    baf_sku = fields.Char(
        related='product_tmpl_id.sku',
        string='SKU',
        readonly=True,
    )
    baf_brand_id = fields.Many2one(
        related='product_tmpl_id.brand',
        string='Brand',
        readonly=True,
    )
    # product_tmpl_id renders "[code] name"; product_name is the vendor's label.
    baf_product_name = fields.Char(
        related='product_tmpl_id.name',
        string='Product Name',
        readonly=True,
    )
