import logging

from odoo import models, fields, api, Command, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PickingShippingOption(models.Model):
    _name = 'picking.shipping.option'
    _description = 'Available Shipping Rates for a Picking'

    picking_id = fields.Many2one(
        'stock.picking', string="Transfer", ondelete='cascade',
    )
    provider_account_id = fields.Many2one(
        'shipping.provider.account', string="Shipping Account",
    )
    raw_service_type = fields.Char(string="Raw Service Type")
    raw_packaging_type = fields.Char(string="Raw Packaging Type")
    service_name = fields.Char(string="Service Name")
    delivery_time = fields.Char(string="Estimated Delivery Time")
    cost = fields.Monetary(string="Shipping Cost", currency_field='currency_id')
    currency_id = fields.Many2one(related='picking_id.shipping_currency_id')

    def action_select_rate(self):
        self.ensure_one()
        picking = self.picking_id

        pallets = picking.shipping_package_ids
        if not pallets:
            raise UserError(_("No pallets configured on this transfer."))

        if self.provider_account_id.provider == 'fedex':
            label_format = self.provider_account_id.fedex_label_stock_type
        else:
            label_format = (
                self.provider_account_id.dhl_label_format or 'ECOM26_A4_001'
            )

        shipper = picking.company_id.partner_id
        recipient = picking.partner_id

        order = self.env['shipping.delivery.order'].create({
            'picking_id': picking.id,
            'provider_account_id': self.provider_account_id.id,
            'service_type': self.raw_service_type,
            'service_name': self.service_name,
            'packaging_type': self.raw_packaging_type or False,
            'label_format': label_format,
            'freight_charge': self.cost,
            'currency_id': picking.shipping_currency_id.id,
            'declared_value_currency_id': picking.shipping_currency_id.id,
        })
        order._populate_from_partner('shipper', shipper)
        order._populate_from_partner('recipient', recipient)
        order._populate_packages_from_pallets(pallets)

        return {
            'type': 'ir.actions.act_window',
            'name': _('Shipping Delivery Order'),
            'res_model': 'shipping.delivery.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }


class ShippingPickingPackage(models.Model):
    """Lightweight pallet/package row attached to a stock.picking.

    Holds the per-shipment package dimensions/weight that the carrier API
    needs. Defaults are pulled from the picked `stock.package.type` record,
    but each field is overridable per row.
    """
    _name = 'shipping.picking.package'
    _description = 'Shipping Pallet on Stock Picking'

    picking_id = fields.Many2one(
        'stock.picking', string="Transfer",
        required=True, ondelete='cascade', index=True,
    )
    package_type_id = fields.Many2one(
        'stock.package.type', string="Package Type", required=True,
    )
    name = fields.Char(string="Reference")
    weight = fields.Float(string="Weight (kg)", default=1.0)
    length = fields.Float(string="Length (cm)", default=1.0)
    width = fields.Float(string="Width (cm)", default=1.0)
    height = fields.Float(string="Height (cm)", default=1.0)
    tracking_number = fields.Char(
        string="Tracking Number", readonly=True, copy=False,
    )

    @api.onchange('package_type_id')
    def _onchange_package_type_id(self):
        for rec in self:
            pt = rec.package_type_id
            if not pt:
                continue
            if pt.packaging_length:
                rec.length = pt.packaging_length
            if pt.width:
                rec.width = pt.width
            if pt.height:
                rec.height = pt.height
            if pt.base_weight:
                rec.weight = pt.base_weight
            if not rec.name:
                rec.name = pt.name


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    shipping_package_ids = fields.One2many(
        'shipping.picking.package', 'picking_id',
        string="Shipping Pallets", copy=False,
        compute='_compute_shipping_package_ids',
        store=True, readonly=False,
    )
    shipping_option_ids = fields.One2many(
        'picking.shipping.option', 'picking_id',
        string="Shipping Options", copy=False,
    )
    selected_shipping_service = fields.Char(
        string="Selected Shipping Service", readonly=True, copy=False,
    )
    tracking_number = fields.Char(
        string="Tracking Number", readonly=True, copy=False,
    )
    provider_account_id = fields.Many2one(
        'shipping.provider.account',
        string="Used Shipping Account", readonly=True, copy=False,
    )
    delivery_order_ids = fields.One2many(
        'shipping.delivery.order', 'picking_id', string="Delivery Orders",
    )
    delivery_order_count = fields.Integer(
        compute='_compute_delivery_order_count',
    )
    shipping_currency_id = fields.Many2one(
        'res.currency', compute='_compute_shipping_currency_id',
        string="Shipping Currency",
    )
    customs_value = fields.Monetary(
        string="Customs Value", currency_field='shipping_currency_id',
        help="Declared value used for international shipments. Leave empty "
             "to fall back to a default placeholder.",
    )

    @api.depends('delivery_order_ids')
    def _compute_delivery_order_count(self):
        for picking in self:
            picking.delivery_order_count = len(picking.delivery_order_ids)

    @api.depends('move_line_ids.result_package_id')
    def _compute_shipping_package_ids(self):
        for picking in self:
            if picking.shipping_package_ids:
                continue
            packages = picking.move_line_ids.result_package_id.filtered(
                'package_type_id'
            )
            if not packages:
                continue
            commands = []
            for pkg in packages:
                pt = pkg.package_type_id
                commands.append(Command.create({
                    'package_type_id': pt.id,
                    'name': pkg.name,
                    'length': pt.packaging_length or 1.0,
                    'width': pt.width or 1.0,
                    'height': pt.height or 1.0,
                    'weight': pkg.shipping_weight or pt.base_weight or 1.0,
                }))
            picking.shipping_package_ids = commands

    @api.depends('sale_id', 'company_id')
    def _compute_shipping_currency_id(self):
        for picking in self:
            sale = getattr(picking, 'sale_id', False)
            picking.shipping_currency_id = (
                sale.currency_id if sale and sale.currency_id
                else picking.company_id.currency_id
            )

    def action_view_delivery_orders(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _('Delivery Orders'),
            'res_model': 'shipping.delivery.order',
            'context': {'default_picking_id': self.id},
        }
        if self.delivery_order_count == 1:
            action.update({
                'view_mode': 'form',
                'res_id': self.delivery_order_ids.id,
            })
        else:
            action.update({
                'view_mode': 'list,form',
                'domain': [('picking_id', '=', self.id)],
            })
        return action

    def action_fetch_picking_shipping_rates(self):
        self.ensure_one()
        self.shipping_option_ids.unlink()

        if not self.shipping_package_ids:
            raise UserError(
                _("No pallets configured on this transfer. Add at least "
                  "one pallet on the Shipping tab.")
            )
        if not self.partner_id:
            raise UserError(_("This transfer has no recipient (partner)."))

        accounts = self.company_id.shipping_account_ids
        if not accounts:
            raise UserError(_("No Shipping accounts configured in Settings."))

        currency_code = self.shipping_currency_id.name

        for account in accounts:
            try:
                api_client = account.get_api_client()
                rates = api_client.fetch_all_shipping_rates(
                    ship_from=self.company_id.partner_id,
                    ship_to=self.partner_id,
                    pallets=self.shipping_package_ids,
                    currency_code=currency_code,
                    customs_value=self.customs_value or None,
                )

                for rate in rates:
                    pkg = rate.get('packaging_type') or ''
                    service_label = rate['service_type'].replace('_', ' ').title()
                    pkg_label = (
                        " (%s)" % pkg.replace('_', ' ').title() if pkg else ""
                    )
                    self.env['picking.shipping.option'].create({
                        'picking_id': self.id,
                        'service_name': (
                            f"[{account.provider.upper()}] "
                            f"{service_label}{pkg_label}"
                        ),
                        'delivery_time': rate['delivery_time'],
                        'cost': rate['cost'],
                        'provider_account_id': account.id,
                        'raw_service_type': rate['service_type'],
                        'raw_packaging_type': pkg or False,
                    })
            except UserError:
                raise
            except Exception as e:
                _logger.error(
                    "Failed to fetch rates for account %s: %s",
                    account.name, str(e),
                )

    def action_cancel_shipment(self):
        self.ensure_one()
        if not self.tracking_number:
            return

        confirmed_orders = self.delivery_order_ids.filtered(
            lambda o: (
                o.state == 'confirmed'
                and o.tracking_number == self.tracking_number
            )
        )
        if len(confirmed_orders) == 1:
            confirmed_orders.action_cancel_order()
            return
        if len(confirmed_orders) > 1:
            raise UserError(_(
                "Multiple active shipments share this tracking number. "
                "Open the Shipments smart button and cancel each individually."
            ))

        if not self.provider_account_id:
            raise UserError(
                _("No shipping account is linked to this shipment. Cannot void.")
            )
        try:
            api_client = self.provider_account_id.get_api_client()
            api_client.void_shipment(self.tracking_number)
        except Exception as e:
            raise UserError(_("Failed to void label with API: %s") % str(e))

        for pallet in self.shipping_package_ids:
            pallet.tracking_number = False
        self.write({
            'tracking_number': False,
            'selected_shipping_service': False,
            'provider_account_id': False,
        })
