import re

from odoo import models, fields, api


# ── Brand family detection ───────────────────────────────────────────────────
# Patterns are evaluated in order against an UPPERCASED, separator-normalized
# brand label (hyphens / underscores / slashes replaced with spaces, runs of
# whitespace collapsed). First match wins.
#
# Examples that match the JLR family:
#   "Jaguar", "JAG", "Land Rover", "LANDROVER", "Range Rover", "RR",
#   "JLR", "LR", "J"  (any of these as a standalone word or substring)
#
# Examples that match Mercedes:
#   "Mercedes", "Mercedes-Benz", "MERCEDES BENZ", "Benz", "MB"
# ─────────────────────────────────────────────────────────────────────────────

MOD_MOTORCYCLE = 'motorcycle'

_BRAND_PATTERNS = (
    # (regex, base_column_key, brand_family)
    (re.compile(r'\bBMW\b'),                                                'BMW',      'bmw_mini'),
    (re.compile(r'\bMINI\b'),                                               'MINI',     'bmw_mini'),
    (re.compile(r'JAGUAR|ROVER|\bJLR\b|\bJAG\b|\bLR\b|\bRR\b|\bJ\b'),       'JLR',      'jlr'),
    (re.compile(r'MERCEDES|BENZ|\bMB\b'),                                   'MERCEDES', 'mercedes'),
)


def _normalize_brand(name):
    if not name:
        return ''
    norm = re.sub(r'[-_/\\]+', ' ', str(name).upper())
    norm = re.sub(r'\s+', ' ', norm).strip()
    return norm


def resolve_baf_brand_info(brand_name, type_code=0, mod='car'):
    """
    Smart brand → (column_key, brand_family) resolver.

    Returns a (column_key, brand_family) tuple. brand_family is one of
    'bmw_mini', 'jlr', 'mercedes', 'other'. column_key is '' for blank brand,
    `<BRAND>_T12` / `<BRAND>_T39` for BMW/MINI (per type_code), the family
    name for JLR/MERCEDES, or the cleaned brand name for unknowns.
    """
    norm = _normalize_brand(brand_name)
    if not norm:
        return ('', 'other')

    for pattern, base_key, family in _BRAND_PATTERNS:
        if pattern.search(norm):
            if family == 'bmw_mini':
                tc = type_code or 0
                if tc in (1, 2):
                    bucket = 'T12'
                elif tc >= 3:
                    bucket = 'T39'
                else:
                    bucket = 'T12'
                return (f'{base_key}_{bucket}', 'bmw_mini')
            return (base_key, family)

    return (norm.replace(' ', '_'), 'other')


class ProductTemplateBafPricing(models.Model):
    _inherit = 'product.template'

    # ── Supplier / pricing route ──────────────────────────────────────────────
    supplier_route = fields.Selection(
        selection=[
            ('de_table',  'DE Supplier — Discount Table'),
            ('eu_direct', 'EU Supplier — Direct Price'),
        ],
        string='Supplier Pricing Route',
        default='de_table',
        help=(
            "de_table : German supplier (Sup1/Sup2/Sup3). "
            "           Purchase price = UPE × (1 − discount%). "
            "eu_direct: EU supplier sends the net price directly (standard vendor pricelist)."
        ),
    )

    # ── Discount code (alphanumeric) ──────────────────────────────────────────
    baf_discount_code = fields.Char(
        string='BAF Discount Code',
        default='0',
        help="Discount code used to look up the effective discount %% in the BAF "
             "discount table. Numeric for BMW/MINI (e.g. '0'..'60'), alphanumeric "
             "for JLR (e.g. '1A', '2D') or Mercedes (e.g. 'M03').",
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

    # ── Computed column key ───────────────────────────────────────────────────
    baf_column_key = fields.Char(
        string='Column Key',
        compute='_compute_baf_column_key',
        store=True,
        help="Auto-computed from Brand + Type Code + Mod. "
             "Used as the base key for discount table lookups. "
             "Empty = no table lookup available for this product.",
    )

    # ── Brand family (drives BMW/MINI-only UI sections) ──────────────────────
    baf_brand_family = fields.Selection(
        selection=[
            ('bmw_mini', 'BMW / MINI'),
            ('jlr',      'Jaguar / Land Rover / Range Rover'),
            ('mercedes', 'Mercedes'),
            ('other',    'Other'),
        ],
        string='Brand Family',
        compute='_compute_baf_column_key',
        store=True,
        help="Auto-derived from the product's Brand. Drives which fields apply.",
    )

    @api.depends('brand', 'brand.name', 'baf_type_code', 'baf_mod')
    def _compute_baf_column_key(self):
        # Note: motorcycles keep their brand/type key (e.g. BMW_T12) so that
        # sales lookups land on BMW_T12_MOTO / MINI_T39_MOTO. Purchase pins
        # Supplier 3 + 'SUP3_MOTO' explicitly inside baf_get_purchase_price.
        for rec in self:
            brand_name = rec.brand.name if rec.brand else ''
            column_key, family = resolve_baf_brand_info(
                brand_name, type_code=rec.baf_type_code, mod=rec.baf_mod,
            )
            rec.baf_column_key = column_key
            rec.baf_brand_family = family

    # ── Pricing engine entry point ────────────────────────────────────────────

    def baf_get_purchase_price(self, supplier_code='SUP1'):
        """
        Compute the BAF purchase price for this product.

        supplier_code: 'SUP1', 'SUP2', or 'SUP3'
        Returns float (net purchase price) or None if route is eu_direct.

        Logic:
          eu_direct  → return None (use standard Odoo vendor pricelist)
          de_table   → look up discount table → apply SB surcharge if needed
        """
        self.ensure_one()

        if self.supplier_route == 'eu_direct':
            return None

        # ── de_table path ──
        # Motorcycle parts: always Supplier 3, single column SUP3_MOTO
        # (purchase side has no brand/type split for moto)
        if self.baf_mod == MOD_MOTORCYCLE:
            full_column_key = 'SUP3_MOTO'
        else:
            column_key = self.baf_column_key
            if not column_key:
                return None
            # SB products always read their base discount from Supplier 1.
            # The caller's supplier_code still matters for surcharge handling.
            lookup_supplier = 'SUP1' if self.baf_mod == 'sb' else supplier_code
            full_column_key = f"{lookup_supplier}_{column_key}"

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

    def baf_get_sales_price_details(self, partner=None):
        """
        Resolve the sales price and report which lookup was used.

        Returns a dict:
          {
            'price':         final sales price (incl. surcharge),
            'column_key':    full lookup key used (e.g. 'BMW_T12_MOTO'), or '' if N/A,
            'discount_pct':  % applied (0.0 for guests / markup_pct path),
            'pricing_method': 'guest' | 'markup_pct' | 'table_lookup',
          }
        """
        self.ensure_one()

        upe = self.list_price
        surcharge = self.surcharge or 0.0

        # Guest or no partner → full UPE
        if not partner or not partner.sales_group_ids:
            return {
                'price': upe + surcharge,
                'column_key': '',
                'discount_pct': 0.0,
                'pricing_method': 'guest',
            }

        # Pick the customer's group whose brand_family matches the product.
        # A customer can hold one group per family (BMW_MINI_GR1 for BMW,
        # JLR_GR4 for Jaguar, MERCEDES_GR1 for Mercedes). Wildcard groups
        # (brand_family='all') act as a catch-all when no exact match exists.
        product_family = self.baf_brand_family or 'other'
        groups = partner.sales_group_ids.filtered(lambda g: g.active)
        group = (
            groups.filtered(lambda g: g.brand_family == product_family)[:1]
            or groups.filtered(lambda g: g.brand_family == 'all')[:1]
        )
        if not group:
            # Customer has groups, but none cover this product's family.
            # Treat them as a guest for this brand → full UPE.
            return {
                'price': upe + surcharge,
                'column_key': '',
                'discount_pct': 0.0,
                'pricing_method': 'guest',
            }
        group = group[0]

        if group.pricing_method == 'markup_pct':
            # EU-direct fallback: use UPE as base unless caller supplies a vendor price.
            markup = group.markup_pct or 0.0
            sales_price = upe * (1.0 + markup / 100.0)
            return {
                'price': sales_price + surcharge,
                'column_key': '',
                'discount_pct': markup,
                'pricing_method': 'markup_pct',
            }

        # table_lookup — resolved column key (BMW_T12, MINI_T39, JLR, MERCEDES, …)
        # Motorcycle BMW/MINI parts: override the group suffix with MOTO so the
        # lookup lands on BMW_T12_MOTO / MINI_T39_MOTO regardless of the
        # customer's car-side group (GR1..GR4).
        column_key = self.baf_column_key
        if self.baf_mod == MOD_MOTORCYCLE and self.baf_brand_family == 'bmw_mini':
            suffix = 'MOTO'
        else:
            suffix = group.group_column_suffix or 'GR1'
        full_column_key = f"{column_key}_{suffix}" if column_key else ''

        discount_pct = self.env['baf.discount.line'].get_discount_pct(
            table_type='sales',
            column_key=full_column_key,
            discount_code=self.baf_discount_code,
        ) if full_column_key else 0.0
        sales_price = upe * (1.0 - discount_pct / 100.0)

        return {
            'price': sales_price + surcharge,
            'column_key': full_column_key,
            'discount_pct': discount_pct,
            'pricing_method': 'table_lookup',
        }

    def baf_get_sales_price(self, partner=None):
        """Backward-compatible wrapper returning just the price."""
        return self.baf_get_sales_price_details(partner=partner)['price']

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

    def baf_get_sales_price_details(self, partner=None):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_sales_price_details(partner=partner)
