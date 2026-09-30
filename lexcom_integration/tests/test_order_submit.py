from datetime import datetime

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

    def test_new_customer_keeps_lexcom_customer_number(self):
        order, _msg = self._submit(
            customer_number="DMS-777", customer_name="Garage Neu"
        )
        self.assertEqual(order.partner_id.lexcom_customer_number, "DMS-777")

    def test_returning_unknown_customer_reuses_created_contact(self):
        """A garage BAF did not know must not be duplicated on its next order."""
        first, _m1 = self._submit(customer_number="DMS-777", customer_ref="one")
        second, message = self._submit(customer_number="DMS-777", customer_ref="two")
        self.assertNotEqual(first, second)
        self.assertEqual(second.partner_id, first.partner_id)
        self.assertNotIn("did not match", message or "")

    def test_orders_without_customer_number_share_one_fallback_contact(self):
        """The spec leaves customer-number "empty for non-customers"."""
        fallback = self.env.ref("lexcom_integration.partner_lexcom_no_customer_number")
        partners_before = self.env["res.partner"].search_count([])
        first, _m1 = self._submit(customer_number=None, customer_ref="one")
        second, message = self._submit(customer_number=None, customer_ref="two")
        self.assertEqual(first.partner_id, fallback)
        self.assertEqual(second.partner_id, fallback)
        self.assertEqual(self.env["res.partner"].search_count([]), partners_before)
        self.assertNotIn("created", message or "")

    def _submit_without_number(self, **address):
        return self._submit(
            customer_number=None, customer_ref=str(address), billing_address=address,
        )

    def test_no_number_matches_unique_email(self):
        garage = self.env["res.partner"].create({
            "name": "Garage Mail", "email": "Info@Garage-Mail.de",
        })
        order, message = self._submit_without_number(email=" info@garage-mail.de ")
        self.assertEqual(order.partner_id, garage)
        self.assertIn("Garage Mail", message)

    def test_no_number_email_treats_underscore_literally(self):
        self.env["res.partner"].create({
            "name": "Wrong Garage", "email": "werkXstatt@example.com",
        })
        order, _msg = self._submit_without_number(email="werk_statt@example.com")
        self.assertEqual(
            order.partner_id,
            self.env.ref("lexcom_integration.partner_lexcom_no_customer_number"),
        )

    def test_no_number_email_of_a_person_resolves_their_company(self):
        company = self.env["res.partner"].create({
            "name": "Garage Group", "is_company": True,
        })
        self.env["res.partner"].create({
            "name": "Max", "parent_id": company.id, "email": "max@garage-group.de",
        })
        order, _msg = self._submit_without_number(email="max@garage-group.de")
        self.assertEqual(order.partner_id, company)

    def test_no_number_matches_unique_phone_in_any_format(self):
        garage = self.env["res.partner"].create({
            "name": "Garage Phone", "phone": "+49 89 1234567",
            "country_id": self.env.ref("base.de").id,
        })
        order, message = self._submit_without_number(phone="089 123 45 67", country="DE")
        self.assertEqual(order.partner_id, garage)
        self.assertIn("Garage Phone", message)

    def test_no_number_ambiguous_email_falls_through_to_phone(self):
        for name in ("Garage A", "Garage B"):
            self.env["res.partner"].create({"name": name, "email": "shop@shared.de"})
        garage = self.env["res.partner"].create({
            "name": "Garage C", "phone": "+49 30 7654321",
            "country_id": self.env.ref("base.de").id,
        })
        order, _msg = self._submit_without_number(
            email="shop@shared.de", phone="+49 30 7654321",
        )
        self.assertEqual(order.partner_id, garage)

    def test_no_number_ambiguous_phone_uses_shared_contact(self):
        for name in ("Garage D", "Garage E"):
            self.env["res.partner"].create({
                "name": name, "phone": "+49 40 5550000",
                "country_id": self.env.ref("base.de").id,
            })
        order, _msg = self._submit_without_number(phone="+49 40 5550000")
        self.assertEqual(
            order.partner_id,
            self.env.ref("lexcom_integration.partner_lexcom_no_customer_number"),
        )

    def test_fallback_order_keeps_who_ordered_in_internal_note(self):
        order, _msg = self._submit(
            customer_number=None,
            customer_name="Walk-in Garage",
            billing_address={
                "company": "Walk-in Garage", "address1": "Teststrasse 1",
                "postal_code": "80331", "city": "Munich", "country": "DE",
                "email": "walkin@example.com",
            },
        )
        note = self.lexcom_note(order)
        for text in ("Walk-in Garage", "Teststrasse 1", "80331 Munich",
                     "walkin@example.com"):
            self.assertIn(text, note)

    def test_known_customer_note_does_not_repeat_the_address(self):
        order, _msg = self._submit(
            billing_address={"company": "Autocenter Meier", "address1": "Alleeweg 1"}
        )
        self.assertNotIn("Alleeweg 1", self.lexcom_note(order))

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

    def test_all_articles_unmatched_still_creates_confirmed_order(self):
        """Alzura parity: nothing matched still records the request as notes."""
        order, message = self._submit(
            customer_number="ALSO-UNKNOWN",
            items=[self.item_vals(article_id="NOPE-1"),
                   self.item_vals(item_id="2", article_id="NOPE-2")],
        )
        self.assertTrue(order, "the garage's request must not be dropped")
        self.assertEqual(order.state, "sale")
        self.assertEqual(
            order.order_line.mapped("display_type"), ["line_note", "line_note"]
        )
        self.assertIn("NOPE-1", message)
        self.assertIn("NOPE-2", message)
        self.assertIn("ALSO-UNKNOWN", message)

    def test_order_without_items_creates_nothing(self):
        """No item at all leaves nothing to record: zero rows, no partner."""
        before_orders = self.env["sale.order"].search_count([])
        before_partners = self.env["res.partner"].search_count([])

        order, message = self._submit(customer_number="ALSO-UNKNOWN", items=[])
        self.assertFalse(order)
        self.assertIn("no items", message)
        self.assertEqual(self.env["sale.order"].search_count([]), before_orders)
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
        self.assertFalse(
            order.order_line.filtered(lambda l: not l.display_type),
            "an ambiguous SKU must not be guessed into a product line",
        )
        self.assertIn("1H0512345", order.order_line.name)
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
        note = self.lexcom_note(order)
        self.assertIn("Shipping address has changed", note)
        self.assertIn("creditcard", note)
        self.assertIn("Max Mustermann", note)

    def test_payload_info_leaves_terms_and_conditions_alone(self):
        """sale.order.note is the T&C printed on the customer's documents."""
        manual = self.env["sale.order"].create({"partner_id": self.partner.id})
        order, _msg = self._submit(customer_comment="Shipping address has changed")
        self.assertEqual(order.note, manual.note)
        self.assertNotIn("Max Mustermann", str(order.note or ""))

    def test_payload_info_is_an_internal_note_and_escaped(self):
        order, _msg = self._submit(customer_comment="<b>bold</b> & more")
        notes = order.message_ids.filtered(
            lambda m: m.subtype_id == self.env.ref("mail.mt_note")
        )
        self.assertTrue(notes)
        self.assertTrue(all(m.subtype_id.internal for m in notes))
        self.assertIn("&lt;b&gt;bold&lt;/b&gt; &amp; more", self.lexcom_note(order))

    def test_item_comment_goes_on_the_line_description(self):
        order, _msg = self._submit(items=[
            self.item_vals(customer_comment="green color"),
        ])
        line = order.order_line.filtered(lambda l: not l.display_type)
        self.assertEqual(line.name, "Oil filter\nCustomer comment: green color")

    def test_unmatched_item_comment_goes_on_the_note_line(self):
        order, _msg = self._submit(items=[
            self.item_vals(article_id="DOES-NOT-EXIST", customer_comment="green"),
        ])
        note = order.order_line.filtered(lambda l: l.display_type == "line_note")
        self.assertIn("DOES-NOT-EXIST", note.name)
        self.assertIn("Customer comment: green", note.name)

    def test_desired_shipping_date_is_the_delivery_date(self):
        order, _msg = self._submit(desired_shipping_on="2026-03-24")
        self.assertEqual(order.commitment_date, datetime(2026, 3, 24))
        self.assertNotIn("Desired shipping date", self.lexcom_note(order))

    def test_desired_shipping_utc_datetime_is_the_delivery_date(self):
        order, _msg = self._submit(desired_shipping_on="2026-03-24T10:30:00Z")
        self.assertEqual(order.commitment_date, datetime(2026, 3, 24, 10, 30))

    def test_unreadable_desired_shipping_date_stays_in_the_note(self):
        order, _msg = self._submit(desired_shipping_on="24.03.2026")
        self.assertTrue(order)
        self.assertFalse(order.commitment_date)
        self.assertIn("Desired shipping date: 24.03.2026", self.lexcom_note(order))

    def _labour(self, **overrides):
        vals = dict(item_id="2", item_type="LABOUR", article_id=None,
                    operation_id="72201900", description="Change oil filter",
                    units=3, price=1.5, retail_price=1.25)
        vals.update(overrides)
        return self.item_vals(**vals)

    def test_labour_after_article_keeps_its_subtotal(self):
        """Catalog repricing used to zero this line while the total counted it."""
        order, _msg = self._submit(items=[self.item_vals(), self._labour()])
        self.env.flush_all()
        labour = order.order_line.filtered(
            lambda l: l.product_id.default_code == "LEXCOM-LABOUR"
        )
        self.assertEqual(labour.price_unit, 1.25)
        self.assertAlmostEqual(labour.price_subtotal, 3.75)
        for line in order.order_line:
            self.assertAlmostEqual(
                line.price_subtotal, line.price_unit * line.product_uom_qty
            )
        self.assertAlmostEqual(
            sum(order.order_line.mapped("price_subtotal")), order.amount_untaxed
        )

    def test_quantity_change_keeps_the_imported_price(self):
        order, _msg = self._submit()
        self.assertTrue(order.is_lexcom_order)
        line = order.order_line.filtered(lambda l: not l.display_type)
        line.product_uom_qty = 5
        self.assertEqual(line.price_unit, 7.00, "list price is 10.0; sent is 7.00")

    def test_whole_lexcom_order_is_protected_like_alzura(self):
        order, _msg = self._submit()
        manual = self.env["sale.order.line"].create({
            "order_id": order.id, "product_id": self.product.id,
        })
        self.assertTrue(manual._baf_skip_repricing())

    def test_order_from_another_channel_is_not_protected(self):
        other = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "order_line": [(0, 0, {"product_id": self.product.id})],
        })
        self.assertFalse(other.is_lexcom_order)
        self.assertFalse(other.order_line._baf_skip_repricing())

    def test_no_sent_price_uses_the_baf_price(self):
        order, _msg = self._submit(items=[
            self.item_vals(price=None, retail_price=None),
        ])
        line = order.order_line.filtered(lambda l: not l.display_type)
        self.assertEqual(
            line.price_unit, self.product.baf_get_sales_price(self.partner)
        )
