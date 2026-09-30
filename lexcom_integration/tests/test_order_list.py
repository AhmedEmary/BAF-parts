from datetime import timedelta

from lxml import etree

from odoo import fields
from odoo.tests.common import tagged

from .common import DEALER_ID, LexcomCommon
from ..models.lexcom_protocol import LexcomProtocolError

#: Child order of an order-list-response <order>, per orderlist-response.xsd.
ORDER_ELEMENT_ORDER = [
    "order-id", "creation-date", "editable", "status", "brand",
    "customer-number", "customer-ref", "customer-name", "billing-address",
    "delivery-address", "vin", "license-plate", "vehicle-registration",
    "mileage", "order-type",
]


@tagged("post_install", "-at_install")
class TestLexcomOrderList(LexcomCommon):

    def setUp(self):
        super().setUp()
        self.partner.write({
            "street": "Alleeweg 1",
            "zip": "80331",
            "city": "Munich",
            "country_id": self.env.ref("base.de").id,
        })
        self.order, _msg = self.env["sale.order"]._lexcom_build_order(
            self.order_vals(), self.company
        )
        self.assertTrue(self.order)

    def _list(self, **filters):
        root = etree.Element("order-list", version="3.1")
        values = {"dealer-id": DEALER_ID, "country": "DEU", "brand": "Audi"}
        values.update(filters)
        for tag in ("dealer-id", "country", "customer-number", "brand",
                    "order-id", "customer-name", "vin", "license-plate",
                    "creation-date", "vehicle-registration"):
            if values.get(tag) is not None:
                etree.SubElement(root, tag).text = values[tag]
        element, outcome, _message, _order = self.env["lexcom.protocol"]._dispatch(
            "order-list", root, self.company
        )
        self.assertEqual(outcome, "ok")
        self.assert_lexcom_schema(element)
        return element

    def _ids(self, element):
        return [el.findtext("order-id") for el in element.findall("order")]

    def test_lists_open_order_with_spec_fields_in_order(self):
        element = self._list()
        self.assertEqual(element.tag, "order-list-response")
        self.assertEqual(element.get("version"), "3.1")
        self.assertEqual(element.findtext("dealer-id"), DEALER_ID)
        self.assertEqual(element.findtext("country"), "DEU")
        self.assertIn(self.order.name, self._ids(element))

        node = next(
            el for el in element.findall("order")
            if el.findtext("order-id") == self.order.name
        )
        tags = [child.tag for child in node]
        self.assertEqual(
            tags, [t for t in ORDER_ELEMENT_ORDER if t in tags],
            "children must follow the specification's element order",
        )
        self.assertEqual(node.findtext("editable"), "true")
        self.assertEqual(node.findtext("status"), "sale")
        self.assertEqual(node.findtext("brand"), "Audi")
        self.assertEqual(node.findtext("customer-number"), "LEX-CUST-1")
        self.assertEqual(node.findtext("customer-ref"), "Order #003 for Audi")
        self.assertEqual(node.findtext("customer-name"), "Autocenter Meier (test)")
        self.assertEqual(node.findtext("vin"), "ABCDE26Y213112345")
        self.assertEqual(node.findtext("license-plate"), "M-LC 100")
        self.assertEqual(
            node.findtext("creation-date"),
            fields.Date.to_string(self.order.create_date.date()),
        )
        lines = [el.text for el in node.findall("billing-address/address-line")]
        self.assertEqual(lines, ["Alleeweg 1", "80331", "Munich", "Germany"])

    def test_empty_optional_elements_are_omitted(self):
        self.order.write({
            "client_order_ref": False,
            "lexcom_vin": False,
            "lexcom_license_plate": False,
        })
        node = next(
            el for el in self._list().findall("order")
            if el.findtext("order-id") == self.order.name
        )
        for tag in ("customer-ref", "vin", "license-plate"):
            self.assertIsNone(node.find(tag), tag)

    def test_customer_number_filter(self):
        other = self.env["res.partner"].create({
            "name": "Other garage", "contact_number": "LEX-CUST-2",
        })
        other_order, _msg = self.env["sale.order"]._lexcom_build_order(
            self.order_vals(customer_number="LEX-CUST-2",
                            customer_ref="other"), self.company
        )
        self.assertEqual(other_order.partner_id, other)

        ids = self._ids(self._list(**{"customer-number": "LEX-CUST-1"}))
        self.assertIn(self.order.name, ids)
        self.assertNotIn(other_order.name, ids)

    def test_unknown_customer_number_lists_nothing(self):
        element = self._list(**{"customer-number": "NO-SUCH-CUSTOMER"})
        self.assertEqual(element.findall("order"), [])

    def test_customer_number_matches_lexcom_number_of_created_contact(self):
        order, _msg = self.env["sale.order"]._lexcom_build_order(
            self.order_vals(customer_number="DMS-777", customer_ref="new"),
            self.company,
        )
        element = self._list(**{"customer-number": "DMS-777"})
        self.assertEqual(self._ids(element), [order.name])
        self.assertEqual(
            element.find("order").findtext("customer-number"), "DMS-777"
        )

    def test_closed_orders_are_not_listed(self):
        """Every listed order must be one order-append will accept."""
        self.order.locked = True
        self.assertNotIn(self.order.name, self._ids(self._list()))
        self.order.locked = False
        self.order._action_cancel()
        self.assertNotIn(self.order.name, self._ids(self._list()))

    def test_manual_order_from_another_channel_is_listed(self):
        """order-list searches the whole DMS, not only LexCom's own orders."""
        manual = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "company_id": self.company.id,
        })
        node = next(
            el for el in self._list().findall("order")
            if el.findtext("order-id") == manual.name
        )
        self.assertEqual(node.findtext("status"), "draft")
        self.assertEqual(node.findtext("customer-number"), "LEX-CUST-1")

    def test_list_is_not_capped(self):
        """The spec sets no upper bound, so every open order is returned."""
        self.env["sale.order"].create([
            {"partner_id": self.partner.id, "company_id": self.company.id}
            for _i in range(101)
        ])
        ids = self._ids(self._list(**{"customer-number": "LEX-CUST-1"}))
        self.assertEqual(len(ids), 102)

    def test_order_id_filter(self):
        self.assertEqual(
            self._ids(self._list(**{"order-id": self.order.name})),
            [self.order.name],
        )
        self.assertEqual(self._ids(self._list(**{"order-id": "S-NOPE"})), [])

    def test_customer_name_wildcard(self):
        self.assertIn(
            self.order.name,
            self._ids(self._list(**{"customer-name": "Autocenter*"})),
        )
        self.assertIn(
            self.order.name,
            self._ids(self._list(**{"customer-name": "autocenter meier (test)"})),
        )
        self.assertNotIn(
            self.order.name,
            self._ids(self._list(**{"customer-name": "Autocenter"})),
            "without a wildcard the name must match exactly",
        )

    def test_customer_name_percent_is_literal(self):
        self.assertNotIn(
            self.order.name, self._ids(self._list(**{"customer-name": "%"}))
        )

    def test_vehicle_filters(self):
        self.assertIn(
            self.order.name,
            self._ids(self._list(vin="ABCDE26Y213112345",
                                 **{"license-plate": "M-LC 100"})),
        )
        self.assertNotIn(
            self.order.name, self._ids(self._list(vin="WRONGVIN"))
        )

    def test_creation_date_filter(self):
        today = fields.Date.to_string(self.order.create_date.date())
        yesterday = fields.Date.to_string(
            self.order.create_date.date() - timedelta(days=1)
        )
        self.assertIn(
            self.order.name, self._ids(self._list(**{"creation-date": today}))
        )
        self.assertNotIn(
            self.order.name, self._ids(self._list(**{"creation-date": yesterday}))
        )

    def test_malformed_creation_date_is_refused(self):
        with self.assertRaises(LexcomProtocolError):
            self._list(**{"creation-date": "29.09.2026"})

    def test_listed_order_is_appendable(self):
        order_id = self._ids(self._list(**{"customer-number": "LEX-CUST-1"}))[0]
        order, _msg = self.env["sale.order"]._lexcom_append_order(
            self.order_vals(order_id=order_id), self.company
        )
        self.assertTrue(order)

    def test_unknown_dealer_is_refused(self):
        with self.assertRaises(LexcomProtocolError):
            self._list(**{"dealer-id": "NOT-OURS"})

    def test_brand_is_mandatory(self):
        root = etree.fromstring(
            b'<order-list version="3.1"><dealer-id>BAF1</dealer-id>'
            b"<country>DEU</country></order-list>"
        )
        with self.assertRaises(LexcomProtocolError):
            self.env["lexcom.protocol"]._dispatch("order-list", root, self.company)
