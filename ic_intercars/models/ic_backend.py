"""Inter Cars backend — credentials + config + token cache.

Modelled on ``shipping.provider.account`` in ``shipping_custom``: the
model owns the client id / secret and returns an ``InterCarsAPIClient``
from ``get_client()``. Multiple backends are allowed (per company, per
market), but only one is typically active.
"""

import json

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ic_client_api import InterCarsAPIClient

# ISO 639-1 codes accepted by IC's Accept-Language header (Swagger list).
_IC_LANGUAGES = [
    ('bg', 'Bulgarian'), ('bs', 'Bosnian'), ('cs', 'Czech'),
    ('de', 'German'),    ('el', 'Greek'),   ('en', 'English'),
    ('et', 'Estonian'),  ('hu', 'Hungarian'), ('hr', 'Croatian'),
    ('lt', 'Lithuanian'), ('lv', 'Latvian'), ('pl', 'Polish'),
    ('ro', 'Romanian'), ('sk', 'Slovak'), ('sl', 'Slovenian'),
    ('sr', 'Serbian'), ('uk', 'Ukrainian'),
]


class IcBackend(models.Model):
    _name = 'ic.backend'
    _description = 'Inter Cars API Backend'

    name = fields.Char(string="Name", required=True, default="Inter Cars")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', string="Company",
        default=lambda self: self.env.company,
    )

    # ── IC vendor partner ────────────────────────────────────────────────
    # Every drop-ship PO goes to this partner. Modelled as an ordinary
    # Odoo vendor so it plugs into product.supplierinfo, drop-ship
    # routes, and the BAF vendor-compare wizard without special-casing.
    vendor_id = fields.Many2one(
        'res.partner',
        string="Inter Cars Vendor",
        required=True,
        domain=[('supplier_rank', '>', 0)],
        help="The res.partner record that represents Inter Cars as a "
             "vendor. Purchase orders for IC drop-ship items are placed "
             "against this partner.",
    )

    # ── OAuth2 credentials ───────────────────────────────────────────────
    client_id = fields.Char(string="Client ID", copy=False)
    client_secret = fields.Char(string="Client Secret", copy=False)
    token_url = fields.Char(
        string="Token URL",
        help="OAuth2 token endpoint URL. NOT published in the IC Swagger — "
             "ask Inter Cars for the exact URL for your account.",
    )
    base_url = fields.Char(
        string="Base URL",
        default='https://api.webapi.intercars.eu',
        help="Root of the IC REST API. Do not include a trailing slash.",
    )
    oauth_scope = fields.Char(
        string="OAuth2 Scope",
        default='allinone',
        help="Scope value sent as a query param to the token endpoint. "
             "IC's default is 'allinone'.",
    )
    access_token = fields.Char(
        string="Access Token", copy=False, readonly=True,
        help="Cached bearer token; refreshed automatically on 401.",
    )
    token_expiry = fields.Float(
        string="Token Expiry (epoch)", copy=False, readonly=True,
        help="Unix timestamp at which the cached token stops being valid.",
    )

    # ── Business config ──────────────────────────────────────────────────
    ship_to = fields.Char(
        string="Default shipTo",
        help="IC customer identifier used as ``shipTo`` on ordering and "
             "on searches. Uppercase alphanumeric per IC's schema.",
    )
    delivery_method = fields.Char(
        string="Default Delivery Method",
        help="IC delivery-method code sent on the requisition. LEAVE "
             "EMPTY to let IC apply your account's default (recommended) "
             "— an invalid code fails ordering with ICF209. Note the "
             "code shown as defaultDeliveryMethod on /ic/customer (e.g. "
             "DIST) is a customer-profile value and is NOT necessarily "
             "accepted on requisitions. Use 'Fetch Account Info' to see "
             "your account data, and ask IC which codes are orderable.",
    )
    payment_method = fields.Char(
        string="Default Payment Method",
        help="IC payment-method code sent on the requisition. Optional.",
    )
    default_language = fields.Selection(
        _IC_LANGUAGES, string="Catalog Language", default='de', required=True,
        help="Sent as the ``Accept-Language`` header on catalog calls.",
    )
    currency_id = fields.Many2one(
        'res.currency', string="Currency",
        default=lambda self: self.env.company.currency_id,
    )
    market = fields.Selection([
        ('de', 'Germany (DE)'),
        ('pl', 'Poland (PL)'),
        ('other', 'Other'),
    ], string="Market", default='de',
        help="Controls Polish-only features (deferredPayment, KSeF, GTU).")
    stock_cap = fields.Integer(
        string="Stock Cap", default=10,
        help="Availability cap returned by IC: value X means '≥ X'. "
             "Do not change unless IC changes the cap.",
    )
    vat_rate_pct = fields.Float(
        string="Assumed VAT %",
        default=19.0,
        help="Used when submitting the requisition to compute "
             "``unitPriceGross`` from ``unitPriceNet``. Overridden per PO "
             "line by the Odoo tax if the tax exposes a rate.",
    )

    # ── CSV channel (F04217) — separate bulk feed, kept for future use ──
    csv_login = fields.Char(string="CSV Login")
    csv_password = fields.Char(string="CSV Password")

    # ── Live account snapshot (filled by the Fetch Account Info button) ─
    account_info = fields.Text(
        string="IC Account Info (live snapshot)",
        readonly=True, copy=False,
        help="Raw JSON from GET /ic/customer and /ic/customer/finances. "
             "Shows the payment methods, delivery configuration and "
             "credit standing IC has on file for this account.",
    )
    account_info_date = fields.Datetime(
        string="Snapshot Taken", readonly=True, copy=False,
    )

    # ── API ──────────────────────────────────────────────────────────────
    def get_client(self):
        """Factory: return an InterCarsAPIClient bound to this backend."""
        self.ensure_one()
        return InterCarsAPIClient(self)

    def action_test_connection(self):
        """Called by the 'Test Connection' button on the form view."""
        self.ensure_one()
        return self.get_client().test_connection()

    def action_fetch_account_info(self):
        """Pull /ic/customer + /ic/customer/finances and snapshot them.

        The main operational use: seeing which paymentMethods and
        delivery configuration IC actually has on file — the values a
        requisition must use (ICF209 'DeliveryMethod is not valid'
        means the configured code isn't in this set).
        """
        self.ensure_one()
        client = self.get_client()
        customer = client.get_customer() or {}
        try:
            finances = client.get_finances() or {}
        except UserError:
            # Finances is a separate permission on some accounts — a
            # partial snapshot still beats none.
            finances = {}
        self.write({
            'account_info': json.dumps(
                {'customer': customer, 'finances': finances},
                indent=2, ensure_ascii=False, default=str,
            ),
            'account_info_date': fields.Datetime.now(),
        })
        summary = _(
            "Status: %(status)s — default payment: %(pay)s — default "
            "delivery: %(dlv)s — payment methods: %(methods)s — "
            "ordering allowed: %(ok)s"
        ) % {
            'status': customer.get('status') or '?',
            'pay': customer.get('defaultPaymentMethod') or '?',
            'dlv': customer.get('defaultDeliveryMethod') or '?',
            'methods': ', '.join(
                str(m) for m in (customer.get('paymentMethods') or [])
            ) or '?',
            'ok': finances.get('orderingAllowed', '?'),
        }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Inter Cars Account Info"),
                'message': summary,
                'type': 'success',
                'sticky': True,
            },
        }

    @api.model
    def _get_default(self):
        """Return the first active backend for the current company.

        Callers should treat ``None`` as 'IC is not configured' and skip
        gracefully — never raise from the shop-side code path.
        """
        return self.sudo().search([
            ('active', '=', True),
            ('company_id', 'in', (self.env.company.id, False)),
        ], limit=1)
