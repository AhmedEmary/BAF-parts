from odoo import models, fields, api


class BafSalesGroup(models.Model):
    """
    Defines a customer pricing group.
    Each res.partner gets one sales_group_id.
    The group controls HOW the sales price is computed:
      - table_lookup : look up discount% from baf.discount.line using the
                       product's discount_code + resolved column_key
      - markup_pct   : apply a fixed markup% on top of the purchase price
                       (used for LR, Mercedes, EU-supplier brands, etc.)
    """
    _name = 'baf.sales.group'
    _description = 'BAF Sales Pricing Group'

    name = fields.Char(string='Group Name', required=True)

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

    partner_ids = fields.One2many(
        'res.partner',
        'sales_group_id',
        string='Customers in this group',
    )


class BafDiscountLine(models.Model):
    """
    Single source of truth for all discount lookup tables.
    Every row in every Excel sheet (purchase + sales) becomes one record here.

    Lookup key:  (table_type, column_key, discount_code)
    Result:      discount_pct

    Examples:
      table_type='purchase', column_key='SUP1_BMW_T12', discount_code=10  → 6.0
      table_type='sales',    column_key='BMW_T12_GR1',  discount_code=10  → 2.0
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

    discount_code = fields.Integer(
        string='Discount Code #',
        required=True,
        index=True,
        help="The integer discount code stored on the product (0–60).",
    )

    discount_pct = fields.Float(
        string='Discount %',
        digits=(6, 4),
        help="Effective discount percentage for this column + code combination. "
             "Edit directly here — no code change required.",
    )

    @api.model
    def get_discount_pct(self, table_type, column_key, discount_code):
        """
        Convenience lookup used by the pricing engine.
        Returns the discount % (float) or 0.0 if not found.
        """
        record = self.search([
            ('table_type',    '=', table_type),
            ('column_key',    '=', column_key),
            ('discount_code', '=', discount_code),
        ], limit=1)
        return record.discount_pct if record else 0.0
