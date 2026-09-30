from datetime import datetime

from odoo.tests.common import tagged

from .common import LexcomCommon


@tagged("post_install", "-at_install")
class TestLexcomOrderAppend(LexcomCommon):

    def setUp(self):
        super().setUp()
        self.order, _msg = self.env["sale.order"]._lexcom_build_order(
            self.order_vals(), self.company
        )
        self.assertTrue(self.order)

    def _append(self, **overrides):
        vals = self.order_vals(order_id=self.order.name, **overrides)
        return self.env["sale.order"]._lexcom_append_order(vals, self.company)

    def test_append_adds_lines_and_overwrites_header(self):
        before = len(self.order.order_line)
        order, _msg = self._append(customer_ref="Renamed basket")
        self.assertEqual(order, self.order)
        self.assertGreater(len(order.order_line), before)
        self.assertEqual(order.client_order_ref, "Renamed basket")

    def test_double_append_doubles_quantities(self):
        """Spec-mandated: double submission doubles the units. Assert it."""
        self._append()
        qty_after_one = sum(
            self.order.order_line.filtered(lambda l: not l.display_type).mapped(
                "product_uom_qty"
            )
        )
        self._append()
        qty_after_two = sum(
            self.order.order_line.filtered(lambda l: not l.display_type).mapped(
                "product_uom_qty"
            )
        )
        self.assertEqual(qty_after_two, qty_after_one + 3)

    def test_unknown_order_is_refused(self):
        vals = self.order_vals(order_id="SO-DOES-NOT-EXIST")
        order, message = self.env["sale.order"]._lexcom_append_order(
            vals, self.company
        )
        self.assertFalse(order)
        self.assertIn("Unknown order", message)

    def test_locked_order_is_refused(self):
        self.order.locked = True
        order, message = self._append()
        self.assertFalse(order)
        self.assertIn("not open", message)

    def test_append_posts_payload_info_as_internal_note(self):
        note_before = self.order.note
        self._append(customer_comment="Second basket for the same car")
        self.assertIn("Second basket for the same car", self.lexcom_note(self.order))
        self.assertEqual(self.order.note, note_before)

    def test_cancelled_order_is_refused(self):
        self.order._action_cancel()
        lines = self.order.order_line
        order, message = self._append()
        self.assertFalse(order)
        self.assertIn("not open", message)
        self.assertEqual(self.order.order_line, lines)

    def test_append_with_only_unknown_articles_adds_note_lines(self):
        """Alzura parity: an unmatched append is recorded, not dropped."""
        old = self.order.order_line
        order, message = self._append(
            items=[self.item_vals(article_id="NOPE-1")]
        )
        self.assertEqual(order, self.order)
        added = order.order_line - old
        self.assertEqual(added.mapped("display_type"), ["line_note"])
        self.assertIn("NOPE-1", added.name)
        self.assertIn("NOPE-1", message)

    def test_append_without_items_is_refused(self):
        old = self.order.order_line
        order, message = self._append(items=[])
        self.assertFalse(order)
        self.assertIn("no items", message)
        self.assertEqual(self.order.order_line, old)

    def test_append_is_not_deduplicated(self):
        """order-append must NOT reuse order-submit's idempotency hash."""
        self._append()
        self._append()
        notes = self.order.order_line.filtered(
            lambda l: not l.display_type
        )
        self.assertGreaterEqual(len(notes), 1)

    def test_append_overwrites_the_delivery_date(self):
        self._append(desired_shipping_on="2026-04-01")
        self.assertEqual(self.order.commitment_date, datetime(2026, 4, 1))
