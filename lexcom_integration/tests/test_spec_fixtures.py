"""End-to-end tests driven by the specification's own XML examples.

Every other test in this module uses XML I wrote, which means it encodes my
reading of the spec - if I misread it, the test misreads it identically. These
fixtures are transcribed verbatim from the PDF, so they exercise fields no
hand-written payload here touches (total-article / total-labour / total-all,
discount attributes, multi-payment, extension elements) and they arrive over
the real HTTP endpoint rather than being handed to the model as a dict.
"""

import pathlib

from lxml import etree

from odoo.tests.common import HttpCase, tagged

from .common import DEALER_ID, LexcomCommon

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: The spec's examples all use this dealer-id.
SPEC_DEALER_ID = "Dealer1"


def fixture(name):
    return (FIXTURES / name).read_bytes()


@tagged("post_install", "-at_install")
class TestLexcomSpecFixtures(HttpCase, LexcomCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The spec examples carry dealer-id "Dealer1"; accept it for these
        # tests so the payloads can stay byte-for-byte as published.
        cls.company.sudo().lexcom_dealer_id = SPEC_DEALER_ID
        # Item 1 of the spec example is taxed at 0.19.
        cls.tax_19 = cls.env["account.tax"].create({
            "name": "LexCom Spec 19",
            "amount": 19.0,
            "amount_type": "percent",
            "type_tax_use": "sale",
            "company_id": cls.company.id,
        })

    def post(self, command, payload):
        return self.url_open(
            "/lexcom/%s" % command,
            data=payload,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "Authorization": self.basic_auth(),
            },
        )

    # ── commands-available ───────────────────────────────────────────────────

    def test_spec_commands_available_request_is_accepted(self):
        response = self.post(
            "commands-available", fixture("commands_available_request.xml")
        )
        self.assertEqual(response.status_code, 200)
        root = etree.fromstring(response.content)
        self.assertEqual(root.tag, "commands-available-response")
        self.assertEqual(root.get("version"), "3.1")
        self.assertTrue(root.get("dms-name"))

    # ── order-submit ─────────────────────────────────────────────────────────

    def test_spec_order_submit_is_accepted_end_to_end(self):
        response = self.post("order-submit", fixture("order_submit_request.xml"))
        self.assertEqual(response.status_code, 200)
        root = etree.fromstring(response.content)
        self.assertEqual(root.tag, "order-submit-response")
        self.assertEqual(root.findtext("order-accepted"), "true")
        self.assertTrue(root.findtext("order-id"))

    def test_spec_order_submit_response_element_order_matches_spec(self):
        """Element order is mandatory; wrong order is a protocol error.

        The spec's table lists dealer-id, country, order-accepted, order-id,
        customer-ref, message - in that order.
        """
        response = self.post("order-submit", fixture("order_submit_request.xml"))
        root = etree.fromstring(response.content)
        tags = [child.tag for child in root]
        expected_prefix = [
            "dealer-id", "country", "order-accepted", "order-id", "customer-ref",
        ]
        self.assertEqual(tags[:len(expected_prefix)], expected_prefix)
        for tag in tags:
            self.assertNotEqual(
                tag, "", "no empty optional element may be emitted"
            )

    def test_spec_order_submit_echoes_dealer_and_country(self):
        response = self.post("order-submit", fixture("order_submit_request.xml"))
        root = etree.fromstring(response.content)
        self.assertEqual(root.findtext("dealer-id"), SPEC_DEALER_ID)
        self.assertEqual(root.findtext("country"), "DEU")
        self.assertEqual(root.findtext("customer-ref"), "Order #003 for Audi")

    def test_spec_order_submit_creates_the_expected_lines(self):
        """Two of the three spec items resolve here; the third is unknown.

        Article 1H0512345 exists in this fixture set, 1H0523456 does not, and
        item 3 is LABOUR. So: one product line, one labour line, one note.
        """
        self.post("order-submit", fixture("order_submit_request.xml"))
        order = self.env["sale.order"].search(
            [("client_order_ref", "=", "Order #003 for Audi")], limit=1
        )
        self.assertTrue(order, "the spec payload must produce an order")

        products = order.order_line.filtered(lambda l: not l.display_type)
        notes = order.order_line.filtered(
            lambda l: l.display_type == "line_note"
        )
        self.assertEqual(len(products), 2)
        self.assertEqual(len(notes), 1)
        self.assertIn("1H0523456", notes.name)

    def test_spec_order_submit_maps_labour_to_service_product(self):
        self.post("order-submit", fixture("order_submit_request.xml"))
        order = self.env["sale.order"].search(
            [("client_order_ref", "=", "Order #003 for Audi")], limit=1
        )
        labour = order.order_line.filtered(
            lambda l: l.product_id.default_code == "LEXCOM-LABOUR"
        )
        self.assertTrue(labour, "item 3 of the spec example is LABOUR")
        self.assertEqual(labour.product_uom_qty, 3)

    def test_spec_order_submit_stamps_vehicle_and_source(self):
        self.post("order-submit", fixture("order_submit_request.xml"))
        order = self.env["sale.order"].search(
            [("client_order_ref", "=", "Order #003 for Audi")], limit=1
        )
        self.assertEqual(order.lexcom_vin, "ABCDE26Y213112345")
        self.assertEqual(order.lexcom_license_plate, "M-LC 100")
        self.assertEqual(order.lexcom_source_system, "partslink24")

    def test_spec_order_submit_records_payments_and_extensions(self):
        """Fields with no Odoo equivalent must survive into the note."""
        self.post("order-submit", fixture("order_submit_request.xml"))
        order = self.env["sale.order"].search(
            [("client_order_ref", "=", "Order #003 for Audi")], limit=1
        )
        self.assertIn("creditcard", order.note)
        self.assertIn("coupon", order.note)
        self.assertIn("lc-huer7asdf-1", order.note)
        self.assertIn("DmsSpecificKey1", order.note)
        self.assertIn("Shipping address has changed", order.note)

    def test_spec_order_submit_applies_the_item_tax_rate(self):
        self.post("order-submit", fixture("order_submit_request.xml"))
        order = self.env["sale.order"].search(
            [("client_order_ref", "=", "Order #003 for Audi")], limit=1
        )
        taxed = order.order_line.filtered(
            lambda l: l.product_id == self.product
        )
        self.assertIn(self.tax_19, taxed.tax_ids)

    def test_spec_order_submit_replay_is_idempotent(self):
        first = self.post("order-submit", fixture("order_submit_request.xml"))
        second = self.post("order-submit", fixture("order_submit_request.xml"))
        self.assertEqual(second.status_code, 200)
        first_id = etree.fromstring(first.content).findtext("order-id")
        second_id = etree.fromstring(second.content).findtext("order-id")
        self.assertEqual(first_id, second_id)
        self.assertEqual(
            self.env["sale.order"].search_count(
                [("client_order_ref", "=", "Order #003 for Audi")]
            ),
            1,
        )

    # ── order-append ─────────────────────────────────────────────────────────

    def _append_payload_for(self, order_name):
        """The spec's append example, retargeted at a real order id."""
        root = etree.fromstring(fixture("order_append_request.xml"))
        root.find("order-id").text = order_name
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    def test_spec_order_append_is_accepted_end_to_end(self):
        self.post("order-submit", fixture("order_submit_request.xml"))
        order = self.env["sale.order"].search(
            [("client_order_ref", "=", "Order #003 for Audi")], limit=1
        )
        before = sum(
            order.order_line.filtered(lambda l: not l.display_type).mapped(
                "product_uom_qty"
            )
        )

        response = self.post(
            "order-append", self._append_payload_for(order.name)
        )
        self.assertEqual(response.status_code, 200)
        root = etree.fromstring(response.content)
        self.assertEqual(root.tag, "order-append-response")
        self.assertEqual(root.findtext("order-accepted"), "true")
        self.assertEqual(root.findtext("order-id"), order.name)

        after = sum(
            order.order_line.filtered(lambda l: not l.display_type).mapped(
                "product_uom_qty"
            )
        )
        self.assertGreater(after, before)

    def test_spec_order_append_accumulates_on_double_submission(self):
        """The spec states double submission doubles the units."""
        self.post("order-submit", fixture("order_submit_request.xml"))
        order = self.env["sale.order"].search(
            [("client_order_ref", "=", "Order #003 for Audi")], limit=1
        )
        payload = self._append_payload_for(order.name)

        self.post("order-append", payload)
        once = sum(
            order.order_line.filtered(lambda l: not l.display_type).mapped(
                "product_uom_qty"
            )
        )
        self.post("order-append", payload)
        twice = sum(
            order.order_line.filtered(lambda l: not l.display_type).mapped(
                "product_uom_qty"
            )
        )
        self.assertEqual(twice - once, once - 6)
