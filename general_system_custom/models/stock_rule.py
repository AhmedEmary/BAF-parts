from odoo import models

class StockRule(models.Model):
    _inherit = 'stock.rule'

    def _get_custom_move_fields(self):
        fields = super()._get_custom_move_fields()
        fields.append('reserved_qty_custom')
        return fields

    def _get_stock_move_values(self, product_id, product_qty, product_uom, location_id, name, origin, company_id, values):
        res = super()._get_stock_move_values(product_id, product_qty, product_uom, location_id, name, origin, company_id, values)
        if values.get('reserved_qty_custom'):
            res['reserved_qty_custom'] = values.get('reserved_qty_custom')
        return res
