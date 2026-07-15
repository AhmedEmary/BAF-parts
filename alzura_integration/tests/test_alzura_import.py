import copy
import json
import os
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "latest_orders.json")

# Product SKUs (supplier_item_number) referenced by the fixture positions.
FIXTURE_SKUS = ["T2H42447", "T2R23864", "JDE38595", "JDE41598"]


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

    def _order(self, index):
        """A deep copy of a fixture order with a unique order ref and buyer id,
        so the SO and partner de-dup never collide with pre-existing data.
        """
        type(self)._uid += 1
        data = copy.deepcopy(self.orders[index])
        data["order"] = "TEST-ALZ-%s" % self._uid
        data.setdefault("buyer", {})["id"] = 990000000 + self._uid
        return data

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

    # --- partner extraction ---------------------------------------------

    def test_partner_full_data_on_create(self):
        order = self._import(0)
        partner = order.partner_id
        self.assertTrue(partner.ref.startswith("ALZURA-"))
        self.assertEqual(partner.street, "Eschauer Allee 12")
        self.assertEqual((partner.street2 or "").strip(), "Lona Speed Service")
        self.assertEqual(partner.city, "Goldscheuer")
        self.assertEqual(partner.zip, "77694")
        self.assertEqual(partner.vat, "DE250459671")
        self.assertEqual(partner.phone, "+4978549872295")
        self.assertEqual(partner.country_id.code, "DE")
        # Status and credit-reform enrichment kept in the internal notes.
        self.assertIn("Premium", partner.comment or "")
        self.assertIn("ACHTUNG", partner.comment or "")

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
