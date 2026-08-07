from odoo import _, api, fields, models
from odoo.exceptions import UserError


class BafDirectImportAmbiguous(models.Model):
    """One row per file line whose SKU exists under several brands in the
    catalogue and could not be assigned automatically. Staff pick a brand
    per row from the candidate list, then trigger
    `res.partner.action_resolve_direct_import_ambiguous` to insert them."""
    _name = 'baf.direct.import.ambiguous'
    _description = 'Direct Import: Ambiguous Row Awaiting Brand Choice'
    _order = 'sku, id'

    partner_id = fields.Many2one(
        'res.partner', string='Vendor',
        required=True, ondelete='cascade', index=True,
    )
    sku = fields.Char(required=True, index=True)
    price = fields.Float(required=True)
    file_brand = fields.Char(
        string='Brand from File',
        help="Brand value read from the imported file, if the file had a "
             "brand column. Empty when the file didn't provide one.",
    )
    candidate_ids = fields.Many2many(
        'product.template',
        'baf_direct_import_ambiguous_candidate_rel',
        'ambiguous_id', 'template_id',
        string='Candidate Products',
    )
    candidate_brand_ids = fields.Many2many(
        'product.brand',
        compute='_compute_candidate_brand_ids',
        string='Candidate Brands',
    )
    selected_brand_id = fields.Many2one(
        'product.brand',
        string='Brand',
        help="Pick the brand the file row applies to. Only candidates that "
             "actually own this SKU are listed.",
    )

    @api.depends('candidate_ids.brand')
    def _compute_candidate_brand_ids(self):
        for row in self:
            row.candidate_brand_ids = row.candidate_ids.mapped('brand')

    @api.constrains('selected_brand_id', 'candidate_ids')
    def _check_selected_brand_is_candidate(self):
        for row in self:
            if row.selected_brand_id and row.selected_brand_id not in row.candidate_brand_ids:
                raise UserError(_(
                    "Selected brand %(brand)s does not carry SKU %(sku)s.",
                ) % {'brand': row.selected_brand_id.display_name, 'sku': row.sku})

    def _resolved_product_tmpl(self):
        """The single product.template this row resolves to given the picked
        brand, or an empty recordset when the pick is still ambiguous or
        missing."""
        self.ensure_one()
        if not self.selected_brand_id:
            return self.env['product.template']
        return self.candidate_ids.filtered(
            lambda t: t.brand == self.selected_brand_id)[:1]
