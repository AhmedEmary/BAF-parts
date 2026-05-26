import logging
import requests
from datetime import datetime, timezone, timedelta

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class DhlAPIClient:
    def __init__(self, account):
        self.account = account
        self.api_key = account.dhl_api_key
        self.api_secret = account.dhl_api_secret
        self.account_number = account.account_number
        self.is_prod = account.prod_environment
        self.base_url = (
            "https://express.api.dhl.com/mydhlapi"
            if account.prod_environment
            else "https://express.api.dhl.com/mydhlapi/test"
        )

    # =========================================================================
    # HTTP / ERRORS
    # =========================================================================
    def _request(self, method, endpoint, payload=None, params=None):
        url = f"{self.base_url}{endpoint}"
        try:
            res = requests.request(
                method, url,
                json=payload,
                params=params,
                headers={'Content-Type': 'application/json'},
                auth=(self.api_key, self.api_secret),
                timeout=20,
            )
            _logger.info(
                "DHL %s %s | status=%s | body=%s",
                method, url, res.status_code, (res.text or '')[:1000],
            )
            return res
        except requests.exceptions.Timeout:
            _logger.exception("DHL API timeout: %s %s", method, url)
            raise UserError("DHL API request timed out.")
        except requests.exceptions.ConnectionError as e:
            _logger.exception("DHL API connection error: %s %s", method, url)
            raise UserError("Failed to connect to DHL API: %s" % str(e))

    def _parse_error(self, res):
        status = getattr(res, 'status_code', 'N/A')
        body = (res.text or '').strip() if hasattr(res, 'text') else ''
        try:
            err = res.json()
            message = (
                err.get('detail')
                or err.get('title')
                or err.get('message')
                or body
            )
        except Exception:
            message = body
        if not message:
            message = "Empty response body from DHL"
        return "[HTTP %s] %s" % (status, message)

    def _planned_date(self, dt=None):
        if dt is None:
            dt = datetime.now(timezone.utc) + timedelta(hours=2)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if dt.hour >= 17:
            dt = (dt + timedelta(days=1)).replace(
                hour=10, minute=0, second=0, microsecond=0,
            )
        while dt.weekday() >= 5:
            dt = (dt + timedelta(days=1)).replace(
                hour=10, minute=0, second=0, microsecond=0,
            )

        return dt.strftime("%Y-%m-%dT%H:%M:%S GMT+00:00")

    # =========================================================================
    # ADDRESS / PACKAGE HELPERS
    # =========================================================================
    def _build_address_rate(self, partner):
        addr = {
            "postalCode": partner.zip or '',
            "cityName": partner.city or '',
            "countryCode": partner.country_id.code or '',
        }
        if partner.street:
            addr["addressLine1"] = partner.street
        if partner.street2:
            addr["addressLine2"] = partner.street2
        if partner.state_id:
            addr["countyName"] = partner.state_id.name
        return addr

    def _build_address_shipment(self, partner):
        postal = {
            "postalCode": partner.zip or '',
            "cityName": partner.city or '',
            "countryCode": partner.country_id.code or '',
            "addressLine1": partner.street or partner.name,
        }
        if partner.street2:
            postal["addressLine2"] = partner.street2
        if partner.state_id:
            postal["countyName"] = partner.state_id.name

        contact = {
            "phone": (
                partner.phone or getattr(partner, 'mobile', '') or '0000000000'
            ),
            "companyName": (
                partner.parent_id.name if partner.parent_id else partner.name
            ),
            "fullName": partner.name,
        }
        if partner.email:
            contact["email"] = partner.email

        return {"postalAddress": postal, "contactInformation": contact}

    def _build_packages(self, pallets):
        return [
            {
                "weight": float(p.weight or 1.0),
                "dimensions": {
                    "length": int(p.length or 1),
                    "width": int(p.width or 1),
                    "height": int(p.height or 1),
                },
            }
            for p in pallets
        ]

    def _build_export_declaration_from_picking(self, picking, currency_code):
        line_items = []
        moves = picking.move_ids.filtered(
            lambda m: m.product_id and m.product_id.type != 'service'
        )
        for i, move in enumerate(moves, start=1):
            qty = float(move.product_uom_qty or 1.0)
            sale_line = getattr(move, 'sale_line_id', False)
            price = (
                float(sale_line.price_unit) if sale_line and sale_line.price_unit
                else float(move.product_id.list_price or 10.0)
            )
            country_code = (
                move.product_id.origin.code
                if hasattr(move.product_id, 'origin') and move.product_id.origin
                else "IT"
            )
            line_items.append({
                "number": i,
                "description": (move.product_id.name or 'Goods')[:35],
                "price": round(price, 2),
                "priceCurrency": currency_code,
                "quantity": {
                    "value": int(qty) or 1, "unitOfMeasurement": "PCS",
                },
                "weight": {"netValue": 1.0, "grossValue": 1.1},
                "manufacturerCountry": country_code,
                "exportReasonType": "permanent",
            })

        if not line_items:
            line_items.append({
                "number": 1,
                "description": "General Goods",
                "price": 100.0,
                "priceCurrency": currency_code,
                "quantity": {"value": 1, "unitOfMeasurement": "PCS"},
                "weight": {"netValue": 1.0, "grossValue": 1.1},
                "manufacturerCountry": "IT",
                "exportReasonType": "permanent",
            })

        return {
            "lineItems": line_items,
            "invoice": {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "number": picking.name or "PICK-001",
            },
            "exportReason": "SOLD",
            "placeOfIncoterm": picking.company_id.city or "Milan",
        }

    # =========================================================================
    # CORE API METHODS
    # =========================================================================
    def test_connection(self):
        res = self._request("GET", "/address-validate", params={
            "type": "delivery",
            "countryCode": "US",
            "postalCode": "10001",
            "strictValidation": "false",
        })
        if res.status_code in [200, 201]:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Connection Successful!',
                    'message': ('Successfully connected to DHL Account: %s',
                                self.account.name),
                    'type': 'success',
                },
            }
        raise UserError("DHL Connection Failed: %s" % self._parse_error(res))

    def fetch_all_shipping_rates(self, ship_from, ship_to, pallets, currency_code,
                                 customs_value=None):
        if not ship_from.country_id:
            raise UserError(
                "Shipper address for '%s' is missing a Country." % ship_from.name
            )
        if not ship_to.country_id:
            raise UserError(
                "Recipient address for '%s' is missing a Country." % ship_to.name
            )

        is_international = ship_from.country_id.code != ship_to.country_id.code
        if customs_value:
            total_value = float(customs_value)
        else:
            total_value = 100.0

        payload = {
            "customerDetails": {
                "shipperDetails": self._build_address_rate(ship_from),
                "receiverDetails": self._build_address_rate(ship_to),
            },
            "accounts": [{"typeCode": "shipper", "number": self.account_number}],
            "plannedShippingDateAndTime": self._planned_date(),
            "unitOfMeasurement": "metric",
            "isCustomsDeclarable": is_international,
            "monetaryAmount": [{
                "typeCode": "declaredValue",
                "value": max(float(total_value), 1.0),
                "currency": currency_code,
            }],
            "requestAllValueAddedServices": False,
            "returnStandardProductsOnly": False,
            "nextBusinessDay": False,
            "productTypeCode": "all",
            "packages": self._build_packages(pallets),
        }

        res = self._request("POST", "/rates", payload)
        if res.status_code not in [200, 201]:
            raise UserError("DHL Rate Error: %s" % self._parse_error(res))

        data = res.json()
        available_rates = []
        for product in data.get('products', []):
            product_code = product.get('productCode', '')
            delivery_time = (
                product.get('deliveryCapabilities', {})
                .get('estimatedDeliveryDateAndTime', 'Standard')
            )
            cost = 0.0
            for price_entry in product.get('totalPrice', []):
                if price_entry.get('priceCurrency') == currency_code:
                    cost = float(price_entry.get('price', 0.0))
                    break
            if cost == 0.0 and product.get('totalPrice'):
                cost = float(product['totalPrice'][0].get('price', 0.0))

            if cost > 0:
                available_rates.append({
                    'service_type': product_code,
                    'delivery_time': delivery_time,
                    'cost': cost,
                })
        return available_rates

    # =========================================================================
    # ORDER-DRIVEN METHODS (used by shipping.delivery.order)
    # =========================================================================
    def _build_address_from_order(self, order, prefix):
        country_code = (
            order[f'{prefix}_country_id'].code or ''
        ) if order[f'{prefix}_country_id'] else ''
        postal = {
            "postalCode": order[f'{prefix}_zip'] or '',
            "cityName": order[f'{prefix}_city'] or '',
            "countryCode": country_code,
            "addressLine1": (
                order[f'{prefix}_street']
                or order[f'{prefix}_name'] or ''
            ),
        }
        if order[f'{prefix}_street2']:
            postal["addressLine2"] = order[f'{prefix}_street2']
        if order[f'{prefix}_state_id']:
            postal["countyName"] = order[f'{prefix}_state_id'].name

        contact = {
            "phone": order[f'{prefix}_phone'] or '0000000000',
            "companyName": (
                order[f'{prefix}_company_name']
                or order[f'{prefix}_name'] or ''
            ),
            "fullName": order[f'{prefix}_name'] or '',
        }
        if order[f'{prefix}_email']:
            contact["email"] = order[f'{prefix}_email']
        return {"postalAddress": postal, "contactInformation": contact}

    def _build_packages_from_order(self, order):
        return [
            {
                "weight": float(p.weight or 1.0),
                "dimensions": {
                    "length": int(p.length or 1),
                    "width": int(p.width or 1),
                    "height": int(p.height or 1),
                },
            }
            for p in order.package_ids
        ]

    def build_shipment_payload(self, order):
        currency_code = (
            order.declared_value_currency_id or order.currency_id
        ).name or 'EUR'
        is_international = order.is_customs_declarable
        declared_value = float(
            order.declared_value or order.freight_charge or 100.0
        )

        content = {
            "packages": self._build_packages_from_order(order),
            "isCustomsDeclarable": is_international,
            "declaredValue": max(declared_value, 1.0),
            "declaredValueCurrency": currency_code,
            "description": order.description or 'Auto parts',
            "incoterm": order.incoterm or 'DAP',
            "unitOfMeasurement": "metric",
        }
        if is_international and order.picking_id:
            content["exportDeclaration"] = (
                self._build_export_declaration_from_picking(
                    order.picking_id, currency_code,
                )
            )

        template_name = (
            order.label_format
            if order.label_format and order.label_format != 'STANDARD'
            else 'ECOM26_A4_001'
        )
        planned_dt = order.planned_shipping_datetime
        if planned_dt:
            planned_dt = planned_dt.replace(tzinfo=timezone.utc)

        return {
            "plannedShippingDateAndTime": self._planned_date(planned_dt),
            "pickup": {"isRequested": bool(order.pickup_requested)},
            "productCode": order.service_type,
            "accounts": [{"typeCode": "shipper", "number": self.account_number}],
            "customerDetails": {
                "shipperDetails": self._build_address_from_order(order, 'shipper'),
                "receiverDetails": self._build_address_from_order(order, 'recipient'),
            },
            "content": content,
            "outputImageProperties": {
                "printerDPI": 300,
                "encodingFormat": "pdf",
                "imageOptions": [{
                    "typeCode": "label",
                    "templateName": template_name,
                    "isRequested": True,
                }],
            },
        }

    def submit_shipment(self, order, payload=None):
        payload = payload or self.build_shipment_payload(order)
        res = self._request("POST", "/shipments", payload)
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
            'endpoint': '/shipments',
            'method': 'POST',
        }
        if res.status_code not in [200, 201]:
            result['error_message'] = self._parse_error(res)
            return result

        try:
            data = res.json()
        except Exception:
            result['error_message'] = 'DHL returned a non-JSON response.'
            return result

        tracking = data.get('shipmentTrackingNumber') or 'UNKNOWN'
        charges = data.get('shipmentCharges') or []
        if charges:
            result['price'] = float(charges[0].get('price') or result['price'])

        labels = []
        for i, doc in enumerate(data.get('documents') or []):
            content = doc.get('content')
            if not content:
                continue
            doc_type = doc.get('typeCode') or ('label' if i == 0 else f'doc{i}')
            filename = f"DHL_{doc_type}_{tracking}.pdf"
            labels.append((filename, content))

        result.update({
            'success': True,
            'tracking_number': tracking,
            'labels': labels,
        })
        return result

    def void_shipment(self, tracking_number):
        # DHL Express does not expose a DELETE /shipments endpoint. We
        # clear the tracking locally only.
        _logger.info(
            "DHL void_shipment: no API call. Tracking %s cleared locally.",
            tracking_number,
        )
        return True
