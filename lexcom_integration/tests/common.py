import base64
import pathlib

from lxml import etree

from odoo.tests.common import TransactionCase

USERNAME = "lexcom-test"
PASSWORD = "s3cret-pass"
DEALER_ID = "BAF1"

XSD_DIR = pathlib.Path(__file__).parent / "fixtures" / "xsd"

#: Response root tag -> LexCom's own schema for it (fixtures/xsd/README.md).
RESPONSE_SCHEMAS = {
    "commands-available-response": "commands-available-response.xsd",
    "order-submit-response": "ordersubmit-response.xsd",
    "order-append-response": "orderappend-response.xsd",
    "order-list-response": "orderlist-response.xsd",
    "error": "error-response.xsd",
}
_schema_cache = {}


class LexcomCommon(TransactionCase):
    """Shared fixture: configured credentials, a mapped brand, a known article."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().write({
            "lexcom_dealer_id": DEALER_ID,
            "lexcom_username": USERNAME,
            "lexcom_enabled": True,
            "lexcom_country": "DEU",
        })
        cls.company.sudo()._lexcom_set_password(PASSWORD)

        cls.brand = cls.env["product.brand"].create({
            "name": "AUDI-TEST",
            "lexcom_code": "Audi",
        })
        cls.other_brand = cls.env["product.brand"].create({
            "name": "VW-TEST",
            "lexcom_code": "Volkswagen",
        })
        cls.product = cls.env["product.product"].create({
            "name": "Oil filter (test)",
            "sku": "1H0512345",
            "brand": cls.brand.id,
            "list_price": 10.0,
        })
        cls.partner = cls.env["res.partner"].create({
            "name": "Autocenter Meier (test)",
            "contact_number": "LEX-CUST-1",
        })

    @classmethod
    def basic_auth(cls, username=USERNAME, password=PASSWORD):
        raw = "%s:%s" % (username, password)
        return "Basic " + base64.b64encode(raw.encode()).decode()

    def assert_lexcom_schema(self, response):
        """Validate a response against LexCom's XSD; returns the parsed root.

        The schema is what the LexCom client itself enforces, so it checks
        element order, occurrence and value patterns in one place.
        """
        root = response
        if not isinstance(response, etree._Element):
            root = etree.fromstring(response)
        name = RESPONSE_SCHEMAS[root.tag]
        if name not in _schema_cache:
            _schema_cache[name] = etree.XMLSchema(etree.parse(str(XSD_DIR / name)))
        schema = _schema_cache[name]
        self.assertTrue(
            schema.validate(root),
            "%s violates LexCom's schema: %s" % (root.tag, schema.error_log.last_error),
        )
        return root

    def lexcom_note(self, order):
        """Text of the internal chatter notes LexCom posted on ``order``."""
        notes = order.message_ids.filtered(
            lambda m: m.subtype_id == self.env.ref("mail.mt_note")
        )
        return " ".join(str(m.body) for m in notes)

    def order_vals(self, **overrides):
        """A minimal parsed order-submit payload, as lexcom.protocol emits."""
        vals = {
            "dealer_id": DEALER_ID,
            "country": "DEU",
            "order_id": None,
            "customer_number": "LEX-CUST-1",
            "customer_name": "Autocenter Meier (test)",
            "customer_ref": "Order #003 for Audi",
            "voucher_id": None,
            "brand": "Audi",
            "vin": "ABCDE26Y213112345",
            "license_plate": "M-LC 100",
            "mileage": 234123,
            "mileage_unit": "km",
            "use_retail_price": True,
            "order_source_system": "partslink24",
            "order_type": "Warranty",
            "customer_contract": "123",
            "shipping_type": "Express",
            "desired_shipping_on": "2026-03-24",
            "currency": "EUR",
            "editor": "Max Mustermann",
            "executer": "MaxMus",
            "customer_comment": None,
            "extensions": [],
            "payments": [],
            "billing_address": {},
            "delivery_address": {},
            "items": [self.item_vals()],
        }
        vals.update(overrides)
        return vals

    def item_vals(self, **overrides):
        item = {
            "item_id": "1",
            "item_type": "ARTICLE",
            "article_id": "1H0512345",
            "operation_id": None,
            "description": "Oil filter",
            "manufacturer": "Manu1",
            "units": 3,
            "price": 7.89,
            "retail_price": 7.00,
            "retail_price_source": "DMS",
            "tax_rate": None,
            "vin": None,
            "customer_comment": None,
        }
        item.update(overrides)
        return item
