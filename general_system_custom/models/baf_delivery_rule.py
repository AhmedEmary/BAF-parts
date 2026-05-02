"""
BAF Delivery Rules
==================
Configurable shipping cost matrix: country zone × package type → cost.

Zones are derived from the delivery country code.  Admins configure the
rules in  Settings → BAF Pricing → Delivery Rules  (or via the menu that is
added by this module).

Usage at checkout
-----------------
    cost = self.env['baf.delivery.rule'].compute_shipping_cost(
        order_amount=order.amount_untaxed,
        total_weight_kg=total_kg,
        country_code=order.partner_shipping_id.country_id.code,
        has_bulky=any(line.product_id.is_bulky_goods for line in order.order_line),
    )
"""

from odoo import models, fields, api

# ── Country-zone mapping ──────────────────────────────────────────────────────
# Based on typical EU shipping zones for a German-based auto-parts supplier.
# Admins can change the zone per individual delivery rule; the zone is only
# auto-detected from the country code for the compute_shipping_cost helper.

_DE_CODES = frozenset({'DE'})

_EU_ZONE_1_CODES = frozenset({
    'AT', 'NL', 'BE', 'LU', 'PL', 'CZ', 'SK', 'DK',
})

_EU_ZONE_2_CODES = frozenset({
    'FR', 'IT', 'ES', 'PT', 'SE', 'FI', 'HU', 'RO',
    'BG', 'SI', 'HR', 'EE', 'LV', 'LT', 'GR',
})

_EU_ZONE_3_CODES = frozenset({
    # Remaining EU + closely associated states
    'IE', 'CY', 'MT', 'GB', 'CH', 'NO', 'IS', 'LI',
})


class BafDeliveryRule(models.Model):
    _name = 'baf.delivery.rule'
    _description = 'BAF Shipping / Delivery Rule'
    _order = 'country_zone, package_type'
    _rec_name = 'name'

    name = fields.Char(
        string='Rule Name',
        required=True,
        help="Human-readable label, e.g. 'Germany – Standard' or 'EU Zone 2 – Bulky'.",
    )

    country_zone = fields.Selection(
        selection=[
            ('de',    'Germany (DE)'),
            ('eu_1',  'EU Zone 1  (AT, NL, BE, LU, PL, CZ, SK, DK)'),
            ('eu_2',  'EU Zone 2  (FR, IT, ES, PT, SE, FI, HU, RO, …)'),
            ('eu_3',  'EU Zone 3  (IE, CY, MT, GB, CH, NO, …)'),
            ('world', 'World (non-EU / unclassified)'),
        ],
        string='Country Zone',
        required=True,
        index=True,
    )

    package_type = fields.Selection(
        selection=[
            ('standard', 'Standard Package'),
            ('bulky',    'Bulky Goods'),
        ],
        string='Package Type',
        required=True,
        index=True,
        help=(
            "Standard: normal parcel. "
            "Bulky: product volume ≥ threshold (default 45 000 cm³) or "
            "product has 'Force Bulky Goods' checked."
        ),
    )

    free_above = fields.Monetary(
        string='Free Shipping Above (€)',
        currency_field='currency_id',
        default=0.0,
        help="When the order's untaxed amount reaches this value, shipping is free. "
             "Set to 0 to never auto-waive shipping for this rule.",
    )

    base_cost = fields.Monetary(
        string='Base Cost (€)',
        currency_field='currency_id',
        default=0.0,
        help="Fixed shipping charge for this zone / package combination.",
    )

    cost_per_kg = fields.Monetary(
        string='Extra Cost per kg (€)',
        currency_field='currency_id',
        default=0.0,
        help="Additional cost per kilogram of total order weight. "
             "Typical use case: bulky goods surcharge by weight.",
    )

    active = fields.Boolean(default=True)

    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
    )

    # ── Zone helper ───────────────────────────────────────────────────────────

    @api.model
    def get_zone_for_country(self, country_code):
        """
        Return the zone key ('de', 'eu_1', 'eu_2', 'eu_3', 'world') for a
        given ISO 3166-1 alpha-2 country code.
        """
        code = (country_code or '').upper().strip()
        if code in _DE_CODES:
            return 'de'
        if code in _EU_ZONE_1_CODES:
            return 'eu_1'
        if code in _EU_ZONE_2_CODES:
            return 'eu_2'
        if code in _EU_ZONE_3_CODES:
            return 'eu_3'

        # Remaining EU member states not explicitly listed above fall into
        # zone 3 (catch-all European zone).
        eu_ref = self.env.ref('base.europe', raise_if_not_found=False)
        if eu_ref and code:
            eu_codes = set(eu_ref.country_ids.mapped('code'))
            if code in eu_codes:
                return 'eu_3'

        return 'world'

    # ── Cost computation ──────────────────────────────────────────────────────

    @api.model
    def compute_shipping_cost(self, order_amount, total_weight_kg, country_code, has_bulky):
        """
        Compute the shipping cost for an order.

        Parameters
        ----------
        order_amount    : float  – untaxed order total (EUR)
        total_weight_kg : float  – sum of (product.weight × qty) for all lines
        country_code    : str    – ISO 3166-1 alpha-2 delivery country code
        has_bulky       : bool   – True if any ordered product is classified as bulky

        Returns
        -------
        float: shipping cost in the company currency (0.0 = free shipping)
        """
        zone = self.get_zone_for_country(country_code)
        package_type = 'bulky' if has_bulky else 'standard'

        rule = self.search([
            ('country_zone', '=', zone),
            ('package_type', '=', package_type),
            ('active', '=', True),
        ], order='id asc', limit=1)

        if not rule:
            return 0.0

        # Free-shipping threshold
        if rule.free_above and order_amount >= rule.free_above:
            return 0.0

        cost = rule.base_cost
        if rule.cost_per_kg and total_weight_kg:
            cost += total_weight_kg * rule.cost_per_kg

        return max(0.0, cost)

