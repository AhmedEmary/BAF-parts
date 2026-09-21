from odoo import models


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    def _baf_skip_repricing(self):
        """LexCom orders keep the prices the import resolved.

        That is the sent retail price, or the BAF price the import already
        looked up. Repricing afterwards zeroed the stored subtotal of LABOUR
        lines, whose service product has no list price, while the order total
        still counted them. Scoped to the order, as Alzura does.
        """
        self.ensure_one()
        return self.order_id.is_lexcom_order or super()._baf_skip_repricing()

    def _lexcom_protected(self):
        return self.filtered(lambda line: line._baf_skip_repricing())

    def _compute_price_unit(self):
        # Core recomputes from the pricelist on any qty / product / partner
        # write; guarded here so the protection holds without b2b_custom too.
        protected = self._lexcom_protected()
        super(SaleOrderLine, self - protected)._compute_price_unit()

    def _compute_discount(self):
        protected = self._lexcom_protected()
        protected.discount = 0.0
        super(SaleOrderLine, self - protected)._compute_discount()
