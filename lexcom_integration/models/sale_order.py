"""Turn a parsed LexCom order dict into Odoo records.

Per the layer boundary (design D12) this module receives plain dicts from
``lexcom.protocol`` and never sees XML, which is what lets every scenario below
be tested with a dict literal.

The edge-case posture is deliberately the Alzura one: degrade, do not reject.
An article we cannot match becomes a note line, a customer we cannot resolve is
created from the payload, and a LABOUR item becomes a service product. The
order still lands, the gaps are visible on it, and every degradation is named
in the response message. Only an order with nothing resolvable at all is
refused.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

from psycopg2 import IntegrityError

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

LEXCOM_LABOUR_CODE = "LEXCOM-LABOUR"
LEXCOM_PARTNER_TAG = "LexCom"

#: Order states that may still receive appended items.
APPENDABLE_STATES = ("draft", "sent", "sale")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    lexcom_source_system = fields.Char(
        string="LexCom Source System",
        readonly=True,
        copy=False,
        help="The LexCom application the order came from (partslink24, ETKA, "
             "ASA, ...). Finer grained than so_source, which only says which "
             "integration received it.",
    )
    lexcom_vin = fields.Char(string="VIN", index=True, copy=False, readonly=True)
    lexcom_license_plate = fields.Char(
        string="License Plate", index=True, copy=False, readonly=True
    )
    lexcom_payload_hash = fields.Char(
        string="LexCom Payload Hash",
        index=True,
        copy=False,
        readonly=True,
        help="Digest of the submitted payload plus its UTC date. The database "
             "constraint on this column is what makes a retried submission "
             "idempotent; an application-level check would lose the race.",
    )

    _lexcom_payload_hash_uniq = models.Constraint(
        "unique(lexcom_payload_hash)",
        "This LexCom order has already been submitted.",
    )

    # ── entry points ─────────────────────────────────────────────────────────

    @api.model
    def _lexcom_build_order(self, vals, company):
        """Create one confirmed sale order. Returns ``(order, message)``.

        ``order`` is an empty recordset when the order was refused, in which
        case ``message`` says why. A replayed payload returns the ORIGINAL
        order with an explanatory message, so a middleware retry is idempotent
        rather than duplicating the customer's order.
        """
        notes = []
        payload_hash = self._lexcom_payload_hash(vals)

        existing = self.sudo().search(
            [("lexcom_payload_hash", "=", payload_hash)], limit=1
        )
        if existing:
            return existing, _(
                "Duplicate submission; returning the original order %s."
            ) % existing.name

        brand, brand_note = self._lexcom_resolve_brand(vals.get("brand"))
        if brand_note:
            notes.append(brand_note)

        lines, line_notes, matched = self._lexcom_build_lines(
            vals, company, brand
        )
        notes.extend(line_notes)

        # Resolve the order's viability BEFORE writing anything. Creating the
        # partner first would leave a stray contact behind on an order we go on
        # to refuse, which is exactly the "zero rows created" guarantee the
        # atomicity rule promises.
        if not matched:
            return self.browse(), _(
                "No item in this order could be resolved; nothing was created. %s"
            ) % " ".join(notes)

        partner, partner_note = self._lexcom_find_or_create_partner(vals)
        if partner_note:
            notes.append(partner_note)

        source = self.env.ref(
            "lexcom_integration.so_source_lexcom", raise_if_not_found=False
        )
        order_vals = {
            "company_id": company.id,
            "partner_id": partner.id,
            "so_source": source.id if source else False,
            "client_order_ref": vals.get("customer_ref") or False,
            "customer_po": vals.get("voucher_id") or vals.get("customer_ref") or False,
            "lexcom_source_system": vals.get("order_source_system") or False,
            "lexcom_vin": vals.get("vin") or False,
            "lexcom_license_plate": vals.get("license_plate") or False,
            "lexcom_payload_hash": payload_hash,
            "order_line": lines,
        }
        shipping = self._lexcom_delivery_partner(partner, vals)
        if shipping:
            order_vals["partner_shipping_id"] = shipping.id
        note = self._lexcom_order_note(vals)
        if note:
            order_vals["note"] = note

        try:
            with self.env.cr.savepoint():
                order = self.sudo().create(order_vals)
        except IntegrityError:
            # Lost the race against a concurrent identical submission. The
            # database, not the application, is what makes this safe.
            existing = self.sudo().search(
                [("lexcom_payload_hash", "=", payload_hash)], limit=1
            )
            if existing:
                return existing, _(
                    "Duplicate submission; returning the original order %s."
                ) % existing.name
            raise

        # The garage already clicked buy in partslink24, and the payload can
        # carry settled card payments, so a draft quotation would misrepresent
        # what the customer was told.
        order.action_confirm()
        return order, (" ".join(notes) or None)

    @api.model
    def _lexcom_append_order(self, vals, company):
        """Append items to an existing open order. Returns ``(order, message)``.

        Per the specification, items ACCUMULATE and header fields are
        overwritten: "in case of a double submission of the same order-append
        command, this would lead to the number of units of each item to be
        doubled". That is stated as expected behaviour, so no deduplication
        happens here - deliberately unlike order-submit.
        """
        order_id = vals.get("order_id")
        order = self.sudo().search(
            [("name", "=", order_id), ("company_id", "=", company.id)], limit=1
        )
        if not order:
            return self.browse(), _("Unknown order '%s'.") % order_id
        if order.state not in APPENDABLE_STATES or order.locked:
            return self.browse(), _(
                "Order %s is not open for changes."
            ) % order.name

        notes = []
        brand, brand_note = self._lexcom_resolve_brand(vals.get("brand"))
        if brand_note:
            notes.append(brand_note)

        lines, line_notes, matched = self._lexcom_build_lines(
            vals, company, brand
        )
        notes.extend(line_notes)
        if not matched:
            return self.browse(), _(
                "No item in this append could be resolved; the order was left "
                "unchanged. %s"
            ) % " ".join(notes)

        header = {}
        if vals.get("customer_ref"):
            header["client_order_ref"] = vals["customer_ref"]
        if vals.get("order_source_system"):
            header["lexcom_source_system"] = vals["order_source_system"]
        if vals.get("vin"):
            header["lexcom_vin"] = vals["vin"]
        if vals.get("license_plate"):
            header["lexcom_license_plate"] = vals["license_plate"]
        header["order_line"] = lines

        order.sudo().write(header)
        return order, (" ".join(notes) or None)

    # ── idempotency ──────────────────────────────────────────────────────────

    @api.model
    def _lexcom_payload_hash(self, vals):
        """Digest of the payload plus its UTC date.

        Folding the date in means the same customer legitimately re-ordering
        the same basket on another day creates a new order, while a retry
        within the day resolves to the original.
        """
        canonical = json.dumps(vals, sort_keys=True, default=str)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return hashlib.sha256(
            ("%s|%s" % (day, canonical)).encode("utf-8")
        ).hexdigest()

    # ── resolution helpers ───────────────────────────────────────────────────

    @api.model
    def _lexcom_resolve_brand(self, code):
        """product.brand for an Appendix A code, plus a note when unmatched."""
        Brand = self.env["product.brand"].sudo()
        if not code:
            return Brand.browse(), None
        brand = Brand.search([("lexcom_code", "=", code)], limit=1)
        if brand:
            return brand, None
        return Brand.browse(), _(
            "Brand code '%s' is not mapped to a BAF brand."
        ) % code

    @api.model
    def _lexcom_resolve_product(self, article_id, brand):
        """Resolve an article-id, using brand to disambiguate.

        ``product.template.sku`` is documented as unique per brand only, so the
        brand is the disambiguator. Without a usable brand we accept a SKU-only
        match ONLY when it is unique across every brand: guessing which
        manufacturer's part the garage meant would ship the wrong item.
        """
        Product = self.env["product.product"].sudo()
        if not article_id:
            return Product.browse()
        if brand:
            return Product.search(
                [("sku", "=", article_id), ("brand", "=", brand.id)], limit=1
            )
        matches = Product.search([("sku", "=", article_id)], limit=2)
        return matches if len(matches) == 1 else Product.browse()

    @api.model
    def _lexcom_partner_tag(self):
        Category = self.env["res.partner.category"].sudo()
        tag = Category.search([("name", "=", LEXCOM_PARTNER_TAG)], limit=1)
        if not tag:
            tag = Category.create({"name": LEXCOM_PARTNER_TAG})
        return tag

    @api.model
    def _lexcom_partner_vals(self, address, fallback_name):
        name = (
            address.get("company")
            or " ".join(
                p for p in (address.get("first_name"), address.get("last_name")) if p
            ).strip()
            or fallback_name
            or _("LexCom Customer")
        )
        street = address.get("address1") or address.get("po_box")
        country = self.env["baf.integration.mixin"]._baf_country_by_code(
            address.get("country")
        )
        return {
            "name": name,
            "street": street or False,
            "street2": address.get("address2") or False,
            "zip": address.get("postal_code") or False,
            "city": address.get("city") or False,
            "country_id": country.id or False,
            "email": address.get("email") or False,
            "phone": address.get("phone") or False,
        }

    @api.model
    def _lexcom_find_or_create_partner(self, vals):
        """Partner by contact_number, else created from the payload.

        Returns ``(partner, note)``. A created partner carries no contract
        pricing, which is harmless while use-retail-price is true (LexCom's
        default) and means list price when it is false - hence the note, so the
        row is findable for cleanup.
        """
        Partner = self.env["res.partner"].sudo()
        number = vals.get("customer_number")
        if number:
            partner = Partner.search([("contact_number", "=", number)], limit=1)
            if partner:
                return partner, None

        partner_vals = self._lexcom_partner_vals(
            vals.get("billing_address") or {}, vals.get("customer_name")
        )
        partner_vals["category_id"] = [(4, self._lexcom_partner_tag().id)]
        partner = Partner.create(partner_vals)
        return partner, _(
            "Customer number '%(num)s' did not match a contact; created '%(name)s'."
        ) % {"num": number or "", "name": partner.name}

    @api.model
    def _lexcom_delivery_partner(self, parent, vals):
        """Delivery child for the delivery-address block, when one is given."""
        address = vals.get("delivery_address") or {}
        if not any(address.get(k) for k in ("address1", "postal_code", "city")):
            return self.env["res.partner"].browse()
        Partner = self.env["res.partner"].sudo()
        existing = Partner.search([
            ("parent_id", "=", parent.id),
            ("type", "=", "delivery"),
            ("zip", "=", address.get("postal_code")),
            ("street", "=", address.get("address1")),
        ], limit=1)
        if existing:
            return existing
        child_vals = self._lexcom_partner_vals(address, parent.name)
        child_vals.update({"parent_id": parent.id, "type": "delivery"})
        return Partner.create(child_vals)

    # ── lines ────────────────────────────────────────────────────────────────

    @api.model
    def _lexcom_build_lines(self, vals, company, brand):
        """Build order_line commands. Returns ``(commands, notes, matched)``."""
        Mixin = self.env["baf.integration.mixin"]
        use_retail = vals.get("use_retail_price", True)
        commands = []
        notes = []
        matched = 0

        for item in vals.get("items") or []:
            qty = item.get("units") or 0
            if item.get("item_type") == "LABOUR":
                product = Mixin._baf_get_or_create_service_product(
                    LEXCOM_LABOUR_CODE, _("LexCom Labour")
                )
                label = item.get("description") or _("Labour operation %s") % (
                    item.get("operation_id") or ""
                )
            else:
                product = self._lexcom_resolve_product(
                    item.get("article_id"), brand
                )
                label = item.get("description") or item.get("article_id") or ""

            if not product:
                notes.append(
                    _("Article '%s' is unknown.") % (item.get("article_id") or "")
                )
                commands.append((0, 0, {
                    "display_type": "line_note",
                    "name": _(
                        "Unmatched LexCom article %(art)s: %(desc)s "
                        "(qty %(qty)s, price %(price)s)"
                    ) % {
                        "art": item.get("article_id") or "",
                        "desc": item.get("description") or "",
                        "qty": qty,
                        "price": item.get("retail_price")
                        if item.get("retail_price") is not None
                        else item.get("price"),
                    },
                }))
                continue

            line_vals = {
                "product_id": product.id,
                "name": label,
                "product_uom_qty": qty,
            }
            price = self._lexcom_line_price(item, product, vals, use_retail)
            if price is not None:
                line_vals["price_unit"] = price
            taxes = Mixin._baf_tax_ids_for_rate(company, item.get("tax_rate"))
            if taxes:
                line_vals["tax_ids"] = [(6, 0, taxes.ids)]
            commands.append((0, 0, line_vals))
            matched += 1

        return commands, notes, matched

    @api.model
    def _lexcom_line_price(self, item, product, vals, use_retail):
        """Honour the sent price when use-retail-price is true, else re-price.

        ``true`` is the documented default for every LexCom system and means
        partslink24 already showed that number to the garage; silently
        re-pricing would change what the customer agreed to.
        """
        if use_retail:
            if item.get("retail_price") is not None:
                return item["retail_price"]
            if item.get("price") is not None:
                return item["price"]
            return None
        partner = None
        if vals.get("customer_number"):
            partner = self.env["res.partner"].sudo().search(
                [("contact_number", "=", vals["customer_number"])], limit=1
            )
        try:
            return product.baf_get_sales_price(partner or None)
        except Exception:
            _logger.exception(
                "LexCom: pricing engine failed for product %s; falling back to "
                "the sent price", product.default_code or product.id
            )
            return item.get("retail_price") or item.get("price")

    # ── note ─────────────────────────────────────────────────────────────────

    @api.model
    def _lexcom_order_note(self, vals):
        """Payload fields with no dedicated Odoo field, as an HTML note."""
        parts = []
        if vals.get("customer_comment"):
            parts.append(_("Customer comment: %s") % vals["customer_comment"])
        if vals.get("editor"):
            parts.append(_("Editor: %s") % vals["editor"])
        if vals.get("executer"):
            parts.append(_("Executer: %s") % vals["executer"])
        if vals.get("order_type"):
            contract = vals.get("customer_contract")
            parts.append(_("Order type: %s%s") % (
                vals["order_type"],
                _(" (contract %s)") % contract if contract else "",
            ))
        if vals.get("shipping_type"):
            parts.append(_("Shipping type: %s") % vals["shipping_type"])
        if vals.get("desired_shipping_on"):
            parts.append(
                _("Desired shipping date: %s") % vals["desired_shipping_on"]
            )
        if vals.get("mileage"):
            parts.append(_("Mileage: %s %s") % (
                vals["mileage"], vals.get("mileage_unit") or ""
            ))
        for payment in vals.get("payments") or []:
            parts.append(_("Payment %(type)s %(amount)s %(cur)s (%(status)s), "
                           "transaction %(txn)s") % {
                "type": payment.get("type") or "",
                "amount": payment.get("amount") or "",
                "cur": payment.get("currency") or "",
                "status": payment.get("status") or "",
                "txn": payment.get("transaction_id") or "",
            })
        for key, value in vals.get("extensions") or []:
            parts.append(_("Extension %(k)s: %(v)s") % {"k": key, "v": value})
        return "<br/>".join(p for p in parts if p) or False
