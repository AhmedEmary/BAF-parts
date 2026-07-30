import copy
import json
import os
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "latest_orders.json")

# Product SKUs (supplier_item_number) referenced by the fixture positions.
FIXTURE_SKUS = [
    "T2H42447",
    "T2R23864",
    "JDE38595",
    "JDE41598",
    # Land Rover orders POE22521340726 (negative handling fee),
    # POE14350330726 (multi-position, no fees), POE28118200726 (single
    # position with DHL tracking) and POE19280160726 (repeated position name).
    "LR030371",
    "LR077218",
    "LR167008",
    "LR082315",
    "LR138360",
    "LR118375",
    "LR054870",
    "LR054871",
]


@tagged("post_install", "-at_install")
class TestAlzuraImport(TransactionCase):
    _uid = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(FIXTURE) as fh:
            cls.orders = json.load(fh)["data"]

        cls.company = cls.env.company
        cls.SaleOrder = cls.env["sale.order"]
        cls.alzura_source = cls.env.ref("alzura_integration.so_source_alzura")

        # One product per fixture SKU so positions resolve. Services keep the
        # test free of warehouse/stock setup while still allowing confirmation.
        cls.products = {
            sku: cls.env["product.product"].create(
                {"name": "Alzura %s" % sku, "sku": sku, "type": "service"}
            )
            for sku in FIXTURE_SKUS
        }

    @staticmethod
    def _unique_vat(seed):
        """A German VAT with a valid MOD 11,10 check digit.

        base_vat validates the checksum, so the buyer VAT cannot simply be a
        counter; it has to be a well-formed number to reach partner creation.
        """
        digits = "9%07d" % seed
        product = 10
        for digit in digits:
            total = (int(digit) + product) % 10 or 10
            product = (2 * total) % 11
        return "DE%s%s" % (digits, (11 - product) % 10)

    def _identify(self, data, seed):
        """Stamp a unique buyer identity onto a fixture order.

        The fixture is a real payload, so on a database that already imported
        these orders the buyers exist and _alzura_find_or_create_partner
        matches them by phone / VAT — the partner assertions would then read a
        record written by an older import instead of the one under test.
        """
        buyer = data.setdefault("buyer", {})
        buyer["id"] = 990000000 + seed
        buyer.setdefault("contact", {})["phone"] = "+4999%08d" % seed
        buyer.setdefault("tax", {})["sales_tax_identification_number"] = (
            self._unique_vat(seed)
        )
        return data

    def _order(self, index):
        """A deep copy of a fixture order with a unique order ref and buyer,
        so the SO and partner de-dup never collide with pre-existing data.
        """
        type(self)._uid += 1
        data = copy.deepcopy(self.orders[index])
        data["order"] = "TEST-ALZ-%s" % self._uid
        return self._identify(data, self._uid)

    def _import(self, index):
        return self.SaleOrder._alzura_import_order(self.company, self._order(index))

    def _charge_lines(self, order):
        return order.order_line.filtered(
            lambda l: l.product_id.default_code == "ALZURA-CHARGE"
        )

    # --- order header / confirmation ------------------------------------

    def test_import_creates_confirmed_sale_order(self):
        order = self._import(0)
        self.assertTrue(order)
        self.assertEqual(order.state, "sale", "order must be confirmed, not draft")
        self.assertTrue(order.b2b_so.startswith("TEST-ALZ-"))
        self.assertEqual(order.so_source, self.alzura_source)

    def test_idempotent_reimport(self):
        data = self._order(0)
        first = self.SaleOrder._alzura_import_order(self.company, data)
        self.assertTrue(first)
        second = self.SaleOrder._alzura_import_order(self.company, data)
        self.assertFalse(second, "re-importing the same order must be skipped")
        self.assertEqual(
            self.SaleOrder.search_count([("b2b_so", "=", data["order"])]), 1
        )

    def test_alzura_internal_number_imported(self):
        # cart_order_id is the number the buyer sees in the Alzura frontend.
        order = self._import(0)
        self.assertEqual(order.alzura_internal_number, "4285747")
        # With no buyer reference it becomes the customer reference, so the
        # default "Order" search box finds the SO by the number customers quote.
        self.assertEqual(order.client_order_ref, "4285747")
        found = self.SaleOrder.search(
            [("client_order_ref", "ilike", "4285747"), ("id", "=", order.id)]
        )
        self.assertEqual(found, order)

    def test_is_alzura_order_flag(self):
        # Drives visibility of the Alzura fields in the form/list views.
        order = self._import(0)
        self.assertTrue(order.is_alzura_order)
        plain = self.SaleOrder.create(
            {
                "partner_id": self.env["res.partner"]
                .create({"name": "Plain Customer"})
                .id
            }
        )
        self.assertFalse(plain.is_alzura_order)

    def test_buyer_reference_still_wins_as_customer_ref(self):
        data = self._order(0)
        data["reference_number"] = "CUSTOMER-REF-7"
        order = self.SaleOrder._alzura_import_order(self.company, data)
        self.assertEqual(order.client_order_ref, "CUSTOMER-REF-7")
        self.assertEqual(order.alzura_internal_number, "4285747")

    # --- position lines --------------------------------------------------

    def test_position_line_mapping(self):
        order = self._import(0)
        # Order 0 has a single position and no fees, so one non-charge line.
        line = order.order_line - self._charge_lines(order)
        self.assertEqual(len(line), 1)
        self.assertEqual(line.product_id.sku, "T2H42447")
        self.assertEqual(line.product_uom_qty, 2)
        self.assertAlmostEqual(line.price_unit, 43.7)

    def test_unmatched_product_kept_as_note_line(self):
        data = self._order(0)
        data["positions"][0]["supplier_item_number"] = "DOES-NOT-EXIST"
        order = self.SaleOrder._alzura_import_order(self.company, data)
        self.assertTrue(order, "order must still import despite an unmatched SKU")
        note = order.order_line.filtered(lambda l: l.display_type == "line_note")
        self.assertEqual(len(note), 1)
        self.assertIn("DOES-NOT-EXIST", note.name)

    # --- fee / charge lines ---------------------------------------------

    def test_no_charge_lines_when_total_matches(self):
        # Order 0: no shipping/payment fees, positions already equal total_sum.
        order = self._import(0)
        self.assertFalse(self._charge_lines(order))

    def test_shipping_fee_line(self):
        # Order 1: shipping method net 3.90, positions 20.52, total_sum 24.42.
        order = self._import(1)
        fee = order.order_line.filtered(lambda l: l.name == "Shipping fee")
        self.assertEqual(len(fee), 1)
        self.assertAlmostEqual(fee.price_unit, 3.90)
        # No leftover gap, so no alzura_charge reconciliation line.
        self.assertFalse(
            order.order_line.filtered(lambda l: l.name == "alzura_charge")
        )

    def test_alzura_charge_reconciles_gap(self):
        data = self._order(2)
        # Inflate the reported total so positions + fees fall 5.00 short.
        data["total_sum"]["net"] = data["total_sum"]["net"] + 5.0
        order = self.SaleOrder._alzura_import_order(self.company, data)
        charge = order.order_line.filtered(lambda l: l.name == "alzura_charge")
        self.assertEqual(len(charge), 1)
        self.assertAlmostEqual(charge.price_unit, 5.0)

    def test_negative_handling_fee_becomes_a_credit_line(self):
        # POE22521340726: positions 74.10, handling fee -6.00, total_sum 68.10.
        # The fee is a discount Alzura granted the buyer, so it has to reach
        # the order as a negative line instead of being dropped.
        order = self._import(4)
        products = order.order_line - self._charge_lines(order)
        self.assertEqual(len(products), 3)
        self.assertAlmostEqual(sum(products.mapped("price_subtotal")), 74.10, places=2)

        fee = order.order_line.filtered(lambda l: l.name == "Shipping fee")
        self.assertEqual(len(fee), 1)
        self.assertAlmostEqual(fee.price_unit, -6.00)
        self.assertAlmostEqual(fee.price_subtotal, -6.00)
        self.assertFalse(
            order.order_line.filtered(lambda l: l.name == "alzura_charge"),
            "positions plus the fee already match total_sum",
        )
        self.assertAlmostEqual(order.amount_untaxed, 68.10, places=2)

    def test_multi_position_order_without_fees(self):
        # POE14350330726: two positions, no shipping/payment fee, no gap.
        order = self._import(5)
        self.assertFalse(self._charge_lines(order))
        self.assertEqual(len(order.order_line), 2)
        self.assertEqual(
            sorted(order.order_line.mapped("product_id.sku")),
            ["LR082315", "LR138360"],
        )
        self.assertAlmostEqual(order.amount_untaxed, 31.48, places=2)

    # --- totals / repricing protection ------------------------------------

    def test_untaxed_total_matches_alzura_total_sum(self):
        for index in range(len(self.orders)):
            data = self._order(index)
            order = self.SaleOrder._alzura_import_order(self.company, data)
            self.assertAlmostEqual(
                order.amount_untaxed,
                data["total_sum"]["net"],
                places=2,
                msg="order %s must bill exactly Alzura's total_sum.net" % index,
            )

    def test_tax_comes_from_the_payload_vat(self):
        # Every fixture position states vat 0.19; with a matching 19 % sale tax
        # on the company, amount_total must land on total_sum.gross.
        tax = self.env["account.tax"].create(
            {
                "name": "Alzura Test VAT 19",
                "amount": 19.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": self.company.id,
            }
        )
        # A real database already holds 19 % taxes, and the resolver prefers
        # the company's configured one, so point it at the tax under test.
        self.company.account_sale_tax_id = tax
        data = self._order(4)
        order = self.SaleOrder._alzura_import_order(self.company, data)

        for line in order.order_line:
            self.assertEqual(line.tax_ids, tax, "line %s untaxed" % line.name)
        self.assertAlmostEqual(
            order.amount_total, data["total_sum"]["gross"], places=2
        )

    def test_every_fixture_order_matches_alzura_gross(self):
        # Alzura rounds VAT per line (HALF-UP), so the company must round the
        # same way for amount_total to land on total_sum.gross. Two fixture
        # orders only agree under round_per_line: POE14350330726 (14.50 x 19 %
        # is an exact half-cent tie) and POE19280160726.
        self.company.tax_calculation_rounding_method = "round_per_line"
        self.env["account.tax"].create(
            {
                "name": "Alzura Test VAT 19",
                "amount": 19.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": self.company.id,
            }
        )
        for index in range(len(self.orders)):
            data = self._order(index)
            order = self.SaleOrder._alzura_import_order(self.company, data)
            self.assertAlmostEqual(
                order.amount_total,
                data["total_sum"]["gross"],
                places=2,
                msg="order %s (%s) gross" % (index, data["order"]),
            )

    def test_ambiguous_rate_uses_the_company_sale_tax(self):
        # Real databases hold several taxes at the same rate ("19%" beside
        # "19% EU D"); the company's configured one must win instead of
        # whichever the search happens to return first.
        def make(name, sequence):
            return self.env["account.tax"].create(
                {
                    "name": name,
                    "amount": 19.0,
                    "amount_type": "percent",
                    "type_tax_use": "sale",
                    "sequence": sequence,
                    "company_id": self.company.id,
                }
            )

        self.env["account.tax"].search(
            [("type_tax_use", "=", "sale"), ("amount", "=", 19.0)]
        ).write({"active": False})
        first = make("Alzura VAT 19 first", 1)
        chosen = make("Alzura VAT 19 chosen", 99)
        self.company.account_sale_tax_id = chosen

        order = self._import(5)
        for line in order.order_line:
            self.assertEqual(line.tax_ids, chosen, "line %s" % line.name)
        self.assertNotIn(first, order.order_line.tax_ids)

    def test_missing_tax_rate_falls_back_to_product_default(self):
        # No 19 % sale tax available: the import must still succeed rather
        # than invent accounting configuration. Archived instead of deleted,
        # since a real database has these taxes in use on posted entries.
        self.env["account.tax"].search(
            [("type_tax_use", "=", "sale"), ("amount", "=", 19.0)]
        ).write({"active": False})
        order = self._import(5)
        self.assertTrue(order)
        self.assertAlmostEqual(order.amount_untaxed, 31.48, places=2)

    def test_lines_are_exempt_from_baf_repricing(self):
        order = self._import(0)
        product = order.order_line.product_id[:1]
        for line in order.order_line:
            self.assertTrue(line._baf_skip_repricing())

        plain = self.SaleOrder.create(
            {
                "partner_id": order.partner_id.id,
                "order_line": [(0, 0, {"product_id": product.id})],
            }
        )
        self.assertFalse(plain.order_line._baf_skip_repricing())

    def test_duplicate_alzura_source_still_counts_as_alzura(self):
        # Databases carry hand-made duplicates of the Alzura source; an order
        # stamped with one must not escape the repricing guard.
        order = self._import(0)
        duplicate = self.env["so.source"].create({"name": self.alzura_source.name})
        order.so_source = duplicate
        self.assertTrue(order.is_alzura_order)
        self.assertTrue(order.order_line[:1]._baf_skip_repricing())

    def test_price_survives_a_dependency_write(self):
        # Writing a _compute_baf_price dependency must not re-derive the price
        # from the BAF catalog; the Alzura payload stays authoritative.
        data = self._order(1)
        order = self.SaleOrder._alzura_import_order(self.company, data)
        fee = order.order_line.filtered(lambda l: l.name == "Shipping fee")
        line = (order.order_line - self._charge_lines(order))[:1]
        before = line.price_unit

        line.write({"product_uom_qty": line.product_uom_qty + 1})

        self.assertAlmostEqual(line.price_unit, before)
        self.assertAlmostEqual(fee.price_unit, 3.90)
        self.assertAlmostEqual(fee.price_subtotal, 3.90)

    # --- partner extraction ---------------------------------------------

    def test_partner_full_data_on_create(self):
        data = self._order(0)
        order = self.SaleOrder._alzura_import_order(self.company, data)
        partner = order.partner_id
        self.assertTrue(partner.ref.startswith("ALZURA-"))
        self.assertEqual(partner.street, "Eschauer Allee 12")
        self.assertEqual((partner.street2 or "").strip(), "Lona Speed Service")
        self.assertEqual(partner.city, "Goldscheuer")
        self.assertEqual(partner.zip, "77694")
        self.assertEqual(
            partner.vat, data["buyer"]["tax"]["sales_tax_identification_number"]
        )
        self.assertEqual(partner.phone, data["buyer"]["contact"]["phone"])
        self.assertEqual(partner.country_id.code, "DE")
        # Status and credit-reform enrichment kept in the internal notes.
        self.assertIn("Premium", partner.comment or "")
        self.assertIn("ACHTUNG", partner.comment or "")

    def test_partner_named_after_billing_recipient(self):
        # address.name is the billing recipient (the business for B2B buyers);
        # contact.name is only the person ordering and goes to the notes.
        order = self._import(0)
        partner = order.partner_id
        self.assertEqual(partner.name, "Abduramani Nurij")
        self.assertIn("Contact person: Nurij Abduramani", partner.comment or "")

    def test_partner_tagged_alzura_tyre24(self):
        order = self._import(0)
        self.assertIn(
            "Alzura / Tyre24", order.partner_id.category_id.mapped("name")
        )
        self.assertNotIn(
            "Alzura Buyer", order.partner_id.category_id.mapped("name")
        )

    def test_existing_contact_tags_its_company_too(self):
        company = self.env["res.partner"].create(
            {"name": "Preexisting Garage GmbH", "is_company": True}
        )
        data = self._order(0)
        contact = self.env["res.partner"].create(
            {
                "name": "Preexisting Contact",
                "parent_id": company.id,
                "phone": data["buyer"]["contact"]["phone"],
            }
        )
        order = self.SaleOrder._alzura_import_order(self.company, data)
        # Matched by phone: the tag must land on the contact and its company.
        self.assertIn("Alzura / Tyre24", contact.category_id.mapped("name"))
        self.assertIn("Alzura / Tyre24", company.category_id.mapped("name"))
        self.assertEqual(order.partner_id, contact)

    def test_same_buyer_across_orders_reuses_one_partner(self):
        # Fixture orders 2 and 3 are two orders from the same Alzura buyer.
        type(self)._uid += 1
        seed = self._uid
        orders = self.SaleOrder.browse()
        for index in (2, 3):
            data = self._identify(copy.deepcopy(self.orders[index]), seed)
            data["order"] = "TEST-ALZ-%s-%s" % (seed, index)
            orders |= self.SaleOrder._alzura_import_order(self.company, data)
        self.assertEqual(len(orders), 2)
        self.assertEqual(len(orders.partner_id), 1, "one buyer, one partner")

    def test_cooperation_becomes_the_order_partner(self):
        # A buyer inside a cooperation orders on the cooperation's behalf, so
        # the cooperation is the customer and the buyer becomes its contact.
        data = self._order(0)
        data["buyer"]["cooperation"] = {
            "number": "COOP-%s" % self._uid,
            "name": "Alzura Test Cooperation %s" % self._uid,
        }
        order = self.SaleOrder._alzura_import_order(self.company, data)
        self.assertTrue(order.partner_id.is_company)
        self.assertEqual(order.partner_id.ref, "ALZURA-COOP-COOP-%s" % self._uid)
        contact = self.env["res.partner"].search(
            [("ref", "=", "ALZURA-%s" % data["buyer"]["id"])], limit=1
        )
        self.assertEqual(contact.parent_id, order.partner_id)

    def test_masked_email_not_stored(self):
        order = self._import(0)
        # Alzura returns a message URL instead of a real email.
        self.assertFalse(order.partner_id.email)

    def test_partner_bank_created(self):
        order = self._import(0)
        bank = order.partner_id.bank_ids
        self.assertEqual(len(bank), 1)
        self.assertEqual(bank.acc_number.replace(" ", ""), "DE42664518620000112764")
        self.assertEqual(bank.acc_holder_name, "Nurij Abduramani")
        self.assertEqual(bank.bank_id.bic, "SOLADES1KEL")

    # --- full batch via the fetch entrypoint ----------------------------

    def test_fetch_orders_batch(self):
        body = {"data": [self._order(i) for i in range(4)]}
        with patch.object(
            type(self.SaleOrder), "_alzura_orders_payload", return_value=body
        ):
            result = self.SaleOrder._alzura_fetch_orders(self.company)
        self.assertEqual(result["created"], 4)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(result["total"], 4)

        # Re-running the same payload imports nothing new (idempotent on b2b_so).
        with patch.object(
            type(self.SaleOrder), "_alzura_orders_payload", return_value=body
        ):
            again = self.SaleOrder._alzura_fetch_orders(self.company)
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["skipped"], 4)
