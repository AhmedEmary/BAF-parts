"""LexCom DMS 3.1 protocol layer: XML in, dict out, XML back.

This module deliberately performs **no ORM writes**. It converts an incoming
document into a plain dict, hands that to ``sale.order``, and renders the
result. Keeping the boundary here is what lets the order-building tests be
written against dict literals instead of hand-written XML.

Two spec rules drive the rendering helpers:

* element order inside a document is mandatory;
* an optional element that would be empty must be OMITTED, not sent empty.

Both are protocol errors if broken, so ``_child`` refuses to write an empty
value and every renderer appends in the order the specification tables list.
"""

import logging

from lxml import etree

from odoo import _, models

_logger = logging.getLogger(__name__)

VERSION = "3.1"
DMS_NAME = "Odoo - BAF Parts"

SUPPORTED_COMMANDS = ("commands-available", "order-submit", "order-append")

#: Mandatory elements per command, checked before any work is attempted.
#: Mirrors the "Occ. = 1" rows of the specification's request tables.
MANDATORY_FIELDS = {
    "order-submit": ("dealer-id", "country", "brand"),
    "order-append": ("dealer-id", "country", "order-id", "brand"),
    "commands-available": (),
}


class LexcomProtocolError(Exception):
    """A business-level rejection. Answered with HTTP 200 and an error document.

    Distinct from an unhandled exception, which is a critical server error and
    is answered with 5xx per the specification.
    """


def _text(node, path, default=None):
    """Text of the first ``path`` child, stripped; ``default`` when absent."""
    if node is None:
        return default
    found = node.find(path)
    if found is None or found.text is None:
        return default
    value = found.text.strip()
    return value or default


def _attr(node, path, name, default=None):
    if node is None:
        return default
    found = node if path is None else node.find(path)
    if found is None:
        return default
    return found.get(name, default)


def _decimal(node, path, default=None):
    raw = _text(node, path)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _integer(node, path, default=None):
    raw = _text(node, path)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _bool(node, path, default=None):
    raw = _text(node, path)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def _child(parent, tag, value, **attrs):
    """Append ``tag`` only when it carries a value.

    The specification requires empty optional elements to be omitted, so this
    is the single place that rule is enforced. ``False`` is a real value for a
    boolean element and is written; ``None`` and ``""`` are not.
    """
    if value is None or value == "":
        return None
    el = etree.SubElement(parent, tag)
    if isinstance(value, bool):
        el.text = "true" if value else "false"
    else:
        el.text = str(value)
    for key, val in attrs.items():
        if val not in (None, ""):
            el.set(key, str(val))
    return el


def _address_dict(node):
    """Parse a billing-address / delivery-address block into a flat dict."""
    if node is None:
        return {}
    return {
        "gender": _text(node, "gender"),
        "title": _text(node, "title"),
        "first_name": _text(node, "first-name"),
        "last_name": _text(node, "last-name"),
        "company": _text(node, "company"),
        "department": _text(node, "department"),
        "position": _text(node, "position"),
        "address1": _text(node, "address1"),
        "address2": _text(node, "address2"),
        "po_box": _text(node, "po-box"),
        "postal_code": _text(node, "postal-code"),
        "city": _text(node, "city"),
        "province": _text(node, "province"),
        # Address-level country is ISO alpha-2 and DOES resolve against
        # res.country.code, unlike the request-level alpha-3 country.
        "country": _text(node, "country"),
        "email": _text(node, "email"),
        "phone": _text(node, "phone"),
        "fax": _text(node, "fax"),
    }


class LexcomProtocol(models.AbstractModel):
    _name = "lexcom.protocol"
    _description = "LexCom DMS Protocol"

    # ── entry point ──────────────────────────────────────────────────────────

    def _dispatch(self, command, root, company, headers=None):
        """Handle one parsed request.

        Returns ``(response_element, outcome, message, order)`` where outcome is
        one of ``ok`` / ``business_error``. Raises ``LexcomProtocolError`` for a
        rejection the caller renders; anything else escaping is a bug and the
        controller turns it into a 5xx.
        """
        if command not in SUPPORTED_COMMANDS:
            raise LexcomProtocolError(
                _("Unsupported command '%s'.") % command
            )
        self._validate_version(command, root)
        self._validate_mandatory(command, root)

        handler = {
            "commands-available": self._handle_commands_available,
            "order-submit": self._handle_order_submit,
            "order-append": self._handle_order_append,
        }[command]
        return handler(root, company)

    # ── validation ───────────────────────────────────────────────────────────

    def _validate_version(self, command, root):
        """The request version must be 3.1 - except on commands-available.

        The specification is explicit that the version attribute of
        commands-available "must be ignored by the server as it could be a
        different version": it is the discovery call, so refusing it on version
        grounds would make version discovery impossible.
        """
        if command == "commands-available":
            return
        version = root.get("version")
        if version != VERSION:
            raise LexcomProtocolError(
                _("Unsupported protocol version '%(got)s'; this DMS implements "
                  "%(want)s.") % {"got": version or "", "want": VERSION}
            )

    def _validate_mandatory(self, command, root):
        for path in MANDATORY_FIELDS.get(command, ()):
            if _text(root, path) is None:
                raise LexcomProtocolError(
                    _("Mandatory field '%s' is missing.") % path
                )

    def _validate_dealer(self, root, company):
        """The dealer-id must match the company the credential resolved to."""
        dealer_id = _text(root, "dealer-id")
        expected = company.sudo().lexcom_dealer_id
        if expected and dealer_id != expected:
            raise LexcomProtocolError(
                _("Unknown dealer-id '%s'.") % dealer_id
            )
        return dealer_id

    # ── commands-available ───────────────────────────────────────────────────

    def _handle_commands_available(self, root, company):
        response = etree.Element("commands-available-response")
        response.set("version", VERSION)
        response.set("dms-name", DMS_NAME)
        for command in SUPPORTED_COMMANDS:
            _child(response, "command", command)
        return response, "ok", None, None

    # ── order-submit / order-append ──────────────────────────────────────────

    def _handle_order_submit(self, root, company):
        dealer_id = self._validate_dealer(root, company)
        vals = self._parse_order(root)
        order, message = self.env["sale.order"]._lexcom_build_order(vals, company)
        response = self._render_order_response(
            "order-submit-response",
            dealer_id=dealer_id,
            country=vals.get("country"),
            accepted=bool(order),
            order_id=order.name if order else None,
            customer_ref=vals.get("customer_ref"),
            message=message,
        )
        outcome = "ok" if order else "business_error"
        return response, outcome, message, order

    def _handle_order_append(self, root, company):
        dealer_id = self._validate_dealer(root, company)
        vals = self._parse_order(root)
        order, message = self.env["sale.order"]._lexcom_append_order(vals, company)
        response = self._render_order_response(
            "order-append-response",
            dealer_id=dealer_id,
            country=vals.get("country"),
            accepted=bool(order),
            order_id=vals.get("order_id"),
            customer_ref=vals.get("customer_ref"),
            message=message,
        )
        outcome = "ok" if order else "business_error"
        return response, outcome, message, order

    # ── parsing ──────────────────────────────────────────────────────────────

    def _parse_order(self, root):
        """order-submit / order-append request -> plain dict.

        The two commands share almost their whole element set; order-append
        adds order-id and drops nothing we consume.
        """
        return {
            "dealer_id": _text(root, "dealer-id"),
            "country": _text(root, "country"),
            "order_id": _text(root, "order-id"),
            "customer_number": _text(root, "customer-number"),
            "customer_name": _text(root, "customer-name"),
            "customer_ref": _text(root, "customer-ref"),
            "voucher_id": _text(root, "voucher-id"),
            "brand": _text(root, "brand"),
            "vin": _text(root, "vin"),
            "license_plate": _text(root, "license-plate"),
            "mileage": _integer(root, "mileage"),
            "mileage_unit": _attr(root, "mileage", "unit"),
            # LexCom systems set this true by default; true means partslink24
            # already showed the customer that price, so we honour it.
            "use_retail_price": _bool(root, "use-retail-price", True),
            "order_source_system": _text(root, "order-source-system"),
            "order_type": _text(root, "order-type"),
            "customer_contract": _attr(root, "order-type", "customer-contract"),
            "shipping_type": _text(root, "shipping-type"),
            "desired_shipping_on": _text(root, "desired-shipping-on"),
            "currency": _text(root, "currency"),
            "editor": _text(root, "editor"),
            "executer": _text(root, "executer"),
            "customer_comment": _text(root, "customer-comment"),
            "extensions": [
                (el.get("key"), (el.text or "").strip())
                for el in root.findall("extension")
            ],
            "payments": self._parse_payments(root.find("payments")),
            "billing_address": _address_dict(root.find("billing-address")),
            "delivery_address": _address_dict(root.find("delivery-address")),
            "items": [self._parse_item(el) for el in root.findall("item")],
        }

    def _parse_payments(self, node):
        if node is None:
            return []
        payments = []
        for payment in node.findall("payment"):
            method = payment.find("payment-method")
            payments.append({
                "status": _text(payment, "status"),
                "amount": _decimal(payment, "amount"),
                "currency": _text(payment, "currency"),
                "transaction_id": _text(payment, "transaction-id"),
                "type": _text(method, "type"),
                "provider": _text(method, "provider"),
                "provider_transaction_id": _attr(
                    method, "provider", "transaction-id"
                ),
                "scheme": _text(method, "scheme"),
            })
        return payments

    def _parse_item(self, node):
        item_type = (_text(node, "item-type") or "ARTICLE").upper()
        data = node.find("article-data")
        if item_type == "LABOUR":
            data = node.find("labour-data")
        parsed = {
            "item_id": _text(node, "item-id"),
            "item_type": item_type,
            "article_id": _text(data, "article-id"),
            "operation_id": _text(data, "operation-id"),
            "description": _text(data, "description"),
            "manufacturer": _text(data, "manufacturer"),
            "units": _integer(data, "units", 0),
            "price": _decimal(data, "price"),
            "retail_price": _decimal(data, "retail-price"),
            "retail_price_source": _attr(data, "retail-price", "source"),
            "tax_rate": _decimal(data, "tax-rate"),
            "vin": _text(data, "vin"),
            "customer_comment": _text(data, "customer-comment"),
        }
        return parsed

    # ── rendering ────────────────────────────────────────────────────────────

    def _render_order_response(self, tag, dealer_id, country, accepted,
                               order_id=None, customer_ref=None, message=None):
        """Render an order response in the specification's element order.

        Order is mandatory: dealer-id, country, order-accepted, order-id,
        customer-ref, message.
        """
        response = etree.Element(tag)
        response.set("version", VERSION)
        _child(response, "dealer-id", dealer_id)
        _child(response, "country", country)
        # order-accepted is Occ. 1, so it is always written, including false.
        el = etree.SubElement(response, "order-accepted")
        el.text = "true" if accepted else "false"
        _child(response, "order-id", order_id)
        _child(response, "customer-ref", customer_ref)
        _child(response, "message", message)
        return response

    def _render_error(self, message):
        error = etree.Element("error")
        error.set("version", VERSION)
        _child(error, "message", message or _("Malformed request."))
        return error

    def _serialize(self, element):
        """Element -> UTF-8 bytes with the XML declaration the spec requires."""
        return etree.tostring(
            element, xml_declaration=True, encoding="UTF-8", pretty_print=True
        )
