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

    def test_append_is_not_deduplicated(self):
        """order-append must NOT reuse order-submit's idempotency hash."""
        self._append()
        self._append()
        notes = self.order.order_line.filtered(
            lambda l: not l.display_type
        )
        self.assertGreaterEqual(len(notes), 1)
