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

    # Which product brand-family this group prices. A customer typically
    # belongs to ONE group per family (BMW/MINI tier, JLR tier, Mercedes
    # tier). 'all' is a fallback wildcard used when only the markup method
    # applies (e.g. EU direct).
    brand_family = fields.Selection(
        selection=[
            ('bmw_mini', 'BMW / MINI'),
            ('jlr',      'Jaguar / Land Rover / Range Rover'),
            ('mercedes', 'Mercedes'),
            ('other',    'Other / Unknown'),
            ('all',      'All Brands (fallback)'),
        ],
        string='Brand Family',
        required=True,
        default='all',
        help="Restricts this group to products of the matching brand family. "
             "Customers can hold one group per family (e.g. JLR_GR1 for "
             "Jaguar parts and BMW_MINI_GR2 for BMW parts).",
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

    def _is_moto_group(self):
        """A group is the 'moto tier' of its brand family when its column
        suffix is MOTO (e.g. BMW_MINI_MOTO). Detected from the existing
        suffix convention so no extra field is needed."""
        self.ensure_one()
        return (self.group_column_suffix or '').strip().upper() == 'MOTO'

    @api.constrains('partner_ids', 'brand_family', 'group_column_suffix')
    def _check_partner_ids_unique_family(self):
        family_labels = dict(self._fields['brand_family'].selection)
        for group in self:
            tier = group._is_moto_group()
            conflicting_partners = []
            for partner in group.partner_ids:
                same_tier_groups = partner.sales_group_ids.filtered(
                    lambda g: g.brand_family == group.brand_family
                              and g._is_moto_group() == tier
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
                    "A customer can only belong to one %(tier)s pricing group in the %(family)s family. "
                    "Conflicts found: %(details)s"
                ) % {
                    'tier': tier_label,
                    'family': family_labels.get(group.brand_family, group.brand_family),
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
