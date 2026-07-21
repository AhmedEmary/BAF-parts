import re

from odoo import _, models, fields, api


# Sort weight for a vendor with no delivery frame set: always last. An int (not
# float('inf')) so the vendor-compare wizard can store it in an Integer column
# and sort by the exact same value the ranking here uses.
BAF_NO_DELIVERY_RANK = 9999


def baf_delivery_rank(weeks):
    """Delivery sort weight: real frames rank by week count, unset ranks last."""
    return weeks if weeks and weeks > 0 else BAF_NO_DELIVERY_RANK


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
    # (regex, brand_family) — classifies a brand into a family for the type-split
    # check and the stored baf_brand_family field. The discount column base is
    # the brand's baf.brand.family (see baf_family_base_key): brands merged into
    # one family share one column, keyed by the family name.
    (re.compile(r'\bBMW\b'),                                                'bmw_mini'),
    (re.compile(r'\bMINI\b'),                                               'bmw_mini'),
    (re.compile(r'JAGUAR|ROVER|\bJLR\b|\bJAG\b|\bLR\b|\bRR\b|\bJ\b'),       'jlr'),
    (re.compile(r'MERCEDES|BENZ|\bMB\b'),                                   'mercedes'),
)

# Type buckets in the order their columns appear in a types-and-groups file.
BAF_TYPE_BUCKETS = ('T12', 'T39')

# Families whose column key carries a type bucket (BMW_T12 / BMW_T39). Every
# other family prices a discount code with a single rate (JLR, MERCEDES, ...).
BAF_TYPE_SPLIT_FAMILIES = frozenset({'bmw_mini'})


def baf_brand_base_key(brand_name):
    """Fallback base discount column key from a brand name (its own normalized
    name), used only when a family is not available. Real products key their
    discount column off the family (see baf_family_base_key): brands merged into
    one family share a single column."""
    norm = _normalize_brand(brand_name)
    return norm.replace(' ', '_') if norm else ''


def baf_family_base_key(family):
    """Discount column base shared by every brand in a family: the family's own
    normalized name. 'JLR' -> 'JLR', 'BMW / MINI' -> 'BMW_MINI'. Brands merged
    into one family share this single column, so the discount table holds one
    line per code per family (not per brand)."""
    norm = _normalize_brand(family.name) if family else ''
    return norm.replace(' ', '_') if norm else ''


def baf_brand_family_of(brand_name):
    """Brand family of a brand name; 'other' for brands with no pattern."""
    norm = _normalize_brand(brand_name)
    if not norm:
        return 'other'
    for pattern, family in _BRAND_PATTERNS:
        if pattern.search(norm):
            return family
    return 'other'

def _normalize_brand(name):
    if not name:
        return ''
    norm = re.sub(r'[-_/\\]+', ' ', str(name).upper())
    norm = re.sub(r'\s+', ' ', norm).strip()
    return norm


def resolve_baf_brand_info(brand_name, type_code=0, mod='car', family_base=None):
    """
    Smart brand → (column_key, brand_family) resolver.

    Returns a (column_key, brand_family) tuple. brand_family is one of
    'bmw_mini', 'jlr', 'mercedes', 'other' and is classified from the name.
    The column_key base is the family's shared key when `family_base` is given
    (brands in a family share one column); otherwise it falls back to the
    brand's own name. column_key is '' for a blank brand, `<BASE>_T12` /
    `<BASE>_T39` for BMW/MINI (per type_code), else just `<BASE>`.

    BMW/MINI type-code split:
        T12 → 1, 2, 4, 6, 8
        T39 → 3, 5, 7, 9
    A missing/zero type code falls back to T12.
    """
    if not _normalize_brand(brand_name):
        return ('', 'other')

    base_key = family_base if family_base is not None else baf_brand_base_key(brand_name)
    family = baf_brand_family_of(brand_name)
    if not base_key:
        return ('', family)
    if family in BAF_TYPE_SPLIT_FAMILIES:
        tc = type_code or 0
        bucket = 'T39' if tc in BAF_T39_TYPE_CODES else 'T12'
        return (f'{base_key}_{bucket}', family)
    return (base_key, family)


class ProductTemplateBafPricing(models.Model):
    _inherit = 'product.template'

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

    # ── Computed column key ───────────────────────────────────────────────────
    baf_column_key = fields.Char(
        string='Column Key',
        compute='_compute_baf_column_key',
        store=True,
        help="Brand-based key (e.g. BMW_T12) for PURCHASE discount lookups: "
             "vendors quote per brand. Empty = no table lookup for this product.",
    )

    baf_sales_column_key = fields.Char(
        string='Sales Column Key',
        compute='_compute_baf_column_key',
        store=True,
        help="Family-based key (e.g. BMW_MINI_T12) for SALES discount lookups: "
             "brands merged into one family share a single sales column, so the "
             "sales discount table holds one line per code per family.",
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

    @api.depends('brand', 'brand.name', 'brand.family_id', 'brand.family_id.name',
                 'baf_type_code', 'baf_mod')
    def _compute_baf_column_key(self):
        # Note: motorcycles keep their brand/type key (e.g. BMW_T12) so that
        # sales lookups land on BMW_T12_MOTO / MINI_T39_MOTO. On the purchase
        # side the engine substitutes the plain 'MOTO' column for moto products.
        # Purchase key is always brand-based (vendors quote per brand). Sales key:
        #  - type-split families (BMW/MINI) keep a per-brand column split by type
        #    (BMW_T12, MINI_T39) -- one sales line per (brand, type) per group.
        #  - other families share one column (the family base) -- one sales line
        #    per code covers every brand in the family.
        for rec in self:
            brand = rec.brand
            brand_name = brand.name if brand else ''
            column_key, family = resolve_baf_brand_info(
                brand_name, type_code=rec.baf_type_code, mod=rec.baf_mod,
            )
            if family in BAF_TYPE_SPLIT_FAMILIES:
                sales_key = column_key
            else:
                family_base = baf_family_base_key(brand.family_id) if brand and brand.family_id else None
                sales_key = family_base if family_base is not None else column_key
            rec.baf_column_key = column_key
            rec.baf_sales_column_key = sales_key
            rec.baf_brand_family = family

    # ── Pricing engine entry point ────────────────────────────────────────────

    def baf_get_purchase_price(self, vendor):
        """Net purchase price from `vendor` for this product, or None."""
        details = self.baf_get_purchase_price_details(vendor)
        return details['price'] if details else None

    def baf_get_purchase_price_details(self, vendor):
        """
        Resolve the purchase price for this product from `vendor`, dispatching
        on the vendor's chosen method. Returns a dict or None when the vendor
        cannot price this product.

          {'price', 'column_key', 'discount_pct', 'sb_surcharge', 'pricing_method'}
        """
        self.ensure_one()
        if not vendor:
            return None

        method = vendor.baf_purchase_method
        upe = self.list_price

        if method == 'direct':
            price = self._baf_supplierinfo_price(vendor)
            if price is None:
                return None
            return {
                'price': round(price, 2),
                'column_key': '',
                'discount_pct': 0.0,
                'sb_surcharge': 0.0,
                'pricing_method': 'direct',
            }

        if method == 'codes':
            value = self.env['discount.code.value'].search([
                ('partner_id', '=', vendor.id),
                ('code_id.name', '=', self.baf_discount_code or ''),
            ], limit=1)
            if not value:
                return None
            price = upe * (1.0 - value.percentage / 100.0)
            return {
                'price': round(price, 2),
                'column_key': '',
                'discount_pct': value.percentage,
                'sb_surcharge': 0.0,
                'pricing_method': 'codes',
            }

        if method == 'matrix':
            column_key = 'MOTO' if self.baf_mod == MOD_MOTORCYCLE else self.baf_column_key
            if not column_key:
                return None
            pct = self.env['baf.discount.line'].get_discount_pct(
                table_type='purchase',
                column_key=column_key,
                discount_code=self.baf_discount_code,
                partner=vendor,
            )
            if pct is None:
                return None
            price = upe * (1.0 - pct / 100.0)
            sb = 0.0
            if self.baf_mod == 'sb' and vendor.baf_sb_surcharge_pct:
                sb = vendor.baf_sb_surcharge_pct
                price = price * (1.0 - sb / 100.0)
            return {
                'price': round(price, 2),
                'column_key': column_key,
                'discount_pct': pct,
                'sb_surcharge': sb,
                'pricing_method': 'matrix',
            }

        return None

    def _baf_supplierinfo_price(self, vendor):
        """Net price from product.supplierinfo for this vendor, or None."""
        self.ensure_one()
        seller = self.seller_ids.filtered(lambda s: s.partner_id == vendor)[:1]
        return seller.price if seller else None

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

        # Effective pricing groups: a child company contact with no groups of
        # its own inherits the parent company's groups; a child WITH its own
        # groups uses them and ignores the company's.
        sales_groups = partner._baf_effective_sales_groups()

        # B2B EU VAT tier ─────────────────────────────────────────────────────
        # Registered customers with a VAT number from an EU country get a flat
        # −5 % on JLR products when they don't already have a group covering this
        # product (a matching-family group or a wildcard); pricing groups take
        # priority and usually offer more.
        product_bfam = self.brand.family_id
        if (
            product_family == 'jlr'
            and getattr(partner, 'is_b2b_eu_vat', False)
            and not sales_groups.filtered(
                lambda g: g.active and (not g.family_id or g.family_id == product_bfam)
            )
        ):
            return {
                'price': upe * 0.95 + surcharge,
                'column_key': 'B2B_EU_VAT',
                'discount_pct': 5.0,
                'pricing_method': 'b2b_vat_discount',
            }

        # No pricing groups at all → full UPE
        if not sales_groups:
            return {
                'price': upe + surcharge,
                'column_key': '',
                'discount_pct': 0.0,
                'pricing_method': 'guest',
            }

        # Pick the customer's group covering this product's family. A group
        # scoped to the product's family wins (so a JLR group prices JLR parts
        # and never Mercedes ones); a wildcard group (no family) is the
        # last-resort catch-all. A customer can hold one car group + one moto
        # group per family (e.g. BMW_MINI_GR1 for BMW car parts AND BMW_MINI_MOTO
        # for BMW motorcycle parts). The moto tier is detected from the group's
        # column suffix == 'MOTO'.
        is_moto_product = self.baf_mod == MOD_MOTORCYCLE and self.baf_brand_family == 'bmw_mini'
        groups = sales_groups.filtered(lambda g: g.active)
        family_groups = groups.filtered(lambda g: product_bfam and g.family_id == product_bfam)
        if is_moto_product:
            group = (
                family_groups.filtered(lambda g: g._is_moto_group())[:1]
                or family_groups.filtered(lambda g: not g._is_moto_group())[:1]
                or groups.filtered(lambda g: not g.family_id)[:1]
            )
        else:
            group = (
                family_groups.filtered(lambda g: not g._is_moto_group())[:1]
                or groups.filtered(lambda g: not g.family_id)[:1]
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

        # table_lookup — family-based sales key (BMW_MINI_T12, JLR, MERCEDES, …)
        # Motorcycle BMW/MINI parts: override the group suffix with MOTO so the
        # lookup lands on <FAMILY>_T12_MOTO regardless of the customer's
        # car-side group (GR1..GR4).
        column_key = self.baf_sales_column_key
        if self.baf_mod == MOD_MOTORCYCLE and self.baf_brand_family == 'bmw_mini':
            suffix = 'MOTO'
        else:
            suffix = group.group_column_suffix or 'GR1'
        full_column_key = f"{column_key}_{suffix}" if column_key else ''

        discount_pct = (self.env['baf.discount.line'].get_discount_pct(
            table_type='sales',
            column_key=full_column_key,
            discount_code=self.baf_discount_code,
        ) or 0.0) if full_column_key else 0.0
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

    # ── Alternative direct vendors (portal) ───────────────────────────────────

    def _baf_alternative_direct_vendors(self, default_price):
        """Alternative direct-vendor options for the portal, ordered by delivery
        time ascending. Qualifying vendors use the 'direct' method and have a
        delivery time frame (> 0) and a sales markup (> 0) set; their sales
        price = direct price * (1 + markup%).

        Selection: walk the delivery frames from fastest to slowest, keeping a
        running best price that starts at `default_price`. In each frame take
        the cheapest vendor, but keep it only if it is STRICTLY cheaper than the
        best price kept so far (then it becomes the new best). Frames whose
        cheapest isn't an improvement are dropped entirely.

        Returns dicts (delivery-ascending):
        {vendor_id, price, delivery_lower, delivery_label}."""
        self.ensure_one()

        by_weeks = {}  # weeks -> list of (price, vendor_id)
        for vendor in self.seller_ids.mapped('partner_id'):
            # Same eligibility + price as the sale-order line will charge, so
            # what we advertise here can never differ from what we bill.
            price = self._baf_alt_vendor_unit_price(vendor)
            if price is None:
                continue
            by_weeks.setdefault(vendor.baf_delivery_weeks, []).append((price, vendor.id))

        options = []
        best_price = default_price
        for weeks in sorted(by_weeks):
            # cheapest in this frame (tie-break by lowest vendor id)
            price, vendor_id = min(by_weeks[weeks])
            if price < best_price:
                options.append({
                    'vendor_id': vendor_id,
                    'price': price,
                    'delivery_lower': weeks,
                    'delivery_label': '%d-%d weeks' % (weeks, weeks + 1),
                })
                best_price = price
        return options

    def _baf_alternative_vendor_display(self, partner=None):
        """Product-page data: the product's default sales price for `partner`
        plus the alternative direct-vendor options (price + delivery frame)."""
        self.ensure_one()
        default_price = self.baf_get_sales_price(partner=partner)
        return {
            'default_price': default_price,
            'options': self._baf_alternative_direct_vendors(default_price),
        }

    def _baf_alt_vendor_unit_price(self, vendor):
        """Sales unit price for `vendor` via direct + markup, or None when the
        vendor doesn't qualify / doesn't price this product."""
        self.ensure_one()
        if not vendor or vendor.baf_purchase_method != 'direct':
            return None
        brand = self.brand
        if brand and brand not in vendor.baf_brand_ids:
            return None
        weeks = vendor.baf_delivery_weeks or 0
        markup = vendor.baf_direct_sale_markup_pct or 0.0
        if weeks <= 0 or markup <= 0:
            return None
        seller = self.seller_ids.filtered(lambda s: s.partner_id == vendor)[:1]
        if not seller or not seller.price:
            return None
        return round(seller.price * (1.0 + markup / 100.0), 2)


class ProductProductBafPricing(models.Model):
    _inherit = 'product.product'

    def _baf_alternative_direct_vendors(self, default_price):
        self.ensure_one()
        return self.product_tmpl_id._baf_alternative_direct_vendors(default_price)

    def _baf_alt_vendor_unit_price(self, vendor):
        self.ensure_one()
        return self.product_tmpl_id._baf_alt_vendor_unit_price(vendor)

    def baf_get_purchase_price(self, vendor):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_purchase_price(vendor)

    def baf_get_purchase_price_details(self, vendor):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_purchase_price_details(vendor)

    def baf_get_sales_price(self, partner=None):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_sales_price(partner=partner)

    def baf_get_sales_price_details(self, partner=None):
        self.ensure_one()
        return self.product_tmpl_id.baf_get_sales_price_details(partner=partner)

    # ── Auto vendor selection ─────────────────────────────────────────────────

    def _baf_eligible_vendors(self):
        """Vendors that (a) have a purchase pricing method set and
        (b) list this product's brand in Brands Supplied."""
        self.ensure_one()
        brand = self.brand
        if not brand:
            return self.env['res.partner']
        return self.env['res.partner'].search([
            ('baf_purchase_method', '!=', False),
            ('baf_brand_ids', 'in', brand.id),
        ])

    def baf_get_best_vendor(self):
        """
        Auto-select the best eligible vendor for this product. Ranking is:
        shortest delivery period first, then lowest price, then vendor id
        (ascending). A vendor with no delivery period set (0/empty) is ranked
        last (slowest). Returns {vendor, price, method, reason, candidates}. A
        vendor that cannot price the product is listed but excluded from ranking.
        """
        self.ensure_one()
        empty = {'vendor': self.env['res.partner'], 'price': 0.0,
                 'method': None, 'reason': '', 'candidates': []}

        eligible = self._baf_eligible_vendors()
        if not eligible:
            empty['reason'] = _(
                "No vendor lists '%(brand)s' with a purchase pricing method."
            ) % {'brand': self.brand.name if self.brand else ''}
            return empty

        candidates = []
        for vendor in eligible:
            details = self.baf_get_purchase_price_details(vendor)
            candidates.append({
                'vendor': vendor,
                'price': details['price'] if details else None,
                'method': details['pricing_method'] if details else vendor.baf_purchase_method,
                'column_key': details['column_key'] if details else '',
                'discount_pct': details['discount_pct'] if details else 0.0,
                'sb_surcharge': details['sb_surcharge'] if details else 0.0,
                'delivery_weeks': vendor.baf_delivery_weeks or 0,
                'is_winner': False,
                'note': '' if details else _("Vendor cannot price this product."),
            })

        priced = [c for c in candidates if c['price'] is not None]
        if not priced:
            empty['candidates'] = candidates
            empty['reason'] = _("No eligible vendor produced a usable price.")
            return empty

        # Rank: shortest delivery first, then cheapest, then lowest id.
        # A vendor with no delivery period (0/empty) sorts last (slowest).
        priced.sort(key=lambda c: (baf_delivery_rank(c['delivery_weeks']),
                                   round(c['price'], 4), c['vendor'].id))
        winner = priced[0]
        winner['is_winner'] = True

        win_delivery = baf_delivery_rank(winner['delivery_weeks'])
        cheapest = round(winner['price'], 4)
        ties = [
            c for c in priced
            if baf_delivery_rank(c['delivery_weeks']) == win_delivery
            and round(c['price'], 4) == cheapest
        ]
        delivery_txt = (
            _("%(a)d-%(b)d weeks") % {'a': winner['delivery_weeks'],
                                      'b': winner['delivery_weeks'] + 1}
            if win_delivery != BAF_NO_DELIVERY_RANK else _("no delivery period")
        )
        if len(ties) > 1:
            reason = _(
                "%(vendor)s wins (%(delivery)s, %(price).2f; tie with %(n)d others, lowest id chosen)."
            ) % {'vendor': winner['vendor'].display_name, 'delivery': delivery_txt,
                 'price': winner['price'], 'n': len(ties) - 1}
        else:
            reason = _(
                "%(vendor)s wins: %(delivery)s delivery, %(price).2f via %(method)s%(col)s."
            ) % {'vendor': winner['vendor'].display_name, 'delivery': delivery_txt,
                 'price': winner['price'], 'method': winner['method'],
                 'col': (_(" (column %s)") % winner['column_key']) if winner['column_key'] else ''}

        return {'vendor': winner['vendor'], 'price': winner['price'],
                'method': winner['method'], 'reason': reason,
                'candidates': candidates}
