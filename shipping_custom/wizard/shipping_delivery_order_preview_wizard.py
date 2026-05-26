import json
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShippingDeliveryOrderPreviewWizard(models.TransientModel):
    _name = 'shipping.delivery.order.preview.wizard'
    _description = 'Shipping Delivery Order Payload Preview'

    order_id = fields.Many2one(
        'shipping.delivery.order',
        string='Delivery Order',
        required=True,
        ondelete='cascade',
    )
    endpoint = fields.Char(
        string='Endpoint', readonly=True,
        compute='_compute_preview', store=False,
    )
    payload_json = fields.Text(
        string='Payload', readonly=True,
        compute='_compute_preview', store=False,
    )

    @api.depends('order_id')
    def _compute_preview(self):
        for wiz in self:
            if not wiz.order_id:
                wiz.endpoint = ''
                wiz.payload_json = ''
                continue
            try:
                client = wiz.order_id.provider_account_id.get_api_client()
                payload = client.build_shipment_payload(wiz.order_id)
                wiz.payload_json = json.dumps(payload, indent=2, default=str)
                wiz.endpoint = '%s%s' % (
                    getattr(client, 'base_url', ''),
                    '/shipments' if wiz.order_id.provider == 'dhl'
                    else '/ship/v1/shipments',
                )
            except Exception as e:
                _logger.exception("Failed to build preview payload")
                wiz.payload_json = "Error building payload:\n%s" % str(e)
                wiz.endpoint = ''

    def action_confirm_send(self):
        self.ensure_one()
        if not self.order_id:
            raise UserError(_("No delivery order linked to this preview."))
        order = self.order_id
        order.action_generate_label()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shipping.delivery.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }
