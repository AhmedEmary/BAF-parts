from odoo import models, fields, api


# ── Column-key resolution matrix ─────────────────────────────────────────────
#
# The column_key is built from three product fields:
#   brand_code  (e.g. 'BMW', 'MINI', 'LR', 'MB')
#   type_bucket ('T12' for type_code 1-2, 'T39' for 3-9, '' if brand has no type)
#   mod_code    ('MOTO' if mod='motorcycle', '' otherwise)
#
# The full key is assembled as:  {brand_code}_{type_bucket}   or   {brand_code}_MOTO
#
# For a supplier purchase key, the supplier prefix is added by the pricing engine:
#   SUP1_BMW_T12,  SUP2_MINI_T39,  SUP3_MOTO
#
# For a sales key, the group suffix is added by the pricing engine:
#   BMW_T12_GR1,  MINI_T39_GR2,  MOTO_GR1
#
# Brands that have NO table (LR, MB when using markup_pct) return '' so the
# engine falls through to the markup_pct path.
# ─────────────────────────────────────────────────────────────────────────────

BRANDS_WITH_TYPE = {'BMW', 'MINI'}       # brands that use T12/T39 split
BRANDS_TABLE_ONLY = {'BMW', 'MINI'}      # brands that use discount table pricing
MOD_MOTORCYCLE = 'motorcycle'


class ProductTemplateBafPricing(models.Model):
    _inherit = 'product.template'

    # ── Supplier / pricing route ──────────────────────────────────────────────
    supplier_route = fields.Selection(
        selection=[
            ('de_table',  'DE Supplier — Discount Table'),
            ('lr_level',  'LR/JLR — Pre-calculated Price Levels'),
            ('eu_direct', 'EU Supplier — Direct Price'),
        ],
        string='Supplier Pricing Route',
        default='eu_direct',
        help=(
            "de_table : German supplier (Sup1/Sup2/Sup3). "
            "           Purchase price = UPE × (1 − discount%). "
            "lr_level : JLR master file. Price levels stored directly on the product. "
            "eu_direct: EU supplier sends the net price directly (standard vendor pricelist)."
        ),
    )

    # ── Discount code (integer 0–60) ──────────────────────────────────────────
    baf_discount_code = fields.Integer(
        string='BAF Discount Code #',
        default=0,
        help="Integer discount code (0–60) used to look up the effective "
             "discount % in the BAF discount table. Required for DE supplier products.",
    )

    # ── Type code (1–9) ───────────────────────────────────────────────────────
    baf_type_code = fields.Integer(
        string='Type Code (1–9)',
        default=0,
        help="BMW/MINI type code. 1–2 → T12 column, 3–9 → T39 column. "
             "Leave 0 for brands without a type split.",
    )

    # ── Mod ───────────────────────────────────────────────────────────────────
    baf_mod = fields.Selection(
        selection=[
            ('car',        'Car'),
            ('motorcycle', 'Motorcycle'),
            ('sb',         'SB (Supplier 1 surcharge)'),
        ],
        string='Mod',
        default='car',
        help="Controls which sub-table to use and whether the SB surcharge applies.",
    )

    # ── SB surcharge override (Supplier 1 only) ───────────────────────────────
    # The default −5.2% is set on the supplier group; individual products can
    # override it by leaving this at 0 (= use group default).
    baf_sb_surcharge_override = fields.Float(
        string='SB Surcharge Override %',
        default=0.0,
        help="Leave 0 to use the default SB surcharge from the supplier configuration. "
             "Set a non-zero value to override for this product only.",
    )

    # ── Pre-calculated LR price levels (JLR master file) ─────────────────────
    baf_lr_price_level_1 = fields.Float(string='LR Price Level 1 (highest discount)', digits=(16, 4))
    baf_lr_price_level_2 = fields.Float(string='LR Price Level 2', digits=(16, 4))
    baf_lr_price_level_3 = fields.Float(string='LR Price Level 3', digits=(16, 4))
    baf_lr_price_level_4 = fields.Float(string='LR Price Level 4', digits=(16, 4))
    baf_lr_price_level_5 = fields.Float(string='LR Price Level 5', digits=(16, 4))
    baf_lr_price_level_6 = fields.Float(string='LR Price Level 6 (closest to UPE)', digits=(16, 4))

    # ── Computed column key ───────────────────────────────────────────────────
    baf_column_key = fields.Char(
        string='Column Key',
        compute='_compute_baf_column_key',
        store=True,
        help="Auto-computed from Brand + Type Code + Mod. "
             "Used as the base key for discount table lookups. "
             "Empty = no table lookup available for this product.",
    )

    @api.depends('brand', 'baf_type_code', 'baf_mod')
    def _compute_baf_column_key(self):
        for rec in self:
            brand_name = rec.brand.name.upper().strip() if rec.brand and rec.brand.name else ''

            # Motorcycle always wins — single column regardless of brand/type
            if rec.baf_mod == MOD_MOTORCYCLE:
                rec.baf_column_key = 'MOTO'
                continue

            if not brand_name or brand_name not in BRANDS_TABLE_ONLY:
                rec.baf_column_key = ''
                continue

            if brand_name in BRANDS_WITH_TYPE:
                if rec.baf_type_code in (1, 2):
                    type_bucket = 'T12'
                elif rec.baf_type_code >= 3:
                    type_bucket = 'T39'
                else:
                    type_bucket = 'T12'   # default fallback
                rec.baf_column_key = f"{brand_name}_{type_bucket}"
            else:
                rec.baf_column_key = brand_name

    # ── Pricing engine entry point ────────────────────────────────────────────

    def baf_get_purchase_price(self, supplier_code='SUP1'):
        """
        Compute the BAF purchase price for this product.

        supplier_code: 'SUP1', 'SUP2', or 'SUP3'
        Returns float (net purchase price) or None if route is eu_direct.

        Logic:
          eu_direct  → return None (use standard Odoo vendor pricelist)
          lr_level   → return None (use baf_lr_price_levelX fields directly)
          de_table   → look up discount table → apply SB surcharge if needed
        """
        self.ensure_one()

        if self.supplier_route == 'eu_direct':
            return None

        if self.supplier_route == 'lr_level':
            return None   # caller reads baf_lr_price_levelX

        # ── de_table path ──
        column_key = self.baf_column_key
        if not column_key:
            return None

        # For Motorcycle parts: only Supplier 3
        if column_key == 'MOTO':
            supplier_code = 'SUP3'

        full_column_key = f"{supplier_code}_{column_key}"

        discount_pct = self.env['baf.discount.line'].get_discount_pct(
            table_type='purchase',
            column_key=full_column_key,
            discount_code=self.baf_discount_code,
        )

        upe = self.list_price
        purchase_price = upe * (1.0 - discount_pct / 100.0)

        # SB surcharge — Supplier 1 only
        if self.baf_mod == 'sb' and supplier_code == 'SUP1':
            surcharge_pct = self.baf_sb_surcharge_override or 5.2
            purchase_price = purchase_price * (1.0 - surcharge_pct / 100.0)

        return purchase_price

    def baf_get_sales_price(self, partner=None):
        """
        Compute the BAF sales price for this product for a given partner.

        partner: res.partner record (or None for guest)
        Returns float (final sales price incl. surcharge).

        Logic:
          Guest / no group          → return list_price (UPE = MSRP)
          group.pricing_method = markup_pct   → purchase_price × (1 + markup%)
          group.pricing_method = table_lookup → UPE × (1 − sales_discount%)
          Then adds product.surcharge on top.
        """
        self.ensure_one()

        upe = self.list_price
        surcharge = self.surcharge or 0.0

        # Guest or no partner → full UPE
        if not partner or not partner.sales_group_id:
            return upe + surcharge

        group = partner.sales_group_id

        if group.pricing_method == 'markup_pct':
            # LR, MB, EU-supplier brands
            # Purchase price: for LR use level stored on product, else eu_direct = vendor price
            if self.supplier_route == 'lr_level':
                # Default to level 5 for normal B2C (partner can override via group suffix)
                level = int(group.group_column_suffix or '5')
                level = max(1, min(6, level))
                purchase_price = getattr(self, f'baf_lr_price_level_{level}', upe)
            else:
                # EU direct: use the standard Odoo vendor pricelist price as base
                # (caller can pass purchase_price explicitly if needed)
                purchase_price = upe  # fallback; refined by caller if needed

            markup = group.markup_pct or 0.0
            sales_price = purchase_price * (1.0 + markup / 100.0)

        else:
            # table_lookup — BMW / MINI
            column_key = self.baf_column_key
            suffix = group.group_column_suffix or 'GR1'
            full_column_key = f"{column_key}_{suffix}"

            discount_pct = self.env['baf.discount.line'].get_discount_pct(
                table_type='sales',
                column_key=full_column_key,
                discount_code=self.baf_discount_code,
            )
            sales_price = upe * (1.0 - discount_pct / 100.0)

        return sales_price + surcharge

class ProductProductBafPricing(models.Model):
    _inherit = 'product.product'

    def baf_get_purchase_price(self, supplier_code='SUP1'):
        self.ensure_one()
        # Delegate the call to the underlying product.template
        return self.product_tmpl_id.baf_get_purchase_price(supplier_code=supplier_code)

    def baf_get_sales_price(self, partner=None):
        self.ensure_one()
        # Delegate the call to the underlying product.template
        return self.product_tmpl_id.baf_get_sales_price(partner=partner)
