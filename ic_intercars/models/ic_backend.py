"""Inter Cars backend — credentials + config + token cache.

Modelled on ``shipping.provider.account`` in ``shipping_custom``: the
model owns the client id / secret and returns an ``InterCarsAPIClient``
from ``get_client()``. Multiple backends are allowed (per company, per
market), but only one is typically active.
"""

import io
import json
import logging
import zipfile
from datetime import timedelta

import requests

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

from .ic_client_api import InterCarsAPIClient

_logger = logging.getLogger(__name__)

# CSV feed lives on a separate host from the REST API. The daily
# ProductInformation file lands overnight; if today isn't there yet
# the fetcher walks back day by day until it hits one.
_CSV_ROOT = 'https://data.webapi.intercars.eu/customer'
_CSV_MAX_FETCH_ATTEMPTS = 7
_CSV_FETCH_TIMEOUT = 120

# The live price endpoint accepts many SKUs per call; 100 is what the
# bulk-create wizard uses and IC has been happy with in practice.
_PRICE_BATCH = 100

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

    # ── CSV fetch (shared by wizard + cron) ─────────────────────────────
    def _baf_fetch_product_info_csv(self):
        """Download today's ProductInformation CSV bytes from IC.

        IC generates the file overnight — if today's is not yet up we
        walk back day by day until one is found (or we've exhausted
        ``_CSV_MAX_FETCH_ATTEMPTS``). ZIPs are unwrapped in-flight.
        Raises ``UserError`` with a per-day error list on total failure
        so both the wizard and the cron surface a useful message.
        """
        self.ensure_one()
        if not self.csv_login or not self.csv_password:
            raise UserError(_(
                "IC CSV credentials are missing on backend '%s' "
                "(fields 'CSV Login' and 'CSV Password')."
            ) % self.name)

        base = f"{_CSV_ROOT}/{self.csv_login}/ProductInformation"
        auth = (self.csv_login, self.csv_password)
        today = fields.Date.context_today(self)
        errors = []
        for delta in range(_CSV_MAX_FETCH_ATTEMPTS):
            day = today - timedelta(days=delta)
            fname = f"ProductInformation_{day.isoformat()}.csv.zip"
            url = f"{base}/{fname}"
            _logger.info("IC CSV: trying %s", url)
            try:
                res = requests.get(url, auth=auth,
                                   timeout=_CSV_FETCH_TIMEOUT)
            except requests.RequestException as exc:
                errors.append(f"{fname}: {exc}")
                continue
            if res.status_code == 200 and res.content:
                _logger.info("IC CSV: fetched %s (%d bytes)",
                             fname, len(res.content))
                return self._baf_unzip_csv_if_needed(res.content, fname)
            errors.append(
                f"{fname}: HTTP {res.status_code} "
                f"({(res.text or '')[:120]})"
            )
        raise UserError(_(
            "Could not fetch a ProductInformation CSV from IC. "
            "Tried the last %(n)d day(s):\n%(errs)s"
        ) % {'n': _CSV_MAX_FETCH_ATTEMPTS,
             'errs': '\n'.join(errors)})

    @staticmethod
    def _baf_unzip_csv_if_needed(raw, filename):
        """Return the .csv bytes from an IC download; raw or unzipped."""
        if raw[:2] == b'PK':  # ZIP magic
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                members = [n for n in zf.namelist()
                           if n.lower().endswith('.csv')]
                if not members:
                    raise UserError(_(
                        "ZIP file %s contains no .csv member."
                    ) % filename)
                return zf.read(members[0])
        return raw

    # ── Daily catalog refresh cron ──────────────────────────────────────
    @api.model
    def _cron_refresh_catalog_csv(self):
        """Fetch today's CSV for every active backend that has creds.

        Each backend is independent: one broken CSV account should
        never block a healthy one. Progress goes to the log; the cron
        never raises so its next scheduled run stays untouched.
        """
        backends = self.sudo().search([
            ('active', '=', True),
            ('csv_login', '!=', False),
            ('csv_password', '!=', False),
        ])
        for backend in backends:
            try:
                csv_bytes = backend._baf_fetch_product_info_csv()
                stats = self.env['ic.product.info'].sudo().bulk_load_csv(
                    csv_bytes, replace=True,
                )
                _logger.info(
                    "IC catalog cron: backend %s — %d rows in %.1fs",
                    backend.name, stats['rows'], stats['seconds'],
                )
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "IC catalog cron: refresh failed for backend %s",
                    backend.name)

    # ── Daily price refresh cron ────────────────────────────────────────
    @api.model
    def _cron_refresh_prices(self):
        """Re-quote and re-price every materialised IC product.

        Per active backend, walks all ``product.product`` records
        supplied by that backend's vendor and refreshes cost + sale
        price in batches of ``_PRICE_BATCH``. SKUs IC declines to
        quote in a given run are left untouched — a transient IC error
        must not wipe a working sale price.
        """
        backends = self.sudo().search([('active', '=', True)])
        for backend in backends:
            try:
                backend._baf_refresh_prices()
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "IC price cron: refresh failed for backend %s",
                    backend.name)

    def _baf_refresh_prices(self):
        """Cron body per backend — separate for testability."""
        self.ensure_one()
        if not self.vendor_id:
            return {'quoted': 0, 'declined': 0, 'batches': 0}
        products = self.env['product.product'].sudo().search([
            ('ic_sku', '!=', False),
            ('part_quality', '=', 'aftermarket'),
            ('active', '=', True),
            ('seller_ids.partner_id', '=', self.vendor_id.id),
        ])
        if not products:
            return {'quoted': 0, 'declined': 0, 'batches': 0}
        client = self.get_client()
        quoted = declined = batches = 0
        for start in range(0, len(products), _PRICE_BATCH):
            chunk = products[start:start + _PRICE_BATCH]
            n_quoted, n_declined = self._baf_refresh_prices_chunk(client, chunk)
            quoted += n_quoted
            declined += n_declined
            batches += 1
            # One commit per batch so a mid-run failure never loses the
            # earlier batches' updates.
            if not tools.config['test_enable']:
                self.env.cr.commit()
        _logger.info(
            "IC price cron: backend %s — %d quoted, %d declined, "
            "%d batch(es)",
            self.name, quoted, declined, batches,
        )
        return {'quoted': quoted, 'declined': declined, 'batches': batches}

    def _baf_refresh_prices_chunk(self, client, products):
        """Quote one chunk of products and write cost + sale price.

        Returns ``(quoted, declined)`` — declined counts are SKUs IC did
        not quote (transient issue, discontinued, credit block). Those
        products keep their existing price.
        """
        self.ensure_one()
        skus = [p.ic_sku for p in products if p.ic_sku]
        if not skus:
            return 0, 0
        try:
            response = client.get_price(
                lines=[{'sku': s, 'quantity': 1} for s in skus],
                ship_to=self.ship_to or None,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "IC price cron: quote failed for chunk of %d — %s",
                len(skus), exc)
            return 0, len(skus)
        price_by_sku = {}
        for line in (response.get('lines') or []):
            price = line.get('price') or {}
            if line.get('sku') and price.get('customerPriceNet') is not None:
                price_by_sku[line['sku']] = float(price['customerPriceNet'])
        Product = self.env['product.product'].sudo()
        quoted = 0
        for product in products:
            price_net = price_by_sku.get(product.ic_sku)
            if price_net is None:
                continue
            product._baf_upsert_ic_supplierinfo(self, price_net)
            new_list = Product._baf_ic_list_price(price_net)
            if new_list and product.product_tmpl_id.list_price != new_list:
                product.product_tmpl_id.write({'list_price': new_list})
            quoted += 1
        return quoted, len(skus) - quoted
