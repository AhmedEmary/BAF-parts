from odoo import models, fields
from odoo.exceptions import UserError


class MassVendorWizard(models.TransientModel):
    _name = 'mass.vendor.wizard'
    _description = 'Mass Assign Vendor to Sale Order Lines'

    vendor_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        required=True,
        domain=[('supplier_rank', '>', 0)],
    )

    def action_apply_vendor(self):
        self.ensure_one()
        active_ids = self.env.context.get('active_ids', [])
        if not active_ids:
            raise UserError("No sale order lines selected.")
        self.env['sale.order.line'].browse(active_ids).write(
            {'purchase_vendor_id': self.vendor_id.id}
        )
        return {'type': 'ir.actions.act_window_close'}
