import base64
import json
import logging
from datetime import timedelta
from io import BytesIO

from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools.pdf import PdfFileReader, PdfWriter

_logger = logging.getLogger(__name__)


INCOTERM_SELECTION = [
    ('DAP', 'DAP - Delivered at Place'),
    ('DDP', 'DDP - Delivered Duty Paid'),
    ('EXW', 'EXW - Ex Works'),
    ('FCA', 'FCA - Free Carrier'),
    ('CIF', 'CIF - Cost, Insurance and Freight'),
    ('CIP', 'CIP - Carriage and Insurance Paid To'),
    ('CPT', 'CPT - Carriage Paid To'),
    ('FOB', 'FOB - Free on Board'),
]


class ShippingDeliveryOrder(models.Model):
    _name = 'shipping.delivery.order'
    _description = 'Shipping Delivery Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string='Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New'),
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('confirmed', 'Confirmed'),
            ('error', 'Error'),
            ('cancelled', 'Cancelled'),
        ],
        default='draft',
        required=True,
        tracking=True,
        copy=False,
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Transfer',
        required=True,
        ondelete='cascade',
        tracking=True,
    )
    company_id = fields.Many2one(
        'res.company',
        related='picking_id.company_id',
        store=True,
    )
    provider_account_id = fields.Many2one(
        'shipping.provider.account',
        string='Shipping Account',
        required=True,
        tracking=True,
    )
    provider = fields.Selection(
        related='provider_account_id.provider',
        store=True,
    )

    # ---- Selected service / pricing ----
    service_type = fields.Char(string='Service Code', required=True)
    service_name = fields.Char(string='Service Name')
    label_format = fields.Char(string='Label Format')
    packaging_type = fields.Char(
        string='Packaging Type',
        help="Packaging code (e.g. YOUR_PACKAGING, FEDEX_PAK) chosen at "
             "rate-selection time.",
    )
    freight_charge = fields.Monetary(
        string='Quoted Charge',
        currency_field='currency_id',
    )
    actual_charge = fields.Monetary(
        string='Actual Charge',
        currency_field='currency_id',
        readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
    )

    # ---- Shipper (editable, prefilled from company) ----
    shipper_partner_id = fields.Many2one('res.partner', string='Shipper Partner')
    shipper_name = fields.Char(string='Shipper Name')
    shipper_company_name = fields.Char(string='Shipper Company')
    shipper_phone = fields.Char(string='Shipper Phone')
    shipper_email = fields.Char(string='Shipper Email')
    shipper_street = fields.Char(string='Shipper Street')
    shipper_street2 = fields.Char(string='Shipper Street 2')
    shipper_city = fields.Char(string='Shipper City')
    shipper_zip = fields.Char(string='Shipper ZIP')
    shipper_state_id = fields.Many2one('res.country.state', string='Shipper State')
    shipper_country_id = fields.Many2one('res.country', string='Shipper Country')

    # ---- Recipient ----
    recipient_partner_id = fields.Many2one('res.partner', string='Recipient Partner')
    recipient_name = fields.Char(string='Recipient Name')
    recipient_company_name = fields.Char(string='Recipient Company')
    recipient_phone = fields.Char(string='Recipient Phone')
    recipient_email = fields.Char(string='Recipient Email')
    recipient_street = fields.Char(string='Recipient Street')
    recipient_street2 = fields.Char(string='Recipient Street 2')
    recipient_city = fields.Char(string='Recipient City')
    recipient_zip = fields.Char(string='Recipient ZIP')
    recipient_state_id = fields.Many2one('res.country.state', string='Recipient State')
    recipient_country_id = fields.Many2one('res.country', string='Recipient Country')

    # ---- Shipment details ----
    planned_shipping_datetime = fields.Datetime(
        string='Planned Shipping Time',
        default=lambda self: fields.Datetime.now() + timedelta(hours=2),
    )
    description = fields.Char(string='Description', default='Auto parts')
    incoterm = fields.Selection(INCOTERM_SELECTION, default='DAP')
    is_customs_declarable = fields.Boolean(
        string='Customs Declarable',
        compute='_compute_is_customs_declarable',
        store=True,
        readonly=False,
    )
    declared_value = fields.Monetary(
        string='Declared Value',
        currency_field='declared_value_currency_id',
    )
    declared_value_currency_id = fields.Many2one(
        'res.currency',
        string='Declared Currency',
    )
    pickup_requested = fields.Boolean(string='Pickup Requested')

    # ---- Packages ----
    package_ids = fields.One2many(
        'shipping.delivery.order.package',
        'order_id',
        string='Packages',
        copy=True,
    )

    # ---- API result ----
    tracking_number = fields.Char(string='Tracking Number', readonly=True, copy=False)
    label_attachment_ids = fields.Many2many(
        'ir.attachment',
        'shipping_delivery_order_attachment_rel',
        'order_id',
        'attachment_id',
        string='Labels',
        copy=False,
    )
    label_count = fields.Integer(compute='_compute_label_count')
    error_message = fields.Text(string='Last Error', readonly=True, copy=False)
    last_request_payload = fields.Text(
        string='Last Request Payload', readonly=True, copy=False,
    )
    last_response_body = fields.Text(
        string='Last Response Body', readonly=True, copy=False,
    )
    api_log_ids = fields.One2many(
        'shipping.delivery.order.api.log',
        'order_id',
        string='API Log',
        readonly=True,
        copy=False,
    )

    # ---- Computes ----
    @api.depends('shipper_country_id', 'recipient_country_id')
    def _compute_is_customs_declarable(self):
        for order in self:
            order.is_customs_declarable = bool(
                order.shipper_country_id
                and order.recipient_country_id
                and order.shipper_country_id != order.recipient_country_id
            )

    @api.depends('label_attachment_ids')
    def _compute_label_count(self):
        for order in self:
            order.label_count = len(order.label_attachment_ids)

    # ---- ORM ----
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('shipping.delivery.order')
                    or _('New')
                )
        return super().create(vals_list)

    # ---- Helpers ----
    def _populate_from_partner(self, prefix, partner):
        self.ensure_one()
        if not partner:
            return
        commercial = partner.parent_id or partner
        self.write({
            f'{prefix}_partner_id': partner.id,
            f'{prefix}_name': partner.name,
            f'{prefix}_company_name': (
                commercial.name if commercial != partner else partner.name
            ),
            f'{prefix}_phone': (
                partner.phone or getattr(partner, 'mobile', '') or ''
            ),
            f'{prefix}_email': partner.email or '',
            f'{prefix}_street': partner.street or '',
            f'{prefix}_street2': partner.street2 or '',
            f'{prefix}_city': partner.city or '',
            f'{prefix}_zip': partner.zip or '',
            f'{prefix}_state_id': partner.state_id.id if partner.state_id else False,
            f'{prefix}_country_id': (
                partner.country_id.id if partner.country_id else False
            ),
        })

    def _populate_packages_from_pallets(self, pallets):
        self.ensure_one()
        package_vals = []
        for pallet in pallets:
            package_vals.append((0, 0, {
                'package_type_id': (
                    pallet.package_type_id.id if pallet.package_type_id else False
                ),
                'name': pallet.name or '',
                'weight': pallet.weight or 1.0,
                'length': pallet.length or 1.0,
                'width': pallet.width or 1.0,
                'height': pallet.height or 1.0,
                'description': self.description or 'Auto parts',
            }))
        self.package_ids = [(5, 0, 0)] + package_vals

    # ---- Actions ----
    def action_open_form(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_view_labels(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ir.attachment',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.label_attachment_ids.ids)],
            'target': 'current',
        }

    def action_cancel_order(self):
        for order in self:
            if order.state != 'confirmed':
                raise UserError(_("Only confirmed orders can be cancelled."))
            if not order.tracking_number:
                order.write({'state': 'cancelled'})
                continue
            client = order.provider_account_id.get_api_client()
            try:
                client.void_shipment(order.tracking_number)
            except UserError:
                raise
            except Exception as e:
                _logger.exception("Void failed for %s", order.name)
                raise UserError(
                    _("Failed to void shipment with carrier: %s") % str(e)
                )

            order._log_api_call({
                'method': 'DELETE',
                'endpoint': 'void',
                'status_code': 200,
                'request_payload': {'tracking_number': order.tracking_number},
                'response_text': '',
                'error_message': '',
            })

            for pallet in order.package_ids.mapped('source_pallet_id'):
                if pallet.tracking_number == order.tracking_number:
                    pallet.tracking_number = False

            picking = order.picking_id
            if picking and picking.tracking_number == order.tracking_number:
                picking.write({
                    'tracking_number': False,
                    'selected_shipping_service': False,
                    'provider_account_id': False,
                })
                picking.message_post(body=_(
                    "Shipment %s voided. Tracking %s removed."
                ) % (order.name, order.tracking_number))

            order.write({'state': 'cancelled'})

    def action_reset_to_draft(self):
        for order in self:
            if order.state not in ('error', 'cancelled'):
                raise UserError(
                    _("Only orders in Error or Cancelled state can be reset to draft.")
                )
            order.write({'state': 'draft', 'error_message': False})

    def action_preview_payload(self):
        self.ensure_one()
        if self.state not in ('draft', 'error'):
            raise UserError(
                _("Payload preview is only available before submission.")
            )
        wizard = self.env['shipping.delivery.order.preview.wizard'].create({
            'order_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payload Preview'),
            'res_model': 'shipping.delivery.order.preview.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_generate_label(self):
        self.ensure_one()
        if self.state not in ('draft', 'error'):
            raise UserError(_("This order has already been submitted."))
        if not self.package_ids:
            raise UserError(_("Add at least one package before generating a label."))
        if not self.shipper_country_id or not self.recipient_country_id:
            raise UserError(_("Shipper and recipient countries are required."))

        client = self.provider_account_id.get_api_client()
        payload = client.build_shipment_payload(self)

        try:
            payload_json = json.dumps(payload, indent=2, default=str)
        except Exception:
            payload_json = str(payload)
        self.last_request_payload = payload_json

        try:
            result = client.submit_shipment(self, payload)
        except UserError:
            raise
        except Exception as e:
            _logger.exception("Shipment submission failed for %s", self.name)
            self._log_api_call({
                'method': 'POST',
                'endpoint': 'unknown',
                'status_code': 0,
                'request_payload': payload,
                'response_text': '',
                'error_message': str(e),
            })
            self.write({'state': 'error', 'error_message': str(e)})
            return

        self.last_response_body = (result.get('response_text') or '')[:50000]
        self._log_api_call(result)

        if not result.get('success'):
            self.write({
                'state': 'error',
                'error_message': result.get('error_message') or _('Unknown error'),
            })
            return

        attachments = self._save_label_attachments(result.get('labels') or [])
        self.write({
            'state': 'confirmed',
            'tracking_number': result.get('tracking_number'),
            'actual_charge': result.get('price') or self.freight_charge,
            'label_attachment_ids': [(6, 0, attachments.ids)],
            'error_message': False,
        })

        self._post_to_picking(attachments)

    def _log_api_call(self, result):
        self.ensure_one()
        try:
            payload_text = json.dumps(
                result.get('request_payload'), indent=2, default=str,
            )
        except Exception:
            payload_text = str(result.get('request_payload'))
        self.env['shipping.delivery.order.api.log'].create({
            'order_id': self.id,
            'direction': 'out',
            'method': result.get('method') or 'POST',
            'endpoint': result.get('endpoint') or '',
            'status_code': result.get('status_code') or 0,
            'payload': payload_text,
            'response_body': (result.get('response_text') or '')[:50000],
            'error_message': result.get('error_message') or '',
        })

    def _save_label_attachments(self, labels):
        """Merge per-piece FedEx labels into a single PDF (one consignee
        copy per piece). Other documents stay separate.
        """
        self.ensure_one()
        attachment_ids = self.env['ir.attachment']

        label_pdfs = []
        other_docs = []
        for filename, b64_content in labels:
            if filename.startswith('FedEx_label_'):
                label_pdfs.append(base64.b64decode(b64_content))
            else:
                other_docs.append((filename, b64_content))

        if label_pdfs:
            writer = PdfWriter()
            for pdf_bytes in label_pdfs:
                reader = PdfFileReader(BytesIO(pdf_bytes), strict=False)
                if len(reader.pages):
                    writer.add_page(reader.pages[0])
            buf = BytesIO()
            writer.write(buf)
            attachment_ids |= self.env['ir.attachment'].create({
                'name': f'FedEx_labels_{self.tracking_number or "shipment"}.pdf',
                'datas': base64.b64encode(buf.getvalue()),
                'res_model': self._name,
                'res_id': self.id,
                'mimetype': 'application/pdf',
            })

        for filename, b64_content in other_docs:
            attachment_ids |= self.env['ir.attachment'].create({
                'name': filename,
                'datas': b64_content,
                'res_model': self._name,
                'res_id': self.id,
                'mimetype': 'application/pdf',
            })
        return attachment_ids

    def _post_to_picking(self, attachments):
        self.ensure_one()
        picking = self.picking_id
        if not picking:
            return
        picking.write({
            'tracking_number': self.tracking_number,
            'selected_shipping_service': self.service_name or self.service_type,
            'provider_account_id': self.provider_account_id.id,
        })

        attachment_pairs = [
            (a.name, base64.b64decode(a.datas))
            for a in attachments
        ]
        picking.message_post(
            body=_("Shipment %s confirmed via %s. Tracking: %s") % (
                self.name, self.provider_account_id.name, self.tracking_number,
            ),
            attachments=attachment_pairs,
        )


class ShippingDeliveryOrderPackage(models.Model):
    _name = 'shipping.delivery.order.package'
    _description = 'Shipping Delivery Order Package'

    order_id = fields.Many2one(
        'shipping.delivery.order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    package_type_id = fields.Many2one(
        'stock.package.type',
        string='Package Type',
        help="Odoo native package type (used as the pallet/box source).",
    )
    source_pallet_id = fields.Many2one(
        'shipping.picking.package',
        string='Source Pallet',
        ondelete='set null',
    )
    name = fields.Char(string='Reference')
    weight = fields.Float(string='Weight (kg)', default=1.0)
    length = fields.Float(string='Length (cm)', default=1.0)
    width = fields.Float(string='Width (cm)', default=1.0)
    height = fields.Float(string='Height (cm)', default=1.0)
    description = fields.Char(string='Description', default='Auto parts')

    @api.onchange('package_type_id')
    def _onchange_package_type_id(self):
        for pkg in self:
            pt = pkg.package_type_id
            if not pt:
                continue
            if pt.packaging_length:
                pkg.length = pt.packaging_length
            if pt.width:
                pkg.width = pt.width
            if pt.height:
                pkg.height = pt.height
            if pt.base_weight:
                pkg.weight = pt.base_weight
            if not pkg.name:
                pkg.name = pt.name


class ShippingDeliveryOrderApiLog(models.Model):
    _name = 'shipping.delivery.order.api.log'
    _description = 'Shipping Delivery Order API Log'
    _order = 'create_date desc, id desc'

    order_id = fields.Many2one(
        'shipping.delivery.order',
        required=True,
        ondelete='cascade',
        index=True,
    )
    direction = fields.Selection(
        [('out', 'Outbound'), ('in', 'Inbound')],
        default='out',
        required=True,
    )
    method = fields.Char(string='HTTP Method')
    endpoint = fields.Char(string='Endpoint')
    status_code = fields.Integer(string='Status Code')
    payload = fields.Text(string='Request Payload')
    response_body = fields.Text(string='Response Body')
    error_message = fields.Text(string='Error')
