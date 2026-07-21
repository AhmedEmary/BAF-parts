from odoo import models, fields, api
from odoo.tools.image import is_image_size_above


class ProductBrand(models.Model):
    _name = 'product.brand'
    _inherit = ['image.mixin']
    _description = 'Product Brand'

    name = fields.Char(string='Brand Name', required=True)
    description = fields.Text(string='Description')

    _name_uniq = models.Constraint(
        'unique(name)',
        'A brand with this name already exists.',
    )
    family_id = fields.Many2one(
        'baf.brand.family',
        string='Brand Family',
        ondelete='restrict',
        index=True,
        help="Brands in the same family share one sales discount table and are "
             "priced by the same customer group. A new brand gets its own family "
             "automatically; move it onto a shared family to merge.",
    )
    is_public = fields.Boolean(
        string='Publicly Available',
        default=False,
        help="If checked, this brand is visible to all users (including guests) in the e-commerce."
    )

    @api.model_create_multi
    def create(self, vals_list):
        # A brand with no family gets one named after it, so every brand always
        # belongs to exactly one family (merge later by reassigning family_id).
        # Reuse a same-named family if one already exists, since family names are
        # unique — creating a duplicate would otherwise fail.
        Family = self.env['baf.brand.family']
        for vals in vals_list:
            if not vals.get('family_id') and vals.get('name'):
                family = Family.search([('name', '=', vals['name'])], limit=1)
                vals['family_id'] = (family or Family.create({'name': vals['name']})).id
        return super().create(vals_list)

    def write(self, vals):
        # Moving a brand into a family removes it from its old one (family_id is
        # a Many2one, so membership is already exclusive). If that old family is
        # then left with no brands and no pricing group, drop it so merged brands
        # don't leave orphaned single-brand families behind.
        old_families = self.mapped('family_id') if 'family_id' in vals else None
        res = super().write(vals)
        if old_families:
            stale = old_families.filtered(
                lambda f: not f.brand_ids
                and not self.env['baf.sales.group'].search_count([('family_id', '=', f.id)])
            )
            stale.unlink()
        return res
    # Required by website_sale.shop_product_image when the brand record is
    # used as an image holder fallback for products that have no image.
    can_image_1024_be_zoomed = fields.Boolean(
        string="Can Image 1024 be zoomed",
        compute='_compute_can_image_1024_be_zoomed',
        store=True,
    )

    @api.depends('image_1920', 'image_1024')
    def _compute_can_image_1024_be_zoomed(self):
        for brand in self:
            brand.can_image_1024_be_zoomed = bool(
                brand.image_1920
                and is_image_size_above(brand.image_1920, brand.image_1024)
            )


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    sku = fields.Char(string='SKU', help="SKU of the product unique for each brand", index=True)
    brand = fields.Many2one('product.brand', string='Brand', help="Select the brand for this product", index=True)
    default_code = fields.Char(index=True)
    origin = fields.Many2one(string='Origin', comodel_name='res.country')
    hs_code = fields.Char(string='HS Code')
    surcharge = fields.Monetary(string='Surcharge')
    # ── Physical dimensions (cm) ──────────────────────────────────────────────
    height = fields.Float(string='Height (cm)', digits=(10, 4),
                          help="Product height in centimetres.")
    width  = fields.Float(string='Width (cm)',  digits=(10, 4),
                          help="Product width in centimetres.")
    length = fields.Float(string='Length (cm)', digits=(10, 4),
                          help="Product length in centimetres.")
    weight = fields.Float(string='Weight (kg)', help="Product weight in kilograms.")

    # h/w/l are stored in cm; volume is stored as cm3 for this project.
    # Keep enough precision for fractional dimensions.
    volume = fields.Float(string='Volume (cm³)', compute='_compute_volume', store=True, digits=(16, 4))

    # ── Bulky goods classification ────────────────────────────────────────────
    # Default threshold: 45 000 cm³ (45 L).  Override per product with
    # force_bulky_goods, or change the system-wide default in Settings →
    # Technical → System Parameters → baf.bulky_volume_threshold_cm3.
    force_bulky_goods = fields.Boolean(
        string='Force Bulky Goods',
        default=False,
        help="Always classify this product as bulky regardless of its volume.",
    )
    is_bulky_goods = fields.Boolean(
        string='Bulky Goods',
        compute='_compute_is_bulky_goods',
        store=True,
        help="True when volume ≥ threshold (default 45 000 cm³) or when "
             "'Force Bulky Goods' is checked. Controls the shipping rate bracket.",
    )

    @api.depends('height', 'width', 'length')
    def _compute_volume(self):
        for rec in self:
            rec.volume = (rec.height or 0.0) * (rec.width or 0.0) * (rec.length or 0.0)

    @api.depends('volume', 'force_bulky_goods')
    def _compute_is_bulky_goods(self):
        threshold = float(
            self.env['ir.config_parameter'].sudo().get_param(
                'baf.bulky_volume_threshold_cm3', default='45000'
            )
        )
        for rec in self:
            rec.is_bulky_goods = rec.force_bulky_goods or (rec.volume or 0.0) >= threshold

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

    def _get_image_holder(self):
        # Fall back to the brand image when neither the template nor its
        # first variant has one. Only kicks in if a brand image exists.
        holder = super()._get_image_holder()
        if (
            holder == self
            and not self.image_128
            and self.brand
            and self.brand.image_128
        ):
            return self.brand
        return holder

    def _get_images(self):
        images = super()._get_images()
        if (
            not self.image_1920
            and not self.product_template_image_ids
            and self.brand
            and self.brand.image_1920
        ):
            return [self.brand]
        return images


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def _compute_image_1920(self):
        super()._compute_image_1920()
        for record in self:
            if not record.image_1920 and record.product_tmpl_id.brand.image_1920:
                record.image_1920 = record.product_tmpl_id.brand.image_1920

    def _compute_image_1024(self):
        super()._compute_image_1024()
        for record in self:
            if not record.image_1024 and record.product_tmpl_id.brand.image_1024:
                record.image_1024 = record.product_tmpl_id.brand.image_1024

    def _compute_image_512(self):
        super()._compute_image_512()
        for record in self:
            if not record.image_512 and record.product_tmpl_id.brand.image_512:
                record.image_512 = record.product_tmpl_id.brand.image_512

    def _compute_image_256(self):
        super()._compute_image_256()
        for record in self:
            if not record.image_256 and record.product_tmpl_id.brand.image_256:
                record.image_256 = record.product_tmpl_id.brand.image_256

    def _compute_image_128(self):
        super()._compute_image_128()
        for record in self:
            if not record.image_128 and record.product_tmpl_id.brand.image_128:
                record.image_128 = record.product_tmpl_id.brand.image_128

    def _get_images(self):
        images = super()._get_images()
        if (
            not self.image_variant_1920
            and not self.product_tmpl_id.image_1920
            and not self.product_variant_image_ids
            and not self.product_tmpl_id.product_template_image_ids
            and self.product_tmpl_id.brand
            and self.product_tmpl_id.brand.image_1920
        ):
            return [self.product_tmpl_id.brand]
        return images

    @api.model
    def name_search(self, name='', domain=None, operator='ilike', limit=100, order=None):
        """Try exact SKU match first — instant due to B-tree index on default_code."""
        if name:
            exact_domain = (domain or []) + [('default_code', '=', name), ('sku', '=', name)]
            records = self.search(exact_domain, limit=limit, order=order)
            if records:
                return records
        return super().name_search(
            name=name, domain=domain, operator=operator, limit=limit,
        )
