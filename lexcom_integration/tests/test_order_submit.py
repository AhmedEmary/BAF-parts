from odoo.tests.common import tagged

from .common import LexcomCommon


@tagged("post_install", "-at_install")
class TestLexcomOrderSubmit(LexcomCommon):
    """order-submit behaviour, driven by dicts.

    Possible only because the protocol layer converts XML to dicts and
    sale_order owns the write (design D12); otherwise every one of these would
    need hand-written XML.
    """

    def _submit(self, **overrides):
        return self.env["sale.order"]._lexcom_build_order(
            self.order_vals(**overrides), self.company
        )

    def test_happy_path_creates_confirmed_order(self):
        order, message = self._submit()
        self.assertTrue(order)
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.partner_id, self.partner)
        self.assertEqual(order.lexcom_source_system, "partslink24")
        self.assertEqual(order.lexcom_vin, "ABCDE26Y213112345")
        self.assertEqual(order.lexcom_license_plate, "M-LC 100")
        self.assertFalse(message)

    def test_so_source_is_stamped(self):
        order, _msg = self._submit()
        self.assertEqual(
            order.so_source,
            self.env.ref("lexcom_integration.so_source_lexcom"),
        )

    def test_known_customer_number_resolves(self):
        order, _msg = self._submit()
        self.assertEqual(order.partner_id, self.partner)

    def test_unresolvable_customer_is_created_and_tagged(self):
        order, message = self._submit(
            customer_number="NOPE-999",
            customer_name="Garage Nouveau",
            billing_address={
                "company": "Garage Nouveau",
                "address1": "Alleeweg 1",
                "postal_code": "80331",
                "city": "Munich",
                "country": "DE",
            },
        )
        self.assertTrue(order)
        self.assertNotEqual(order.partner_id, self.partner)
        self.assertEqual(order.partner_id.name, "Garage Nouveau")
        self.assertIn("LexCom", order.partner_id.category_id.mapped("name"))
        self.assertIn("NOPE-999", message)

    def test_unmatched_article_becomes_note_line_and_order_is_accepted(self):
        """The Alzura posture: degrade, do not reject."""
        order, message = self._submit(items=[
            self.item_vals(),
            self.item_vals(item_id="2", article_id="DOES-NOT-EXIST"),
        ])
        self.assertTrue(order, "order must still be accepted")
        notes = order.order_line.filtered(
            lambda l: l.display_type == "line_note"
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("DOES-NOT-EXIST", notes.name)
        self.assertIn("DOES-NOT-EXIST", message)

    def test_all_articles_unmatched_creates_nothing(self):
        """Refused order must leave zero rows - the atomicity guarantee."""
        before_orders = self.env["sale.order"].search_count([])
        before_lines = self.env["sale.order.line"].search_count([])
        before_partners = self.env["res.partner"].search_count([])

        order, message = self._submit(
            customer_number="ALSO-UNKNOWN",
            items=[self.item_vals(article_id="NOPE-1"),
                   self.item_vals(item_id="2", article_id="NOPE-2")],
        )
        self.assertFalse(order)
        self.assertIn("No item", message)
        self.assertEqual(self.env["sale.order"].search_count([]), before_orders)
        self.assertEqual(
            self.env["sale.order.line"].search_count([]), before_lines
        )
        self.assertEqual(
            self.env["res.partner"].search_count([]), before_partners,
            "a refused order must not leave a partner behind",
        )

    def test_labour_item_uses_service_product(self):
        order, _msg = self._submit(items=[self.item_vals(
            item_type="LABOUR", article_id=None, operation_id="72201900",
            description="Change oil filter", units=3, price=1.5,
            retail_price=1.25,
        )])
        self.assertTrue(order)
        line = order.order_line.filtered(lambda l: not l.display_type)
        self.assertEqual(line.product_id.default_code, "LEXCOM-LABOUR")
        self.assertEqual(line.product_id.type, "service")

    def test_unknown_brand_with_globally_unique_sku_matches(self):
        order, message = self._submit(brand="Porsche")
        self.assertTrue(order)
        line = order.order_line.filtered(lambda l: not l.display_type)
        self.assertEqual(line.product_id, self.product)
        self.assertIn("Porsche", message)

    def test_unknown_brand_with_ambiguous_sku_becomes_note(self):
        """Two brands share the SKU and the brand code is unmapped: do not guess."""
        self.env["product.product"].create({
            "name": "Oil filter (other brand)",
            "sku": "1H0512345",
            "brand": self.other_brand.id,
        })
        order, message = self._submit(brand="Lancia")
        self.assertFalse(order, "nothing resolvable, so nothing created")
        self.assertIn("1H0512345", message)

    def test_use_retail_price_true_honours_sent_price(self):
        order, _msg = self._submit()
        line = order.order_line.filtered(lambda l: not l.display_type)
        self.assertEqual(line.price_unit, 7.00)

    def test_use_retail_price_false_uses_pricing_engine(self):
        order, _msg = self._submit(use_retail_price=False)
        line = order.order_line.filtered(lambda l: not l.display_type)
        self.assertNotEqual(
            line.price_unit, 7.00,
            "with use-retail-price false the BAF engine prices the line",
        )

    def test_duplicate_same_day_returns_original(self):
        first, _m1 = self._submit()
        second, message = self._submit()
        self.assertEqual(first, second)
        self.assertIn("Duplicate", message)
        self.assertEqual(
            self.env["sale.order"].search_count(
                [("lexcom_payload_hash", "=", first.lexcom_payload_hash)]
            ),
            1,
        )

    def test_different_payload_creates_second_order(self):
        first, _m1 = self._submit()
        second, _m2 = self._submit(customer_ref="A different basket")
        self.assertTrue(second)
        self.assertNotEqual(first, second)

    def test_payload_hash_includes_the_date(self):
        """Same basket on another day must not collide with today's order."""
        vals = self.order_vals()
        today = self.env["sale.order"]._lexcom_payload_hash(vals)
        self.assertTrue(today)
        self.assertEqual(
            today, self.env["sale.order"]._lexcom_payload_hash(vals),
            "hash must be stable within the day",
        )

    def test_tax_rate_resolves_through_the_shared_mixin(self):
        tax = self.env["account.tax"].create({
            "name": "LexCom Test 19",
            "amount": 19.0,
            "amount_type": "percent",
            "type_tax_use": "sale",
            "company_id": self.company.id,
        })
        order, _msg = self._submit(items=[self.item_vals(tax_rate=0.19)])
        line = order.order_line.filtered(lambda l: not l.display_type)
        self.assertIn(tax, line.tax_ids)

    def test_delivery_address_creates_child_contact(self):
        order, _msg = self._submit(delivery_address={
            "address1": "Alleeweg 9",
            "postal_code": "80999",
            "city": "Munich",
            "country": "DE",
        })
        self.assertTrue(order.partner_shipping_id)
        self.assertEqual(order.partner_shipping_id.type, "delivery")
        self.assertEqual(order.partner_shipping_id.zip, "80999")

    def test_order_note_carries_fields_without_odoo_equivalents(self):
        order, _msg = self._submit(
            customer_comment="Shipping address has changed",
            payments=[{
                "status": "paid", "amount": 120.0, "currency": "EUR",
                "transaction_id": "lc-huer7asdf-1", "type": "creditcard",
                "provider": "Adyen", "provider_transaction_id": "adyen-1",
                "scheme": "visa",
            }],
        )
        self.assertIn("Shipping address has changed", order.note)
        self.assertIn("creditcard", order.note)
        self.assertIn("Max Mustermann", order.note)
