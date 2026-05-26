import json
import logging
import requests

from odoo import _
from odoo.exceptions import UserError

# All FedEx requests use YOUR_PACKAGING. We ship palletised auto parts;
# FedEx-branded packagings (FEDEX_BOX, FEDEX_PAK, ...) are not used.
FEDEX_PACKAGING = 'YOUR_PACKAGING'

_logger = logging.getLogger(__name__)


class FedexAPIClient:
    def __init__(self, account):
        self.account = account
        self.client_id = account.fedex_client_id
        self.client_secret = account.fedex_client_secret
        self.account_number = account.account_number
        self.token = account.fedex_rest_access_token
        self.base_url = (
            "https://apis.fedex.com"
            if account.prod_environment
            else "https://apis-sandbox.fedex.com"
        )

    # =========================================================================
    # AUTH
    # =========================================================================
    def test_connection(self):
        url = f"{self.base_url}/oauth/token"
        payload = (
            f"grant_type=client_credentials"
            f"&client_id={self.client_id}"
            f"&client_secret={self.client_secret}"
        )
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        try:
            response = requests.post(url, data=payload, headers=headers, timeout=15)
            response_data = response.json()
            if response.ok and 'access_token' in response_data:
                self.account.fedex_rest_access_token = response_data['access_token']
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Connection Successful!',
                        'message': ('Successfully connected to FedEx Account: %s',
                                    self.account.name),
                        'type': 'success',
                    },
                }
            error_msg = response_data.get(
                'errors', [{'message': 'Unknown Error'}],
            )[0].get('message')
            raise UserError(f"Connection Failed: {error_msg}")
        except Exception as e:
            raise UserError(f"Failed to connect: {str(e)}")

    def _auth(self):
        url = f"{self.base_url}/oauth/token"
        payload = (
            f"grant_type=client_credentials"
            f"&client_id={self.client_id}"
            f"&client_secret={self.client_secret}"
        )
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        res = requests.post(url, data=payload, headers=headers, timeout=10)
        if res.ok:
            self.token = res.json().get('access_token')
            self.account.fedex_rest_access_token = self.token
        else:
            raise UserError("FedEx Authentication Failed: %s" % res.text)

    def _request(self, method, endpoint, payload=None):
        if not self.token:
            self._auth()
        url = f"{self.base_url}{endpoint}"
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }
        res = requests.request(method, url, json=payload, headers=headers, timeout=20)
        if res.status_code == 401:
            self._auth()
            headers['Authorization'] = f'Bearer {self.token}'
            res = requests.request(method, url, json=payload, headers=headers, timeout=20)
        return res

    # =========================================================================
    # ADDRESS HELPERS
    # =========================================================================
    def _build_address(self, partner, include_contact=True):
        address = {
            "countryCode": partner.country_id.code or '',
            "city": partner.city or '',
            "postalCode": partner.zip or '',
            "streetLines": [
                line for line in [partner.street, partner.street2] if line
            ],
        }
        if partner.state_id and partner.country_id.code in [
            'US', 'CA', 'PR', 'IN', 'AU', 'IT', 'BR', 'MX',
        ]:
            address["stateOrProvinceCode"] = partner.state_id.code

        if not include_contact:
            return {"address": address}

        contact = {
            "phoneNumber": (
                partner.phone or getattr(partner, 'mobile', False) or '0000000000'
            ),
            "personName": partner.name,
            "companyName": partner.parent_id.name if partner.parent_id else partner.name,
            "emailAddress": partner.email or '',
        }
        return {"address": address, "contact": contact, "tins": []}

    def _validate_addresses_input(self, ship_from, ship_to):
        problems = []
        for partner, role in ((ship_from, 'shipper'), (ship_to, 'recipient')):
            missing = []
            if not partner.country_id or not partner.country_id.code:
                missing.append('country')
            if not (partner.street or partner.street2):
                missing.append('street')
            if not partner.city:
                missing.append('city')
            if not partner.zip:
                missing.append('zip')
            if missing:
                name = partner.display_name or partner.name or '?'
                problems.append(
                    "%s '%s' is missing: %s" % (role, name, ', '.join(missing))
                )
        if problems:
            raise UserError(
                "Cannot fetch FedEx rates: the shipping addresses are "
                "incomplete.\n%s\n\nOpen the partner record(s) and fill "
                "in country, street, city and zip before requesting "
                "rates." % '\n'.join('- ' + x for x in problems)
            )

    def _validate_packages_input(self, pallets):
        invalid = []
        for p in pallets:
            missing = []
            if not p.weight or float(p.weight) <= 0:
                missing.append('weight')
            for dim in ('length', 'width', 'height'):
                value = getattr(p, dim, 0) or 0
                if float(value) <= 0:
                    missing.append(dim)
            if missing:
                name = (
                    getattr(p, 'name', None)
                    or getattr(p, 'display_name', None)
                    or '?'
                )
                invalid.append("%s (missing: %s)" % (name, ', '.join(missing)))
        if invalid:
            raise UserError(
                "Cannot fetch FedEx rates: the following pallets are missing "
                "weight or dimensions:\n%s\n\nFill in weight, length, width "
                "and height before requesting rates."
                % '\n'.join('- ' + x for x in invalid)
            )

    def _build_packages(self, pallets, packaging_type='YOUR_PACKAGING'):
        items = []
        for p in pallets:
            item = {
                "weight": {"units": "KG", "value": float(p.weight)},
                "dimensions": {
                    "units": "CM",
                    "length": int(p.length),
                    "width": int(p.width),
                    "height": int(p.height),
                },
                "groupPackageCount": 1,
                "itemDescription": "Auto parts",
                "itemDescriptionForClearance": "Auto parts",
            }
            if getattr(p, 'name', None):
                item["customerReferences"] = [{
                    "customerReferenceType": "CUSTOMER_REFERENCE",
                    "value": p.name[:40],
                }]
            if packaging_type == 'YOUR_PACKAGING':
                item["subPackagingType"] = "PALLET"
            items.append(item)
        return items

    def _build_customs_from_picking(self, picking, currency_code, freight_charge):
        """Build a customsClearanceDetail block from a stock.picking.

        Iterates the picking's stock moves to itemise commodities. Pulls
        price from the sale-order line when available, otherwise from the
        product's list price.
        """
        moves = picking.move_ids.filtered(
            lambda m: m.product_id and m.product_id.type != 'service'
        )

        commodities = []
        for move in moves:
            qty = float(move.product_uom_qty or 1.0)
            sale_line = getattr(move, 'sale_line_id', False)
            price = (
                float(sale_line.price_unit) if sale_line and sale_line.price_unit
                else float(move.product_id.list_price or 10.0)
            )
            origin_country = (
                move.product_id.origin.code
                if hasattr(move.product_id, 'origin') and move.product_id.origin
                else "IT"
            )
            commodities.append({
                "description": (move.product_id.name or 'Goods')[:35],
                "customsValue": {
                    "amount": round(price * qty, 2),
                    "currency": currency_code,
                },
                "unitPrice": {"amount": price, "currency": currency_code},
                "countryOfManufacture": origin_country,
                "weight": {
                    "units": "KG",
                    "value": float(move.product_id.weight or 1.0) * qty,
                },
                "quantity": qty,
                "quantityUnits": "PCS",
                "numberOfPieces": 1,
            })

        if not commodities:
            commodities.append({
                "description": "General Goods",
                "customsValue": {"amount": 100.0, "currency": currency_code},
                "unitPrice": {"amount": 100.0, "currency": currency_code},
                "countryOfManufacture": "IT",
                "weight": {"units": "KG", "value": 1.0},
                "quantity": 1.0,
                "quantityUnits": "PCS",
                "numberOfPieces": 1,
            })

        return {
            "dutiesPayment": {"paymentType": "SENDER"},
            "commodities": commodities,
            "commercialInvoice": {
                "shipmentPurpose": "SOLD",
                "originatorName": picking.company_id.name,
                "freightCharge": {
                    "amount": float(freight_charge),
                    "currency": currency_code,
                },
                "termsOfSale": "DAP",
            },
        }

    # =========================================================================
    # CORE API METHODS
    # =========================================================================
    def validate_address(self, partner):
        if not partner.country_id:
            raise UserError(
                "The address for '%s' is missing a Country." % partner.name
            )

        payload = {
            "addressesToValidate": [
                self._build_address(partner, include_contact=False),
            ],
        }
        res = self._request("POST", "/address/v1/addresses/resolve", payload)
        if res.status_code not in [200, 201]:
            error_data = res.json()
            errors = error_data.get('errors', [])
            error_msg = errors[0].get('message') if errors else res.text
            raise UserError(
                "FedEx Address Validation Error for '%s':\n%s"
                % (partner.name, error_msg)
            )

    def fetch_all_shipping_rates(self, ship_from, ship_to, pallets, currency_code,
                                 customs_value=None):
        self._validate_addresses_input(ship_from, ship_to)
        self._validate_packages_input(pallets)
        self.validate_address(ship_from)
        self.validate_address(ship_to)

        if not self.token:
            self._auth()

        src_country = ship_from.country_id.code
        dst_country = ship_to.country_id.code
        is_international = src_country != dst_country
        handling_units = len(pallets)
        total_weight = sum(float(p.weight) for p in pallets) or 1.0
        declared_value = float(customs_value) if customs_value else 100.0
        if declared_value <= 0:
            declared_value = 100.0

        packaging_type = FEDEX_PACKAGING

        payload = {
            "accountNumber": {"value": self.account_number},
            "requestedShipment": {
                "rateRequestType": ["ACCOUNT", "LIST"],
                "preferredCurrency": currency_code,
                "pickupType": "USE_SCHEDULED_PICKUP",
                "packagingType": packaging_type,
                "shipper": self._build_address(ship_from, include_contact=False),
                "recipient": self._build_address(ship_to, include_contact=False),
                "requestedPackageLineItems": self._build_packages(
                    pallets, packaging_type=packaging_type,
                ),
            },
        }
        if is_international:
            payload["requestedShipment"]["customsClearanceDetail"] = {
                "dutiesPayment": {"paymentType": "SENDER"},
                "commercialInvoice": {"shipmentPurpose": "SOLD"},
                "commodities": [{
                    "description": "General Goods",
                    "quantity": 1,
                    "numberOfPieces": handling_units,
                    "quantityUnits": "EA",
                    "weight": {"units": "KG", "value": total_weight},
                    "customsValue": {
                        "amount": declared_value, "currency": currency_code,
                    },
                    "unitPrice": {
                        "amount": declared_value, "currency": currency_code,
                    },
                    "countryOfManufacture": src_country or "IT",
                }],
            }

        _logger.info(
            "FedEx rate fetch — env=%s shipment %s->%s, %d pallet(s), "
            "total %skg, packaging=%s.",
            'PROD' if self.account.prod_environment else 'SANDBOX',
            src_country, dst_country, handling_units, total_weight,
            packaging_type,
        )

        rates, error = self._fetch_rates_for_payload(
            packaging_type, payload, currency_code,
        )

        if rates:
            for r in rates:
                r['packaging_type'] = packaging_type
            dedup = {}
            for r in rates:
                key = (r['service_type'], r['packaging_type'])
                cur = dedup.get(key)
                if cur is None or r['cost'] < cur['cost']:
                    dedup[key] = r
            return list(dedup.values())

        err = error or {}
        params = err.get('parameters') or {}
        params_str = ''
        if params:
            params_str = '\nparams: ' + ', '.join(
                '%s=%s' % (k, v) for k, v in params.items()
            )
        raise UserError(
            "FedEx returned no rates for packaging '%s'.\n[%s] %s%s"
            % (
                packaging_type,
                err.get('code') or 'UNKNOWN',
                err.get('message') or 'No rates returned.',
                params_str,
            )
        )

    def _fetch_rates_for_payload(self, packaging_type, payload, currency_code):
        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "FedEx rate request [%s]:\n%s",
                packaging_type, json.dumps(payload, indent=2),
            )

        res = self._request("POST", "/rate/v1/rates/quotes", payload)

        # Sandbox Customs Bug Workaround
        if (res.status_code == 400
                and 'SYSTEM.UNEXPECTED.ERROR' in res.text
                and 'customsClearanceDetail' in payload['requestedShipment']):
            del payload['requestedShipment']['customsClearanceDetail']
            res = self._request("POST", "/rate/v1/rates/quotes", payload)

        if res.status_code not in [200, 201]:
            log_fn = _logger.warning if res.status_code >= 500 else _logger.info
            log_fn(
                "FedEx rate request [%s] HTTP %s\n"
                "  endpoint: POST /rate/v1/rates/quotes\n"
                "  request:  %s\n"
                "  response: %s",
                packaging_type, res.status_code,
                json.dumps(payload, separators=(',', ':'))[:3000],
                (res.text or '')[:2000],
            )
            return [], self._extract_error(res)

        data = res.json()
        rates = []
        if 'output' in data and 'rateReplyDetails' in data['output']:
            for detail in data['output']['rateReplyDetails']:
                service = detail.get('serviceType')
                cost = 0.0
                for rate in detail.get('ratedShipmentDetails', []):
                    if rate.get('currency') == currency_code:
                        cost = rate.get(
                            'totalNetChargeWithDutiesAndTaxes',
                            rate.get('totalNetCharge', 0.0),
                        )
                        break
                if cost == 0.0 and detail.get('ratedShipmentDetails'):
                    fallback = detail['ratedShipmentDetails'][0]
                    cost = fallback.get(
                        'totalNetChargeWithDutiesAndTaxes',
                        fallback.get('totalNetCharge', 0.0),
                    )
                if cost > 0:
                    rates.append({
                        'service_type': service,
                        'delivery_time': detail.get('deliveryStation', 'Standard'),
                        'cost': cost,
                    })

        if not rates:
            alerts = []
            for detail in (data.get('output', {}) or {}).get(
                'rateReplyDetails', []
            ) or []:
                for note in detail.get('alerts') or []:
                    alerts.append("%s: %s" % (
                        note.get('code') or '?', note.get('message') or '',
                    ))
            for note in (data.get('output', {}) or {}).get('alerts') or []:
                alerts.append("%s: %s" % (
                    note.get('code') or '?', note.get('message') or '',
                ))
            _logger.info(
                "FedEx rate request [%s] HTTP 200 but no rates. Alerts: %s",
                packaging_type, alerts or '(none)',
            )
            return [], {
                'code': 'EMPTY',
                'message': (
                    'FedEx returned 200 but no priced rates. '
                    'Alerts: ' + '; '.join(alerts)
                ) if alerts else 'FedEx returned 200 but no priced rates.',
                'parameters': {},
            }
        return rates, None

    def _extract_error(self, res):
        try:
            error_data = res.json()
        except ValueError:
            return {
                'code': 'HTTP_%d' % res.status_code,
                'message': (res.text or '')[:200],
                'parameters': {},
            }
        errors = error_data.get('errors') or []
        first = errors[0] if errors else {}
        params = {
            (entry.get('key') or '?'): (entry.get('value') or '')
            for entry in (first.get('parameterList') or [])
        }
        return {
            'code': (first.get('code') or '').upper() or 'UNKNOWN',
            'message': first.get('message') or (res.text or '')[:200],
            'parameters': params,
        }

    # =========================================================================
    # ORDER-DRIVEN METHODS (used by shipping.delivery.order)
    # =========================================================================
    def _build_address_from_order(self, order, prefix, include_contact=True):
        country_code = (
            order[f'{prefix}_country_id'].code or ''
        ) if order[f'{prefix}_country_id'] else ''
        street_lines = [
            line for line in [
                order[f'{prefix}_street'], order[f'{prefix}_street2'],
            ] if line
        ]
        address = {
            "countryCode": country_code,
            "city": order[f'{prefix}_city'] or '',
            "postalCode": order[f'{prefix}_zip'] or '',
            "streetLines": street_lines,
        }
        state = order[f'{prefix}_state_id']
        if state and country_code in [
            'US', 'CA', 'PR', 'IN', 'AU', 'IT', 'BR', 'MX',
        ]:
            address["stateOrProvinceCode"] = state.code

        if not include_contact:
            return {"address": address}

        contact = {
            "phoneNumber": order[f'{prefix}_phone'] or '0000000000',
            "personName": order[f'{prefix}_name'] or '',
            "companyName": (
                order[f'{prefix}_company_name']
                or order[f'{prefix}_name'] or ''
            ),
            "emailAddress": order[f'{prefix}_email'] or '',
        }
        return {"address": address, "contact": contact, "tins": []}

    def _build_packages_from_order(self, order):
        items = []
        for p in order.package_ids:
            sub_packaging = "PALLET" if p.package_type_id else "PACKAGE"
            item = {
                "weight": {"units": "KG", "value": float(p.weight or 1.0)},
                "dimensions": {
                    "units": "CM",
                    "length": int(p.length or 1),
                    "width": int(p.width or 1),
                    "height": int(p.height or 1),
                },
                "subPackagingType": sub_packaging,
                "groupPackageCount": 1,
                "itemDescription": (
                    p.description or order.description or 'Auto parts'
                ),
                "itemDescriptionForClearance": (
                    p.description or order.description or 'Auto parts'
                ),
            }
            ref_name = p.name or (p.package_type_id.name if p.package_type_id else '')
            if ref_name:
                item["customerReferences"] = [{
                    "customerReferenceType": "CUSTOMER_REFERENCE",
                    "value": ref_name[:40],
                }]
            items.append(item)
        return items

    def build_shipment_payload(self, order):
        currency_code = (
            order.declared_value_currency_id or order.currency_id
        ).name or 'EUR'
        is_international = order.is_customs_declarable
        packages = self._build_packages_from_order(order)
        handling_units = len(packages) or 1
        service_type = order.service_type
        stock_type = order.label_format or 'PAPER_8.5X11_TOP_HALF_LABEL'
        api_stock_type = (
            stock_type
            .replace('8.5', '85')
            .replace('4X6.75', '4X675')
            .replace('7X4.75', '7X47')
        )

        payload = {
            "accountNumber": {"value": self.account_number},
            "labelResponseOptions": "LABEL",
            "requestedShipment": {
                "rateRequestType": ["PREFERRED"],
                "preferredCurrency": currency_code,
                "pickupType": "USE_SCHEDULED_PICKUP",
                "serviceType": service_type,
                "packagingType": FEDEX_PACKAGING,
                "shippingChargesPayment": {"paymentType": "SENDER"},
                "labelSpecification": {
                    "labelStockType": api_stock_type,
                    "imageType": "PDF",
                },
                "shipper": self._build_address_from_order(order, 'shipper'),
                "recipients": [
                    self._build_address_from_order(order, 'recipient'),
                ],
                "requestedPackageLineItems": packages,
            },
        }

        if 'FREIGHT' in (service_type or ''):
            if any(k in service_type for k in [
                'INTERNATIONAL', 'DAY_FREIGHT', 'REGIONAL',
            ]):
                payload["requestedShipment"]["expressFreightDetail"] = {
                    "shippersLoadAndCount": handling_units,
                    "bookingConfirmationNumber": "12345678",
                }
            else:
                total_weight = sum(
                    float(p.weight or 0.0) for p in order.package_ids
                ) or 1.0
                payload["requestedShipment"]["freightShipmentDetail"] = {
                    "fedExFreightAccountNumber": self.account_number,
                    "role": "SHIPPER",
                    "lineItems": [{
                        "weight": {"units": "KG", "value": total_weight},
                        "handlingUnits": handling_units,
                        "description": "Palletized Goods",
                    }],
                }

        if is_international and order.picking_id:
            payload["requestedShipment"]["customsClearanceDetail"] = (
                self._build_customs_from_picking(
                    order.picking_id, currency_code, order.freight_charge or 0.0,
                )
            )
            payload["requestedShipment"]["shippingDocumentSpecification"] = {
                "shippingDocumentTypes": ["COMMERCIAL_INVOICE"],
                "commercialInvoiceDetail": {
                    "documentFormat": {
                        "docType": "PDF", "stockType": "PAPER_LETTER",
                    },
                },
            }

        return payload

    def submit_shipment(self, order, payload=None):
        payload = payload or self.build_shipment_payload(order)
        res = self._request("POST", "/ship/v1/shipments", payload)
        return self._parse_shipment_response(res, order, payload)

    def _parse_shipment_response(self, res, order, payload):
        result = {
            'success': False,
            'status_code': res.status_code,
            'tracking_number': '',
            'price': float(order.freight_charge or 0.0),
            'labels': [],
            'response_text': res.text or '',
            'error_message': '',
            'request_payload': payload,
            'endpoint': '/ship/v1/shipments',
            'method': 'POST',
        }
        try:
            data = res.json()
        except Exception:
            data = {}

        if res.status_code not in [200, 201]:
            errors = data.get('errors') or []
            result['error_message'] = (
                errors[0].get('message') if errors else (res.text or 'FedEx error')
            )
            return result

        transactions = data.get('output', {}).get('transactionShipments') or []
        if not transactions:
            result['error_message'] = 'FedEx response missing transactionShipments.'
            return result
        details = transactions[0]
        tracking_number = details.get('masterTrackingNumber') or 'UNKNOWN'

        rate_details = (
            details.get('completedShipmentDetail', {})
            .get('shipmentRating', {})
            .get('shipmentRateDetails')
            or [{}]
        )
        result['price'] = float(
            rate_details[0].get('totalNetCharge') or result['price']
        )

        labels = []
        for i, doc in enumerate(details.get('pieceResponses') or []):
            for ld in doc.get('packageDocuments') or []:
                enc = ld.get('encodedLabel')
                if enc:
                    labels.append(
                        (f"FedEx_label_{tracking_number}_{i + 1}.pdf", enc)
                    )
        for i, doc in enumerate(details.get('shipmentDocuments') or []):
            enc = doc.get('encodedLabel')
            if enc:
                labels.append(
                    (f"FedEx_doc_{tracking_number}_{i + 1}.pdf", enc)
                )

        result.update({
            'success': True,
            'tracking_number': tracking_number,
            'labels': labels,
        })
        return result

    def void_shipment(self, tracking_number):
        master_tracking = tracking_number.split(',')[0].strip()
        payload = {
            "accountNumber": {"value": self.account_number},
            "trackingNumber": master_tracking,
        }
        res = self._request("PUT", "/ship/v1/shipments/cancel", payload)

        if res.status_code not in [200, 201]:
            error_data = res.json()
            errors = error_data.get('errors', [])
            error_msg = errors[0].get('message') if errors else res.text
            raise UserError("FedEx Cancel Error: %s" % error_msg)
        return True
