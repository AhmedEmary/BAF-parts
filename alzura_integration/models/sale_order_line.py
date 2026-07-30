from odoo import models


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    def _baf_skip_repricing(self):
        """Alzura lines carry the marketplace's own net prices.

        The importer writes price_unit straight from the payload so the order
        total matches Alzura's total_sum. Letting a catalog lookup run would
        replace those with pricelist / BAF prices and flatten the shipping /
        payment / alzura_charge lines to 0.00, because their service product
        has no list price.
        """
        self.ensure_one()
        return self.order_id.is_alzura_order or super()._baf_skip_repricing()

    def _alzura_protected(self):
        return self.filtered(lambda line: line._baf_skip_repricing())

    def _compute_price_unit(self):
        # Core recomputes from the pricelist on any qty / product / partner
        # write, which would drop the Alzura price. Guarded here rather than
        # in b2b_custom so the protection holds without that module too.
        protected = self._alzura_protected()
        super(SaleOrderLine, self - protected)._compute_price_unit()

    def _compute_discount(self):
        protected = self._alzura_protected()
        protected.discount = 0.0
        super(SaleOrderLine, self - protected)._compute_discount()
