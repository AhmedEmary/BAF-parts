from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class BafSalesGroup(models.Model):
    """
    Defines a customer pricing group.
    Each res.partner can belong to at most one sales_group per brand family.
    The group controls HOW the sales price is computed:
      - table_lookup : look up discount% from baf.discount.line using the
                       product's discount_code + resolved column_key
      - markup_pct   : apply a fixed markup% on top of the purchase price
                       (used for LR, Mercedes, EU-supplier brands, etc.)
    """
    _name = 'baf.sales.group'
    _description = 'BAF Sales Pricing Group'

    name = fields.Char(string='Group Name', required=True)

    # The brand family this group prices. The discount import sets it from the
    # family chosen on the wizard. Empty = wildcard: the group prices every
    # product as the last-resort fallback (e.g. an EU-direct markup group).
    family_id = fields.Many2one(
        'baf.brand.family',
        string='Brand Family',
        ondelete='restrict',
        index=True,
        help="Products whose brand belongs to this family are priced by this "
             "group. Leave empty to make it a fallback that prices every brand.",
    )

    pricing_method = fields.Selection(
        selection=[
            ('table_lookup', 'Discount Table Lookup'),
            ('markup_pct',   'Markup Percentage'),
        ],
        string='Pricing Method',
        required=True,
        default='markup_pct',
        help=(
            "table_lookup: use the discount table (BMW/MINI German supplier). "
            "markup_pct: apply a fixed % on top of the purchase price (LR, MB, EU suppliers)."
        ),
    )

    markup_pct = fields.Float(
        string='Markup %',
        default=0.0,
        help="Used when pricing_method = markup_pct. "
             "Sales price = purchase_price × (1 + markup_pct / 100). "
             "Adjustable at any time without touching products.",
    )

    # Column-key suffixes per brand/type combination.
    # When pricing_method = table_lookup, the system builds:
    #   column_key = product.baf_column_key + '_' + group_column_suffix
    # e.g.  BMW_T12_GR1
    group_column_suffix = fields.Char(
        string='Table Column Suffix',
        help="Appended to the product's column key to find the right discount "
             "table column. E.g. 'GR1', 'GR2', 'MOTO'. Leave empty for markup method.",
    )

    active = fields.Boolean(default=True)

    partner_ids = fields.Many2many(
        'res.partner',
        'baf_sales_group_partner_rel',
        'group_id',
        'partner_id',
        string='Customers in this group',
    )

    discount_line_ids = fields.One2many(
        'baf.discount.line',
        'group_id',
        string='Discount Codes',
        help="The discount codes and percentages that make up this group. "
             "Deleting the group deletes these lines.",
    )

    def _is_moto_group(self):
        """A group is the 'moto tier' of its brand family when its column
        suffix is MOTO (e.g. BMW_MINI_MOTO). Detected from the existing
        suffix convention so no extra field is needed."""
        self.ensure_one()
        return (self.group_column_suffix or '').strip().upper() == 'MOTO'

    def _baf_prices_same_family(self, other):
        """Two groups compete when they price the same family: same family, or
        both wildcards (no family). A wildcard and a family-scoped group don't
        compete — the scoped one wins for its family. Odoo compares two empty
        Many2ones as equal, so this single test covers both cases."""
        self.ensure_one()
        return self.family_id == other.family_id

    def _baf_scope_label(self):
        self.ensure_one()
        return self.family_id.name if self.family_id else _("all brands")

    @api.constrains('partner_ids', 'family_id', 'group_column_suffix')
    def _check_partner_ids_unique_family(self):
        for group in self:
            tier = group._is_moto_group()
            conflicting_partners = []
            for partner in group.partner_ids:
                same_tier_groups = partner.sales_group_ids.filtered(
                    lambda g: g._is_moto_group() == tier
                              and g._baf_prices_same_family(group)
                )
                if len(same_tier_groups) > 1:
                    conflicting_partners.append((partner, same_tier_groups))

            if conflicting_partners:
                detail_tpl = _("%(partner)s -> %(groups)s")
                details = '; '.join(
                    detail_tpl % {
                        'partner': partner.display_name,
                        'groups': ', '.join(groups.mapped('name')),
                    }
                    for partner, groups in conflicting_partners
                )
                tier_label = _("motorcycle") if tier else _("car")
                raise ValidationError(_(
                    "A customer can only belong to one %(tier)s pricing group for %(scope)s. "
                    "Conflicts found: %(details)s"
                ) % {
                    'tier': tier_label,
                    'scope': group._baf_scope_label(),
                    'details': details,
                })


class BafDiscountLine(models.Model):
    """
    Single source of truth for all discount lookup tables.
    Every row in every Excel sheet (purchase + sales) becomes one record here.

    Lookup key:  (table_type, column_key, discount_code)
    Result:      discount_pct

    Examples:
      table_type='purchase', column_key='SUP1_BMW_T12',     discount_code='10'  → 6.0
      table_type='sales',    column_key='BMW_T12_GR1',      discount_code='10'  → 2.0
      table_type='sales',    column_key='JLR_GR1',          discount_code='1A'  → 27.0
      table_type='purchase', column_key='MERCEDES_PURCHASE',discount_code='M03' → 1.0
    """
    _name = 'baf.discount.line'
    _description = 'BAF Discount Table Line'
    _rec_name = 'column_key'

    table_type = fields.Selection(
        selection=[
            ('purchase', 'Purchase'),
            ('sales',    'Sales'),
        ],
        string='Table Type',
        required=True,
        index=True,
    )

    partner_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        index=True,
        ondelete='cascade',
        help="Vendor this purchase row belongs to. Empty for global sales rows.",
    )

    group_id = fields.Many2one(
        'baf.sales.group',
        string='Sales Group',
        index=True,
        ondelete='cascade',
        help="Sales group this row belongs to. Empty for purchase rows. "
             "Deleting the group deletes its lines.",
    )

    column_key = fields.Char(
        string='Column Key',
        required=True,
        index=True,
        help=(
            "Identifies the specific discount column. "
            "Purchase examples: SUP1_BMW_T12, SUP2_MINI_T39, SUP3_MOTO. "
            "Sales examples: BMW_T12_GR1, MINI_T39_GR2, MOTO_GR1."
        ),
    )

    discount_code = fields.Char(
        string='Discount Code',
        required=True,
        index=True,
        help="Discount code as stored on the product. Numeric for BMW/MINI "
             "(e.g. '0'..'60'), alphanumeric for JLR (e.g. '1A', '2D'), or "
             "alphanumeric for Mercedes (e.g. 'M03').",
    )

    discount_pct = fields.Float(
        string='Discount %',
        digits=(6, 4),
        help="Effective discount percentage for this column + code combination. "
             "Edit directly here — no code change required.",
    )

    @api.model
    def get_discount_pct(self, table_type, column_key, discount_code, partner=None):
        """
        Lookup used by the pricing engine.
        Returns the discount % (float), or None when no matching row exists
        (so 'vendor cannot price this part' is distinct from a genuine 0% row).
        Purchase lookups pass `partner`; sales lookups leave it None (global rows).
        """
        record = self.search([
            ('table_type',    '=', table_type),
            ('column_key',    '=', column_key),
            ('discount_code', '=', str(discount_code).strip()),
            ('partner_id',    '=', partner.id if partner else False),
        ], limit=1)
        return record.discount_pct if record else None
