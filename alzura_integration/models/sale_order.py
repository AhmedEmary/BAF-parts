import logging
from datetime import datetime

import requests
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ALZURA_ORDERS_URL = "https://api-b2b.alzura.com/common/latestorders"


class AlzuraProductMissing(UserError):
    """Raised when an Alzura position references a SKU with no matching product,
    so the whole order is rejected rather than imported with missing lines."""


class SaleOrder(models.Model):
    _inherit = "sale.order"

    @api.model
    def _cron_fetch_alzura_orders(self):
        """Cron entry point: fetch latest orders for every company holding a token."""
        companies = (
            self.env["res.company"].sudo().search([("alzura_token", "!=", False)])
        )
        for company in companies:
            try:
                result = self.with_company(company)._alzura_fetch_orders(company)
            except Exception as e:
                _logger.exception(
                    "Alzura: order fetch failed for company %s: %s", company.name, e
                )
                continue
            _logger.info(
                "Alzura: order fetch for company %s: imported %s new order(s); "
                "%s already present; %s rejected.",
                company.name,
                result["created"],
                result["skipped"],
                result["rejected"],
            )

    def _alzura_fetch_orders(self, company):
        """Call /common/latestorders and import new orders.

        Returns dict(created, skipped, rejected, total). Raises UserError on
        auth/limit errors so the manual button can surface a clear message.
        """
        company = company.sudo()
        body = self._alzura_orders_payload(company)
        orders = body.get("data") if isinstance(body, dict) else None
        orders = orders if isinstance(orders, list) else []

        created = skipped = rejected = 0
        for order_data in orders:
            try:
                # Savepoint so a rejected order rolls back any partner/address
                # created earlier in _alzura_import_order, without aborting the batch.
                with self.env.cr.savepoint():
                    imported = self._alzura_import_order(company, order_data)
            except AlzuraProductMissing as e:
                rejected += 1
                _logger.warning(
                    "Alzura: rejected order %s for company %s: %s",
                    order_data.get("order"),
                    company.name,
                    e,
                )
                continue
            except Exception as e:
                # Isolate any other per-order failure (confirmation, constraints)
                # so one bad order can't abort or permanently block the batch.
                rejected += 1
                _logger.exception(
                    "Alzura: failed to import order %s for company %s: %s",
                    order_data.get("order"),
                    company.name,
                    e,
                )
                continue
            if imported:
                created += 1
            else:
                skipped += 1

        _logger.info(
            "Alzura: imported %s new order(s), skipped %s, rejected %s, for company %s",
            created,
            skipped,
            rejected,
            company.name,
        )
        return {
            "created": created,
            "skipped": skipped,
            "rejected": rejected,
            "total": len(orders),
        }

    def _alzura_orders_payload(self, company):
        """Return the parsed /common/latestorders body."""
        if not company.alzura_token:
            raise UserError("No Alzura token. Fetch a token in Settings first.")
        if (
            company.alzura_token_expiry
            and company.alzura_token_expiry < fields.Datetime.now()
        ):
            raise UserError("Alzura token expired. Get a new token first.")

        response = requests.get(
            ALZURA_ORDERS_URL,
            headers=company._alzura_request_headers(),
            timeout=30,
        )
        if response.status_code == 401:
            raise UserError("Alzura authentication failed. Refresh the token.")
        if response.status_code == 429:
            raise UserError(
                "Alzura rate limit reached (2 requests per 5 minutes). Try again later."
            )
        response.raise_for_status()

        try:
            return response.json()
        except ValueError:
            return {}

    def _alzura_import_order(self, company, order_data):
        """Create one sale.order from an Alzura order dict. Idempotent via b2b_so.

        Returns the created order, or False if it already exists / is invalid.
        """
        alzura_ref = order_data.get("order")
        if not alzura_ref:
            return False

        existing = self.sudo().search(
            [("b2b_so", "=", alzura_ref), ("company_id", "=", company.id)], limit=1
        )
        if existing:
            return False

        partner = self._alzura_find_or_create_partner(
            order_data.get("buyer") or {}, order_data.get("country")
        )
        reference = order_data.get("reference_number") or ""
        shipping = order_data.get("shipping") or {}

        vals = {
            "company_id": company.id,
            "partner_id": partner.id,
            "partner_shipping_id": self._alzura_delivery_partner(partner, shipping).id,
            "b2b_so": alzura_ref,
            "so_source": self.env.ref(
                "alzura_integration.so_source_alzura",
                raise_if_not_found=False,
            ).id
            or False,
            "customer_po": reference,
            "client_order_ref": reference or alzura_ref,
            "date_order": self._alzura_parse_dt(order_data.get("date")),
            "order_line": self._alzura_build_lines(order_data.get("positions") or [])
            + self._alzura_charge_lines(order_data),
        }
        delivery_date = self._alzura_delivery_date(shipping)
        if delivery_date:
            vals["commitment_date"] = delivery_date
        note = self._alzura_order_note(order_data)
        if note:
            vals["note"] = note

        order = self.sudo().create(vals)
        # Alzura orders are already placed, so confirm them into a sale order
        # rather than leaving them as draft quotations.
        order.action_confirm()
        return order

    def _alzura_build_lines(self, positions):
        """Map Alzura positions to order_line create commands.

        Products are matched on product.product.sku (supplier_item_number).
        A position whose SKU has no product raises AlzuraProductMissing, which
        rejects the whole order rather than importing it with missing lines.
        """
        Product = self.env["product.product"].sudo()
        commands = []
        for pos in positions:
            sku = pos.get("supplier_item_number")
            product = Product.search([("sku", "=", sku)], limit=1)
            qty = pos.get("quantity") or 0.0
            price = (pos.get("price") or {}).get("net") or 0.0
            name = (
                pos.get("position_name")
                or pos.get("position_description")
                or "Alzura item"
            )

            if not product:
                raise AlzuraProductMissing(
                    "No product found for SKU '%s' (%s)." % (sku, name)
                )
            commands.append(
                (
                    0,
                    0,
                    {
                        "product_id": product.id,
                        "name": name,
                        "product_uom_qty": qty,
                        "price_unit": price,
                    },
                )
            )
        return commands

    def _alzura_find_or_create_partner(self, buyer, country_code=False):
        """Find a partner by Alzura buyer id (ref) or email, else create one.

        On first creation every available buyer field is captured: the full
        address (incl. name_additional), VAT, contact email/phone, the bank
        account, and the status / tax number / credit-reform enrichment that
        has no dedicated partner field (stored in the internal notes).
        """
        Partner = self.env["res.partner"].sudo()
        contact = buyer.get("contact") or {}
        address = buyer.get("address") or {}
        tax = buyer.get("tax") or {}

        buyer_id = buyer.get("id")
        ref = "ALZURA-%s" % buyer_id if buyer_id else False
        # Alzura masks the contact email behind a message URL; only trust real ones.
        email = contact.get("email")
        email = email if email and "@" in email else False
        phone = contact.get("phone")

        partner = Partner.browse()
        if ref:
            partner = Partner.search([("ref", "=", ref)], limit=1)
        if not partner and phone:
            partner = Partner.search([("phone", "=", phone)], limit=1)
        if not partner and email:
            partner = Partner.search([("email", "=", email)], limit=1)
        if partner:
            return partner

        partner = Partner.create(
            {
                "name": contact.get("name") or address.get("name") or "Alzura Buyer",
                "ref": ref,
                "email": email,
                "phone": phone,
                "street": address.get("street"),
                "street2": address.get("name_additional") or False,
                "city": address.get("city"),
                "zip": address.get("zip"),
                "country_id": (
                    self._alzura_country(address.get("country"))
                    or self._alzura_country_by_code(country_code)
                ).id
                or False,
                "vat": tax.get("sales_tax_identification_number") or False,
                "comment": self._alzura_partner_note(buyer) or False,
            }
        )
        self._alzura_create_partner_bank(partner, buyer.get("bank") or {})
        return partner

    def _alzura_partner_note(self, buyer):
        """Buyer data with no dedicated partner field: Alzura status, the
        secondary tax number and the credit-reform assessment.
        """
        tax = buyer.get("tax") or {}
        credit = buyer.get("credit_reform") or {}
        parts = []
        if buyer.get("status_name"):
            parts.append("Alzura status: %s" % buyer["status_name"])
        if tax.get("tax_number"):
            parts.append("Tax number: %s" % tax["tax_number"])
        if credit.get("text") or credit.get("index"):
            parts.append(
                "Credit reform: %s (index %s)"
                % (credit.get("text") or "-", credit.get("index") or "-")
            )
        # comment is an Html field, so separate entries with <br/>.
        return "<br/>".join(parts)

    def _alzura_create_partner_bank(self, partner, bank):
        """Store the buyer's IBAN as a res.partner.bank (with its BIC/bank).

        Guarded: a malformed IBAN raises during validation, which must not
        reject the whole order, so failures are logged and skipped.
        """
        iban = (bank.get("iban") or "").replace(" ", "")
        if not iban:
            return
        try:
            res_bank = self.env["res.bank"].browse()
            bic = bank.get("bic_swift")
            if bic:
                res_bank = (
                    self.env["res.bank"].sudo().search([("bic", "=", bic)], limit=1)
                )
                if not res_bank:
                    res_bank = (
                        self.env["res.bank"]
                        .sudo()
                        .create({"name": bank.get("bank") or bic, "bic": bic})
                    )
            self.env["res.partner.bank"].sudo().create(
                {
                    "partner_id": partner.id,
                    "acc_number": iban,
                    "acc_holder_name": bank.get("owner") or False,
                    "bank_id": res_bank.id or False,
                }
            )
        except Exception as e:
            _logger.warning(
                "Alzura: could not store bank account for partner %s: %s",
                partner.ref or partner.name,
                e,
            )

    def _alzura_delivery_partner(self, parent, shipping):
        """Shipping address. Buyer itself unless an alternative address is set,
        in which case a delivery child contact is found/created under the buyer.
        """
        delivery = shipping.get("delivery_address") or {}
        if not delivery.get("use_alternative_address"):
            return parent

        address = delivery.get("address") or {}
        contact = delivery.get("contact") or {}
        Partner = self.env["res.partner"].sudo()

        existing = Partner.search(
            [
                ("parent_id", "=", parent.id),
                ("type", "=", "delivery"),
                ("zip", "=", address.get("zip")),
                ("street", "=", address.get("street")),
            ],
            limit=1,
        )
        if existing:
            return existing

        email = contact.get("email")
        email = email if email and "@" in email else False

        return Partner.create(
            {
                "parent_id": parent.id,
                "type": "delivery",
                "name": address.get("name")
                or contact.get("name")
                or "Delivery address",
                "street": address.get("street"),
                "city": address.get("city"),
                "zip": address.get("zip"),
                "country_id": self._alzura_country(address.get("country")).id or False,
                "phone": contact.get("phone"),
                "email": email,
            }
        )

    def _alzura_charge_lines(self, order_data):
        """Fee order lines so the order net matches Alzura's total_sum.

        - shipping fee = shipping.method.price.net + shipping.handling_fee.net
        - payment fee  = payment.method.price.net + payment.price_additional.net
        Each fee is added only when non-zero. Any remaining gap between the
        positions plus fees and total_sum.net is booked as an alzura_charge
        line. All fees share one get-or-create service product.
        """
        shipping = order_data.get("shipping") or {}
        payment = order_data.get("payment") or {}

        def net(block):
            return (block or {}).get("net") or 0.0

        shipping_fee = net((shipping.get("method") or {}).get("price")) + net(
            shipping.get("handling_fee")
        )
        payment_fee = net((payment.get("method") or {}).get("price")) + net(
            payment.get("price_additional")
        )

        positions_net = sum(
            net(pos.get("price")) * (pos.get("quantity") or 0)
            for pos in order_data.get("positions") or []
        )
        remainder = round(
            net(order_data.get("total_sum"))
            - positions_net
            - shipping_fee
            - payment_fee,
            2,
        )

        charges = [
            ("Shipping fee", shipping_fee),
            ("Payment fee", payment_fee),
            ("alzura_charge", remainder),
        ]
        commands = []
        product = None
        for label, amount in charges:
            if not amount:
                continue
            if product is None:
                product = self._alzura_charge_product()
            commands.append(
                (
                    0,
                    0,
                    {
                        "product_id": product.id,
                        "name": label,
                        "product_uom_qty": 1.0,
                        "price_unit": amount,
                    },
                )
            )
        return commands

    def _alzura_charge_product(self):
        Product = self.env["product.product"].sudo()
        product = Product.search([("default_code", "=", "ALZURA-CHARGE")], limit=1)
        if not product:
            product = Product.create(
                {
                    "name": "Alzura Charge",
                    "default_code": "ALZURA-CHARGE",
                    "type": "service",
                    "purchase_ok": False,
                    "list_price": 0.0,
                }
            )
        return product

    def _alzura_delivery_date(self, shipping):
        """Delivery date: shipping.deliveryDate, else a tracking deliveryDate."""
        value = shipping.get("deliveryDate")
        if not value:
            for trk in shipping.get("tracking") or []:
                if trk.get("deliveryDate"):
                    value = trk["deliveryDate"]
                    break
        return self._alzura_parse_date(value)

    def _alzura_order_note(self, order_data):
        """Summary of fields without a dedicated SO field: comment, shipping
        method/flags/tracking, payment, currency conversion and documents.
        """
        shipping = order_data.get("shipping") or {}
        payment = order_data.get("payment") or {}
        currency = order_data.get("currency") or {}
        parts = []

        if order_data.get("comment"):
            parts.append(f"Comment: {order_data['comment']}")

        method = (shipping.get("method") or {}).get("name")
        if method:
            parts.append("Shipping method: %s" % method)
        flags = [
            f
            for f, on in (
                ("priority", shipping.get("priority")),
                ("neutral", shipping.get("neutral")),
            )
            if on
        ]
        if flags:
            parts.append("Shipping flags: %s" % ", ".join(flags))
        for trk in shipping.get("tracking") or []:
            bits = [trk.get("service"), trk.get("number"), trk.get("url")]
            bits = [b for b in bits if b]
            if bits:
                parts.append("Tracking: %s" % " ".join(bits))

        pay_method = payment.get("method") or {}
        if pay_method.get("name"):
            parts.append("Payment method: %s" % pay_method["name"])

        if currency.get("code_origin"):
            line = "Currency: %s" % currency["code_origin"]
            converted = currency.get("code_converted")
            if converted and converted != currency["code_origin"]:
                line += " -> %s (factor %s)" % (converted, currency.get("factor"))
            parts.append(line)

        for doc in order_data.get("documents") or []:
            if doc.get("endpoint"):
                parts.append(
                    "Document %s: %s" % (doc.get("type") or "", doc["endpoint"])
                )

        # note is an Html field, so separate entries with <br/>.
        return "<br/>".join(p for p in parts if p) or False

    def _alzura_country(self, name):
        """Match a res.country by full name; empty recordset if not found.

        Note: Alzura sends the localized name (e.g. "Deutschland"), which only
        matches when the DB language matches; _alzura_country_by_code is the
        reliable fallback.
        """
        Country = self.env["res.country"]
        if not name:
            return Country
        return Country.search([("name", "=ilike", name)], limit=1)

    def _alzura_country_by_code(self, code):
        """Match a res.country by ISO alpha-2 code; empty recordset otherwise."""
        Country = self.env["res.country"]
        if not code:
            return Country
        return Country.search([("code", "=ilike", code)], limit=1)

    def _alzura_parse_dt(self, value):
        if not value:
            return fields.Datetime.now()
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return fields.Datetime.now()

    def _alzura_parse_date(self, value):
        if not value:
            return False
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except (ValueError, TypeError):
                continue
        return False
