"""Inter Cars REST API client.

Mirrors the FedEx client shape in ``shipping_custom``: a plain Python class
constructed from a backend record, an OAuth2 ``client_credentials`` auth,
a small ``_request`` wrapper that retries once on 401, and one public
method per Inter Cars endpoint. Nothing here talks to the ORM outside of
reading credentials from the backend record.

Reference (contract given by Inter Cars):
  https://api.webapi.intercars.eu   — base URL (config-overridable)
  The OAuth2 token endpoint is NOT in the Swagger; it must be provided
  as ``backend.token_url`` and configured per BAF's IC contract.
"""

import json
import logging
import time

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ── Endpoint constants (single source of truth for paths) ────────────────
_EP_INVENTORY_STOCK    = '/ic/inventory/stock'
_EP_INVENTORY_QUOTE    = '/ic/inventory/quote'
_EP_CATALOG_CATEGORY   = '/ic/catalog/category'
_EP_CATALOG_PRODUCTS   = '/ic/catalog/products'
_EP_PRICING_QUOTE      = '/ic/pricing/quote'
_EP_SALES_REQUISITION  = '/ic/sales/requisition'
_EP_SALES_ORDER        = '/ic/sales/order'
_EP_INVOICE            = '/ic/invoice'
_EP_DELIVERY           = '/ic/delivery'
_EP_DELIVERY_METADATA  = '/ic/delivery/metadata'
_EP_CUSTOMER           = '/ic/customer'
_EP_CUSTOMER_FINANCES  = '/ic/customer/finances'

_REQ_TIMEOUT = 20


class InterCarsAPIClient:
    """Stateless-ish Inter Cars client wrapping the backend record.

    Credentials, token cache and language default are read from the
    ``ic.backend`` record passed in. Token refresh is written back to
    the backend so all workers reuse the same token.
    """

    def __init__(self, backend):
        self.backend = backend
        self.client_id = backend.client_id
        self.client_secret = backend.client_secret
        self.token_url = backend.token_url
        self.base_url = (backend.base_url or '').rstrip('/') or \
            'https://api.webapi.intercars.eu'
        self.token = backend.access_token or ''
        self.default_language = backend.default_language or 'de'

    # ── AUTH ─────────────────────────────────────────────────────────────
    def _auth(self):
        """OAuth2 client-credentials grant.

        The token endpoint URL is not published in the Swagger; if it
        has not been set on the backend, fail loudly rather than guess.

        IC's server requires HTTP Basic auth with client_id as the
        username and client_secret as the password, and the ``grant_type``
        + ``scope`` values as *query* params on the token URL (per IC's
        Postman collection). The body is empty. RFC 6749 §2.3.1 permits
        both Basic and credentials-in-body; IC only accepts the former.
        """
        if not self.token_url:
            raise UserError(_(
                "Inter Cars token URL is not configured. Ask Inter Cars "
                "for the OAuth2 token endpoint and set it on the IC "
                "backend record (field 'Token URL')."
            ))
        if not self.client_id or not self.client_secret:
            raise UserError(_(
                "Inter Cars client_id / client_secret are missing on "
                "the IC backend record."
            ))
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        params = {
            'grant_type': 'client_credentials',
            'scope': self.backend.oauth_scope or 'allinone',
        }
        try:
            res = requests.post(
                self.token_url,
                params=params,
                headers=headers,
                auth=(self.client_id, self.client_secret),
                timeout=_REQ_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise UserError(_("Inter Cars auth request failed: %s") % exc)

        if not res.ok:
            raise UserError(
                _("Inter Cars authentication failed (%s): %s")
                % (res.status_code, (res.text or '')[:500])
            )

        data = res.json() if res.content else {}
        token = data.get('access_token')
        if not token:
            raise UserError(_(
                "Inter Cars auth response is missing 'access_token': %s"
            ) % (data or res.text)[:500])
        self.token = token
        expires_in = int(data.get('expires_in') or 0)
        # Persist on the backend so the token is shared across workers.
        vals = {'access_token': token}
        if expires_in:
            # Store expiry as a unix epoch (int) — the backend field is a
            # Float to avoid TZ complications.
            vals['token_expiry'] = time.time() + expires_in - 30
        self.backend.sudo().write(vals)
        return token

    def test_connection(self):
        """Called by the 'Test Connection' button on the backend form."""
        self._auth()
        # A cheap authenticated call — /ic/customer only needs the token.
        res = self._request('GET', _EP_CUSTOMER)
        if res.status_code in (200, 201):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _(
                        "Inter Cars authentication OK. Customer endpoint "
                        "returned HTTP %s."
                    ) % res.status_code,
                    'type': 'success',
                },
            }
        raise UserError(
            _("Inter Cars test call failed (%s): %s")
            % (res.status_code, (res.text or '')[:500])
        )

    # ── LOW-LEVEL REQUEST ────────────────────────────────────────────────
    def _request(self, method, endpoint, params=None, payload=None,
                 accept_language=None):
        if not self.token:
            self._auth()
        url = f"{self.base_url}{endpoint}"
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/json',
        }
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        if accept_language:
            headers['Accept-Language'] = accept_language

        try:
            res = requests.request(
                method, url, params=params, json=payload, headers=headers,
                timeout=_REQ_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise UserError(
                _("Inter Cars request %s %s failed: %s")
                % (method, endpoint, exc)
            )

        if res.status_code == 401:
            # Token expired — refresh once and retry.
            self._auth()
            headers['Authorization'] = f'Bearer {self.token}'
            try:
                res = requests.request(
                    method, url, params=params, json=payload,
                    headers=headers, timeout=_REQ_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise UserError(
                    _("Inter Cars retried request %s %s failed: %s")
                    % (method, endpoint, exc)
                )

        # Special hint: a 400 on /ic/sales/requisition almost always means
        # overdue payments — surface that to the caller before it treats
        # the error as a technical fault.
        if (
            res.status_code == 400
            and endpoint.startswith(_EP_SALES_REQUISITION)
            and method == 'POST'
        ):
            body = (res.text or '')[:400]
            _logger.warning(
                "Inter Cars requisition returned 400 — likely overdue "
                "payments. Check /ic/customer/finances 'orderingAllowed'. "
                "Body: %s", body,
            )

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "IC %s %s → HTTP %s\n  params=%s\n  payload=%s\n  body=%s",
                method, endpoint, res.status_code, params,
                json.dumps(payload, separators=(',', ':'))[:1000]
                if payload else None,
                (res.text or '')[:1000],
            )
        return res

    def _get_json(self, res, endpoint):
        if res.status_code not in (200, 201):
            raise UserError(
                _("Inter Cars %s error (%s): %s")
                % (endpoint, res.status_code, (res.text or '')[:500])
            )
        try:
            return res.json()
        except ValueError:
            raise UserError(
                _("Inter Cars %s returned non-JSON: %s")
                % (endpoint, (res.text or '')[:300])
            )

    # ── INVENTORY ────────────────────────────────────────────────────────
    def get_stock(self, skus=None, indexes=None, location=None, ship_to=None):
        """GET /ic/inventory/stock — availability by SKU or index.

        ``availability`` in the response is capped at 10 (means ">= 10").
        """
        if not skus and not indexes:
            raise UserError(_("get_stock requires at least sku or index."))
        params = {}
        if skus:
            params['sku'] = ','.join(skus[:100])
        if indexes:
            params['index'] = ','.join(indexes[:100])
        if location:
            params['location'] = location
        if ship_to:
            params['shipTo'] = ship_to
        res = self._request('GET', _EP_INVENTORY_STOCK, params=params)
        return self._get_json(res, _EP_INVENTORY_STOCK)

    def inventory_quote(self, lines):
        """POST /ic/inventory/quote — combined price + per-location stock."""
        res = self._request(
            'POST', _EP_INVENTORY_QUOTE,
            payload={'lines': lines},
        )
        return self._get_json(res, _EP_INVENTORY_QUOTE)

    # ── CATALOG ──────────────────────────────────────────────────────────
    def get_categories(self, category_id=None, language=None):
        """GET /ic/catalog/category — categories tree."""
        params = {}
        if category_id:
            params['categoryId'] = category_id
        res = self._request(
            'GET', _EP_CATALOG_CATEGORY, params=params,
            accept_language=language or self.default_language,
        )
        return self._get_json(res, _EP_CATALOG_CATEGORY)

    def get_catalog_products(self, category_id=None, sku=None, index=None,
                             brands=None, page_number=0, page_size=25,
                             language=None):
        """GET /ic/catalog/products — the primary catalog search.

        One of ``category_id``, ``sku`` or ``index`` is required.
        ``sku`` and ``index`` are single-valued and mutually exclusive.
        Response includes ``genericArticleReferences[]`` — the shared
        ``genericArticleId`` between products is the aftermarket-
        equivalence key.
        """
        if not any([category_id, sku, index]):
            raise UserError(_(
                "get_catalog_products requires one of category_id, sku, or index."
            ))
        if sku and index:
            raise UserError(_("sku and index are mutually exclusive."))
        params = {
            'pageNumber': max(0, min(page_number, 1000)),
            'pageSize':   max(1, min(page_size, 100)),
        }
        if category_id:
            params['categoryId'] = category_id
        if sku:
            params['sku'] = sku
        if index:
            params['index'] = index
        if brands:
            params['brands'] = brands if isinstance(brands, str) else ','.join(brands)
        res = self._request(
            'GET', _EP_CATALOG_PRODUCTS, params=params,
            accept_language=language or self.default_language,
        )
        return self._get_json(res, _EP_CATALOG_PRODUCTS)

    # ── PRICING ──────────────────────────────────────────────────────────
    def get_price(self, lines, ship_to=None, locations=None):
        """POST /ic/pricing/quote — customer-specific prices.

        ``customerPriceNet`` in the response is BAF's cost from IC.
        """
        payload = {'lines': lines[:100]}
        if ship_to:
            payload['shipTo'] = ship_to
        if locations:
            payload['location'] = locations
        res = self._request('POST', _EP_PRICING_QUOTE, payload=payload)
        return self._get_json(res, _EP_PRICING_QUOTE)

    # ── SALES (two-phase ordering) ───────────────────────────────────────
    def submit_requisition(self, lines, ship_to=None, delivery_method=None,
                           payment_method=None, deferred_payment=None,
                           custom_number=None, comments=None,
                           fiscal_document_email=None):
        """POST /ic/sales/requisition — phase 1.

        Line schema uses ``requiredQuantity``, ``unitPriceNet`` and
        ``unitPriceGross`` (not ``quantity``). Nothing is ordered until
        ``confirm_requisition`` is called.
        """
        payload = {'lines': lines}
        if ship_to:
            payload['shipTo'] = ship_to
        if delivery_method:
            payload['deliveryMethod'] = delivery_method
        if payment_method:
            payload['paymentMethod'] = payment_method
        # ``deferredPayment`` is Polish-market only; the caller decides.
        if deferred_payment is not None:
            payload['deferredPayment'] = bool(deferred_payment)
        if custom_number:
            payload['customNumber'] = custom_number
        if comments:
            payload['comments'] = comments
        if fiscal_document_email:
            payload['fiscalDocumentEmailAddress'] = fiscal_document_email
        res = self._request('POST', _EP_SALES_REQUISITION, payload=payload)
        return self._get_json(res, _EP_SALES_REQUISITION)

    def confirm_requisition(self, requisition_id, ship_to=None):
        """POST /ic/sales/requisition/{id}/confirm — phase 2.

        Only ``ACCEPTED`` requisitions can be confirmed. IC returns 409
        if the requisition is already accepted.
        """
        params = {'shipTo': ship_to} if ship_to else None
        res = self._request(
            'POST', f"{_EP_SALES_REQUISITION}/{requisition_id}/confirm",
            params=params,
        )
        return self._get_json(res, _EP_SALES_REQUISITION)

    def cancel_requisition(self, requisition_id, ship_to=None):
        """POST /ic/sales/requisition/{id}/cancel — only if ACCEPTED."""
        params = {'shipTo': ship_to} if ship_to else None
        res = self._request(
            'POST', f"{_EP_SALES_REQUISITION}/{requisition_id}/cancel",
            params=params,
        )
        return self._get_json(res, _EP_SALES_REQUISITION)

    def get_requisition(self, requisition_id):
        """GET /ic/sales/requisition/{id} — current fulfilment state."""
        res = self._request(
            'GET', f"{_EP_SALES_REQUISITION}/{requisition_id}",
        )
        return self._get_json(res, _EP_SALES_REQUISITION)

    def search_orders(self, creation_from, creation_to, ship_to=None,
                      offset=1, limit=50):
        """GET /ic/sales/requisition — order search.

        IC enforces a **max 2-day window** between creation_from/to,
        and limit is capped at 50.
        """
        params = {
            'creationDateFrom': creation_from,
            'creationDateTo': creation_to,
            'offset': max(1, offset),
            'limit': max(1, min(limit, 50)),
        }
        if ship_to:
            params['shipTo'] = ship_to
        res = self._request('GET', _EP_SALES_REQUISITION, params=params)
        return self._get_json(res, _EP_SALES_REQUISITION)

    def get_order(self, order_id):
        """GET /ic/sales/order/{id} — single order detail."""
        res = self._request('GET', f"{_EP_SALES_ORDER}/{order_id}")
        return self._get_json(res, _EP_SALES_ORDER)

    # ── INVOICE ──────────────────────────────────────────────────────────
    def search_invoices(self, issue_from, issue_to, ship_to=None,
                        classification=None, offset=1, limit=200):
        """GET /ic/invoice — invoice summaries.

        Max 2-day window on issue_from/issue_to. Limit ≤ 200.
        ``classification`` is 'INVOICE' or 'CREDIT_NOTE'.
        """
        params = {
            'issueDateFrom': issue_from,
            'issueDateTo': issue_to,
            'offset': max(1, offset),
            'limit': max(1, min(limit, 200)),
        }
        if ship_to:
            params['shipTo'] = ship_to
        if classification:
            params['invoiceClassification'] = classification
        res = self._request('GET', _EP_INVOICE, params=params)
        return self._get_json(res, _EP_INVOICE)

    def get_invoice(self, invoice_id, tech_id=None):
        """GET /ic/invoice/{id} — full invoice detail.

        Slashes in ``invoice_id`` must be URL-encoded to ``%2F``. Requests
        does this for us when we pass the id through ``requests.utils.quote``.
        """
        quoted = requests.utils.quote(str(invoice_id), safe='')
        params = {'techId': tech_id} if tech_id else None
        res = self._request(
            'GET', f"{_EP_INVOICE}/{quoted}", params=params,
        )
        return self._get_json(res, _EP_INVOICE)

    # ── DELIVERY ─────────────────────────────────────────────────────────
    def search_deliveries(self, creation_from, creation_to,
                          offset=1, limit=50):
        """GET /ic/delivery — max 2-day window, limit ≤ 50."""
        params = {
            'creationDateFrom': creation_from,
            'creationDateTo': creation_to,
            'offset': max(1, offset),
            'limit': max(1, min(limit, 50)),
        }
        res = self._request('GET', _EP_DELIVERY, params=params)
        return self._get_json(res, _EP_DELIVERY)

    def get_delivery(self, delivery_id):
        res = self._request('GET', f"{_EP_DELIVERY}/{delivery_id}")
        return self._get_json(res, _EP_DELIVERY)

    def get_delivery_metadata(self, creation_from, creation_to,
                              offset=1, limit=50):
        params = {
            'creationDateFrom': creation_from,
            'creationDateTo': creation_to,
            'offset': max(1, offset),
            'limit': max(1, min(limit, 50)),
        }
        res = self._request('GET', _EP_DELIVERY_METADATA, params=params)
        return self._get_json(res, _EP_DELIVERY_METADATA)

    # ── CUSTOMER ─────────────────────────────────────────────────────────
    def get_customer(self):
        res = self._request('GET', _EP_CUSTOMER)
        return self._get_json(res, _EP_CUSTOMER)

    def get_finances(self):
        """GET /ic/customer/finances — credit / overdue / orderingAllowed.

        A 400 on ordering usually maps to ``orderingAllowed=false`` here.
        """
        res = self._request('GET', _EP_CUSTOMER_FINANCES)
        return self._get_json(res, _EP_CUSTOMER_FINANCES)
