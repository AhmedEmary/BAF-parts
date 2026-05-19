import re

from odoo import _, models, fields, api


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

# BMW/MINI type-code split:
#   T12 column → 1, 2, 4, 6, 8
#   T39 column → 3, 5, 7, 9
# A missing/zero type code falls back to T12.
BAF_T39_TYPE_CODES = frozenset({3, 5, 7, 9})
BAF_T12_TYPE_CODES = frozenset({1, 2, 4, 6, 8})

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

    BMW/MINI type-code split:
        T12 → 1, 2, 4, 6, 8
        T39 → 3, 5, 7, 9
    A missing/zero type code falls back to T12.
    """
    norm = _normalize_brand(brand_name)
    if not norm:
        return ('', 'other')

    for pattern, base_key, family in _BRAND_PATTERNS:
        if pattern.search(norm):
            if family == 'bmw_mini':
                tc = type_code or 0
                bucket = 'T39' if tc in BAF_T39_TYPE_CODES else 'T12'
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
        help=(
            "de_table : German supplier (Sup1/Sup2/Sup3). "
            "           Purchase price = UPE × (1 − discount%). "
            "eu_direct: EU supplier sends the net price directly (standard vendor pricelist)."
        ),
    )

    # ── Discount code (alphanumeric) ──────────────────────────────────────────
    baf_discount_code = fields.Char(
        string='BAF Discount Code',
        help="Discount code used to look up the effective discount %% in the BAF "
             "discount table. Numeric for BMW/MINI (e.g. '0'..'60'), alphanumeric "
             "for JLR (e.g. '1A', '2D') or Mercedes (e.g. 'M03').",
    )

    # ── Type code (1–9) ───────────────────────────────────────────────────────
    baf_type_code = fields.Integer(
        string='Type Code (1–9)',
        help="BMW/MINI type code. "
             "T12 column → 1, 2, 4, 6, 8. "
             "T39 column → 3, 5, 7, 9. "
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
        help="Controls which sub-table to use and whether the SB surcharge applies.",
    )

    # ── SB surcharge override (Supplier 1 only) ───────────────────────────────
    # The default −5.2% is set on the supplier group; individual products can
    # override it by leaving this at 0 (= use group default).
    baf_sb_surcharge_override = fields.Float(
        string='SB Surcharge Override %',
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

        supplier_code: 'SUP1', 'SUP2', 'SUP3', or 'SUP_JLR'
        Returns float (net purchase price) or None if route is eu_direct.
        """
        details = self.baf_get_purchase_price_details(supplier_code=supplier_code)
        return details['price'] if details else None

    def baf_get_purchase_price_details(self, supplier_code='SUP1'):
        """
        Same calculation as baf_get_purchase_price but returns the lookup
        breakdown so callers (auto-vendor selection, comparison wizard) can
        explain *how* the price was derived.

        Returns a dict or None (eu_direct / no column key):
          {
            'price':         net purchase price,
            'column_key':    full lookup key used (e.g. 'SUP1_BMW_T12'),
            'discount_pct':  discount % applied,
            'sb_surcharge':  surcharge % applied (0.0 unless SB+SUP1),
            'pricing_method': 'discount_table',
          }
        """
        self.ensure_one()

        if self.supplier_route == 'eu_direct':
            return None

        if self.baf_mod == MOD_MOTORCYCLE:
            full_column_key = 'SUP3_MOTO'
        else:
            column_key = self.baf_column_key
            if not column_key:
                return None
            lookup_supplier = 'SUP1' if self.baf_mod == 'sb' else supplier_code
            full_column_key = f"{lookup_supplier}_{column_key}"

        discount_pct = self.env['baf.discount.line'].get_discount_pct(
            table_type='purchase',
            column_key=full_column_key,
            discount_code=self.baf_discount_code,
        )

        upe = self.list_price
        purchase_price = upe * (1.0 - discount_pct / 100.0)

        sb_surcharge = 0.0
        if self.baf_mod == 'sb' and supplier_code == 'SUP1':
            sb_surcharge = self.baf_sb_surcharge_override or 5.2
            purchase_price = purchase_price * (1.0 - sb_surcharge / 100.0)

        return {
            'price': round(purchase_price, 2),
            'column_key': full_column_key,
            'discount_pct': discount_pct,
            'sb_surcharge': sb_surcharge,
            'pricing_method': 'discount_table',
        }

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
        product_family = self.baf_brand_family or 'other'

        # Guest or no partner → full UPE
        if not partner:
            return {
                'price': upe + surcharge,
                'column_key': '',
                'discount_pct': 0.0,
                'pricing_method': 'guest',
            }

        # B2B EU VAT tier ─────────────────────────────────────────────────────
        # Registered customers with a VAT number from an EU country get a flat
        # −5 % on JLR products when they don't already have a JLR pricing group
        # (pricing groups always take priority and usually offer more).
        if (
            product_family == 'jlr'
            and getattr(partner, 'is_b2b_eu_vat', False)
            and not partner.sales_group_ids.filtered(
                lambda g: g.active and g.brand_family in ('jlr', 'all')
            )
        ):
            return {
                'price': upe * 0.95 + surcharge,
                'column_key': 'B2B_EU_VAT',
                'discount_pct': 5.0,
                'pricing_method': 'b2b_vat_discount',
            }

        # No pricing groups at all → full UPE
        if not partner.sales_group_ids:
            return {
                'price': upe + surcharge,
                'column_key': '',
                'discount_pct': 0.0,
                'pricing_method': 'guest',
            }

        # Pick the customer's group whose brand_family matches the product.
        # A customer can hold one car group + one moto group per family
        # (e.g. BMW_MINI_GR1 for BMW car parts AND BMW_MINI_MOTO for BMW
        # motorcycle parts). The moto tier is detected from the group's
        # column suffix == 'MOTO'. Wildcard groups (brand_family='all')
        # act as a catch-all when no exact match exists.
        is_moto_product = self.baf_mod == MOD_MOTORCYCLE and self.baf_brand_family == 'bmw_mini'
        groups = partner.sales_group_ids.filtered(lambda g: g.active)
        family_groups = groups.filtered(lambda g: g.brand_family == product_family)
        if is_moto_product:
            group = (
                family_groups.filtered(lambda g: g._is_moto_group())[:1]
                or family_groups.filtered(lambda g: not g._is_moto_group())[:1]
                or groups.filtered(lambda g: g.brand_family == 'all')[:1]
            )
        else:
            group = (
                family_groups.filtered(lambda g: not g._is_moto_group())[:1]
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
        return self.product_tmpl_id.baf_get_purchase_price(supplier_code=supplier_code)

    def baf_get_purchase_price_details(self, supplier_code='SUP1'):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_purchase_price_details(supplier_code=supplier_code)

    def baf_get_sales_price(self, partner=None):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_sales_price(partner=partner)

    def baf_get_sales_price_details(self, partner=None):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_sales_price_details(partner=partner)

    # ── Auto vendor selection ─────────────────────────────────────────────────
    # Tie-break order matches the client's spec:
    #   SUP1 wins on equal-price ties, then SUP2, then SUP3, then SUP_JLR,
    #   then EU_DIRECT. Motorcycle parts ignore everyone except SUP3 vendors.
    _BAF_VENDOR_PREFERENCE = ('SUP1', 'SUP2', 'SUP3', 'SUP_JLR', 'EU_DIRECT')

    def _baf_eligible_vendors(self):
        """Return vendors with a BAF supplier code that supply this brand.

        SUP3 is the motorcycle-only vendor: SUP3 partners are eligible only
        for motorcycle products. Conversely, non-motorcycle products exclude
        SUP3 vendors entirely (the brand entries on the SUP3 partner are
        informational — the moto filter takes precedence).
        """
        self.ensure_one()
        brand = self.brand
        if not brand:
            return self.env['res.partner']
        domain = [
            ('baf_supplier_code', '!=', False),
            ('baf_brand_ids', 'in', brand.id),
        ]
        if self.product_tmpl_id.baf_mod == MOD_MOTORCYCLE:
            domain.append(('baf_supplier_code', '=', 'SUP3'))
        else:
            domain.append(('baf_supplier_code', '!=', 'SUP3'))
        return self.env['res.partner'].search(domain)

    def _baf_supplierinfo_price(self, vendor):
        """Lookup the standard product.supplierinfo price for an EU-direct vendor."""
        self.ensure_one()
        seller = self.seller_ids.filtered(lambda s: s.partner_id == vendor)[:1]
        if not seller:
            seller = self.product_tmpl_id.seller_ids.filtered(
                lambda s: s.partner_id == vendor
            )[:1]
        return seller.price if seller else None

    def baf_get_best_vendor(self):
        """
        Auto-select the cheapest eligible vendor for this product.

        Returns:
          {
            'vendor':     winning res.partner record (or empty record),
            'price':      winning net price (or 0.0),
            'method':     'discount_table' | 'supplierinfo' | None,
            'reason':     human-readable explanation,
            'candidates': list of dicts (one per eligible vendor) with keys
                          vendor, price, method, column_key, discount_pct,
                          sb_surcharge, is_winner, note.
          }
        """
        self.ensure_one()

        empty_result = {
            'vendor': self.env['res.partner'],
            'price': 0.0,
            'method': None,
            'reason': '',
            'candidates': [],
        }

        is_motorcycle = self.product_tmpl_id.baf_mod == MOD_MOTORCYCLE
        eligible = self._baf_eligible_vendors()
        if not eligible:
            if is_motorcycle:
                empty_result['reason'] = _(
                    "Motorcycle product — only Supplier 3 vendors are eligible, "
                    "but none supply '%(brand)s'."
                ) % {'brand': self.brand.name if self.brand else ''}
            else:
                empty_result['reason'] = _(
                    "No non-moto vendor has '%(brand)s' in their Brands Supplied list."
                ) % {'brand': self.brand.name if self.brand else ''}
            return empty_result

        candidates = []
        for vendor in eligible:
            code = vendor.baf_supplier_code
            note = ''
            if code == 'EU_DIRECT':
                price = self._baf_supplierinfo_price(vendor)
                if price is None:
                    note = _("No supplier pricelist entry for this product.")
                    candidates.append({
                        'vendor': vendor,
                        'price': None,
                        'method': 'supplierinfo',
                        'column_key': '',
                        'discount_pct': 0.0,
                        'sb_surcharge': 0.0,
                        'is_winner': False,
                        'note': note,
                    })
                    continue
                candidates.append({
                    'vendor': vendor,
                    'price': price,
                    'method': 'supplierinfo',
                    'column_key': '',
                    'discount_pct': 0.0,
                    'sb_surcharge': 0.0,
                    'is_winner': False,
                    'note': _("Net price from product supplier pricelist."),
                })
                continue

            details = self.baf_get_purchase_price_details(supplier_code=code)
            if not details:
                candidates.append({
                    'vendor': vendor,
                    'price': None,
                    'method': 'discount_table',
                    'column_key': '',
                    'discount_pct': 0.0,
                    'sb_surcharge': 0.0,
                    'is_winner': False,
                    'note': _("Product has no discount-table column for this vendor."),
                })
                continue

            candidates.append({
                'vendor': vendor,
                'price': details['price'],
                'method': details['pricing_method'],
                'column_key': details['column_key'],
                'discount_pct': details['discount_pct'],
                'sb_surcharge': details['sb_surcharge'],
                'is_winner': False,
                'note': '',
            })

        priced = [c for c in candidates if c['price'] is not None]
        if not priced:
            empty_result['candidates'] = candidates
            empty_result['reason'] = _(
                "No vendor produced a usable price (missing discount table rows or supplier pricelist)."
            )
            return empty_result

        pref = {code: idx for idx, code in enumerate(self._BAF_VENDOR_PREFERENCE)}
        priced.sort(key=lambda c: (
            round(c['price'], 4),
            pref.get(c['vendor'].baf_supplier_code, 99),
        ))

        winner = priced[0]
        winner['is_winner'] = True

        cheapest_price = round(winner['price'], 4)
        ties = [c for c in priced if round(c['price'], 4) == cheapest_price]

        winner_code = winner['vendor'].baf_supplier_code
        if is_motorcycle:
            reason = _(
                "Motorcycle product → only Supplier 3 is eligible. "
                "%(vendor)s wins at %(price).2f (column %(col)s)."
            ) % {
                'vendor': winner['vendor'].display_name,
                'price': winner['price'],
                'col': winner['column_key'] or '—',
            }
        elif len(ties) > 1:
            tied_codes = ', '.join(c['vendor'].baf_supplier_code for c in ties)
            reason = _(
                "%(vendor)s (%(code)s) wins at %(price).2f. Tie at this price between %(tied)s → preference order picks %(code)s."
            ) % {
                'vendor': winner['vendor'].display_name,
                'code': winner_code,
                'price': winner['price'],
                'tied': tied_codes,
            }
        elif winner['method'] == 'supplierinfo':
            reason = _(
                "%(vendor)s wins at %(price).2f from the supplier pricelist (EU direct)."
            ) % {
                'vendor': winner['vendor'].display_name,
                'price': winner['price'],
            }
        else:
            reason = _(
                "%(vendor)s (%(code)s) wins at %(price).2f from discount table column %(col)s."
            ) % {
                'vendor': winner['vendor'].display_name,
                'code': winner_code,
                'price': winner['price'],
                'col': winner['column_key'] or '—',
            }

        return {
            'vendor': winner['vendor'],
            'price': winner['price'],
            'method': winner['method'],
            'reason': reason,
            'candidates': candidates,
        }
