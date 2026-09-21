"""Requests captured on the wire from LexCom's own test client.

These are the exact bytes the jar's "Check specification conformity" run sends
(fixtures/README.md). Every test applies the client's pass rule: HTTP 200, a
``text/xml; charset=utf-8`` content type, a body valid against LexCom's schema,
and dealer-id / country (plus order-id on append) echoed back. The error
variants are the client's own: ``X_UNKNOWN_X`` swapped into the dealer-id or
customer-number of the full request.
"""

import pathlib

from lxml import etree

from odoo.tests.common import HttpCase, tagged

from .common import LexcomCommon

JAR = pathlib.Path(__file__).parent / "fixtures" / "jar"
JAR_DEALER_ID = "TEST001"
UNKNOWN = "X_UNKNOWN_X"

#: The client sends this version on purpose and skips schema validation for it.
COMMANDS_AVAILABLE = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    b'<commands-available version="0.0"/>\n'
)


def jar_request(name):
    return etree.fromstring((JAR / name).read_bytes())


def insert_after(root, anchor, tag, text):
    """Insert <tag>text</tag> after <anchor>, keeping the schema's order."""
    element = etree.Element(tag)
    element.text = text
    root.find(anchor).addnext(element)
    return root


@tagged("post_install", "-at_install")
class TestLexcomJarConformity(HttpCase, LexcomCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.sudo().lexcom_dealer_id = JAR_DEALER_ID
        # The client's article; its BMW brand stays unmapped, as on staging.
        cls.jar_product = cls.env["product.product"].create({
            "name": "Diskette Modic Programmierung",
            "sku": "01139787863",
            "list_price": 99.09,
        })

    def post(self, command, root_or_bytes):
        payload = root_or_bytes
        if isinstance(payload, etree._Element):
            payload = etree.tostring(payload, xml_declaration=True, encoding="UTF-8")
        response = self.url_open(
            "/lexcom/%s" % command,
            data=payload,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "Authorization": self.basic_auth(),
            },
        )
        self.assertEqual(response.status_code, 200)
        content_type = response.headers["Content-Type"].replace(" ", "").lower()
        self.assertTrue(content_type.startswith("text/xml"), content_type)
        self.assertTrue(content_type.endswith("charset=utf-8"), content_type)
        return self.assert_lexcom_schema(response.content)

    def assert_echoes(self, root, order_id=None):
        self.assertEqual(root.findtext("dealer-id"), JAR_DEALER_ID)
        self.assertEqual(root.findtext("country"), "DEU")
        if order_id:
            self.assertEqual(root.findtext("order-id"), order_id)

    def variants(self, name):
        """The client's item/payment combinations, cut from its full request."""
        def without(*paths):
            root = jar_request(name)
            for path in paths:
                for node in root.findall(path):
                    root.remove(node)
            return root

        labour = "item[item-type='LABOUR']"
        article = "item[item-type='ARTICLE']"
        return {
            "payment, item and labour": jar_request(name),
            "item": without("payments", labour),
            "labour": without("payments", article),
            "payment": without("item"),
            "bare": without("payments", "item"),
        }

    def submitted_order_id(self):
        root = self.post("order-submit", jar_request("order_submit_request.xml"))
        return root.findtext("order-id")

    # ── commands-available / order-list ──────────────────────────────────────

    def test_commands_available_with_version_0_0(self):
        root = self.post("commands-available", COMMANDS_AVAILABLE)
        advertised = {el.text for el in root.findall("command")}
        self.assertTrue({"order-list", "order-append"} <= advertised)

    def test_order_list(self):
        self.submitted_order_id()
        root = self.post("order-list", jar_request("order_list_request.xml"))
        self.assert_echoes(root)
        self.assertTrue(root.findall("order"))

    # ── order-submit ─────────────────────────────────────────────────────────

    def test_order_submit_variants(self):
        for label, request in self.variants("order_submit_request.xml").items():
            with self.subTest(label):
                root = self.post("order-submit", request)
                self.assert_echoes(root)
                self.assertEqual(
                    root.findtext("order-accepted"),
                    "false" if label in ("bare", "payment") else "true",
                )

    def test_order_submit_keeps_both_items_sharing_item_id_0(self):
        order_id = self.submitted_order_id()
        order = self.env["sale.order"].search([("name", "=", order_id)])
        products = order.order_line.product_id
        self.assertEqual(len(order.order_line), 2)
        self.assertIn(self.jar_product, products)
        self.assertIn("LEXCOM-LABOUR", products.mapped("default_code"))

    def test_order_submit_records_the_payment(self):
        order_id = self.submitted_order_id()
        order = self.env["sale.order"].search([("name", "=", order_id)])
        self.assertIn("Payment creditcard 42.0 EUR (paid), transaction TXN-1",
                      self.lexcom_note(order))

    def test_order_submit_without_customer_number_adds_no_contact(self):
        """The client never sends a customer-number; runs must not pile up contacts."""
        before = self.env["res.partner"].search_count([])
        for request in self.variants("order_submit_request.xml").values():
            self.post("order-submit", request)
        self.assertEqual(self.env["res.partner"].search_count([]), before)

    def test_order_submit_unknown_dealer_is_an_error(self):
        request = jar_request("order_submit_request.xml")
        request.find("dealer-id").text = UNKNOWN
        self.assertEqual(self.post("order-submit", request).tag, "error")

    def test_order_submit_unknown_customer_is_accepted(self):
        """The client WARNs here ("is this intentional?"); it is, by decision."""
        request = insert_after(
            jar_request("order_submit_request.xml"), "country",
            "customer-number", UNKNOWN,
        )
        root = self.post("order-submit", request)
        self.assertEqual(root.findtext("order-accepted"), "true")
        self.assertTrue(self.env["res.partner"].search(
            [("lexcom_customer_number", "=", UNKNOWN)]
        ))

    # ── order-append ─────────────────────────────────────────────────────────

    def test_order_append_variants(self):
        order_id = self.submitted_order_id()
        for label, request in self.variants("order_append_request.xml").items():
            with self.subTest(label):
                request.find("order-id").text = order_id
                root = self.post("order-append", request)
                self.assert_echoes(root, order_id)
                self.assertEqual(
                    root.findtext("order-accepted"),
                    "false" if label in ("bare", "payment") else "true",
                )

    def test_order_append_unknown_dealer_is_an_error(self):
        request = jar_request("order_append_request.xml")
        request.find("order-id").text = self.submitted_order_id()
        request.find("dealer-id").text = UNKNOWN
        self.assertEqual(self.post("order-append", request).tag, "error")

    def test_order_append_unknown_customer_is_accepted(self):
        """Append targets an order, not a customer; the client WARNs here too."""
        order_id = self.submitted_order_id()
        request = insert_after(
            jar_request("order_append_request.xml"), "brand",
            "customer-number", UNKNOWN,
        )
        request.find("order-id").text = order_id
        root = self.post("order-append", request)
        self.assert_echoes(root, order_id)
        self.assertEqual(root.findtext("order-accepted"), "true")
