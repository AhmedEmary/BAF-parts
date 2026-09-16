import logging
import base64
import re
from io import BytesIO
from odoo import models, fields, api, _
from odoo.exceptions import UserError

try:
    import openpyxl
except ImportError:
    openpyxl = None

_logger = logging.getLogger(__name__)

# Vendors whose POs are sent from the Kalkan mailbox; everyone else goes
# through the BAF mailbox. Matched case-insensitively against the vendor's
# name (and its commercial parent), so name suffixes like "GmbH" don't matter.
BAF_KALKAN_VENDOR_KEYWORDS = ('arnold', 'euler', 'brass', 'kalkan')
BAF_KALKAN_FROM = 'Kalkan Automobile <b.oezleblebici@kalkan-auto.de>'
BAF_DEFAULT_FROM = 'BAF Parts <info@baf-parts.com>'


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    sale_order_id = fields.Many2one(
        'sale.order',
        string="Sales Order",
        readonly=True,
        help="The Sales Order that triggered this Purchase Order."
    )

    line_count = fields.Integer(string='Line Count', compute='_compute_line_count')

    send_po_status = fields.Selection(
        selection=[('pending', 'Pending'), ('success', 'Successed')],
        string='Send PO',
        default='pending',
        copy=False,
        help="Status of the PO email sending process."
    )

    baf_customer_account_source = fields.Selection(
        selection=[
            ('default', 'Default (Contact Number)'),
            ('alternative', 'Alternative'),
        ],
        string='Customer Account #',
        default='default',
        copy=True,
        help="Which of the customer's account numbers to send to the supplier "
             "on this PO. Falls back to the default when no alternative is set.",
    )
    baf_customer_account_number = fields.Char(
        string='Customer Account Number',
        compute='_compute_baf_customer_account_number',
        store=True,
        readonly=True,
    )

    @api.depends('baf_customer_account_source',
                 'sale_order_id.partner_id.contact_number',
                 'sale_order_id.partner_id.baf_alt_account_number',
                 'sale_order_id.partner_id.commercial_partner_id.contact_number',
                 'sale_order_id.partner_id.commercial_partner_id.baf_alt_account_number')
    def _compute_baf_customer_account_number(self):
        for po in self:
            customer = po.sale_order_id.partner_id if po.sale_order_id else False
            if not customer:
                po.baf_customer_account_number = ''
                continue
            po.baf_customer_account_number = customer._baf_customer_account_number(
                use_alt=(po.baf_customer_account_source == 'alternative'))

    pallet_count = fields.Integer(string='Pallets', compute='_compute_pallet_count')

    def _compute_pallet_count(self):
        for order in self:
            if not isinstance(order.id, int):
                order.pallet_count = 0
                continue

            pallets = self.env['warehouse.pallet'].search([
                ('line_ids.purchase_order_id', '=', order.id)
            ])
            order.pallet_count = len(pallets)

    def action_view_pallets(self):
        self.ensure_one()
        pallets = self.env['warehouse.pallet'].search([
            ('line_ids.purchase_order_id', '=', self.id)
        ])
        return {
            'name': 'Related Pallets',
            'type': 'ir.actions.act_window',
            'res_model': 'warehouse.pallet',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pallets.ids)],
        }

    @api.depends('order_line')
    def _compute_line_count(self):
        for order in self:
            order.line_count = len(order.order_line)

    def action_view_sale_order(self):
        self.ensure_one()
        return {
            'name': 'Sales Order',
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'view_mode': 'form',
            'res_id': self.sale_order_id.id,
        }

    def action_view_po_lines(self):
        self.ensure_one()
        return {
            'name': f'Lines of {self.name}',
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order.line',
            'view_mode': 'list,form',
            'views': [
                (self.env.ref('general_system_custom.view_purchase_order_line_tree_intelliwise').id, 'list'),
                (False, 'form')
            ],
            'domain': [('order_id', '=', self.id)],
            'context': {'default_order_id': self.id},
        }

    def _sanitize(self, value):
        """ Helper to ensure False/None become empty strings for Excel """
        if value is False or value is None:
            return ""
        return value

    def _baf_display_sku(self, product):
        """SKU as suppliers expect it — the brand's technical `name` prefix on
        `default_code` (e.g. "LR_LR097165") is BAF-internal and must not leak
        into vendor documents. Prefer `product.sku` (per-brand SKU); fall back
        to stripping the "<brand>_" prefix from `default_code`."""
        if product.sku:
            return product.sku
        code = product.default_code or ""
        brand_name = product.brand.name if product.brand else ""
        if brand_name and code.startswith(brand_name + "_"):
            return code[len(brand_name) + 1:]
        return code

    def _baf_po_excel_attachment(self):
        """Build the vendor Excel workbook for these orders and return it as an
        ir.attachment. Shared by the bulk action and the form buttons; the
        single-vendor guard makes it safe on any recordset size.
        """
        if not self:
            return self.env['ir.attachment']

        partners = self.mapped('partner_id')
        if len(set(partners)) > 1:
            raise UserError(_("Different suppliers detected. Please select orders from a single supplier."))

        vendor = partners[0]

        if not openpyxl:
            raise UserError(_("The 'openpyxl' library is missing."))

        wb = openpyxl.Workbook()

        headers = [
            "PO N.", "Brand", "SKU", "Quantity", "Retail",
            "Surcharge", "Unit Net", "Total",
        ]

        ws_std = wb.active
        ws_std.title = "Standard Orders"
        ws_std.append(headers)

        ws_drop = wb.create_sheet("Dropship Orders")
        # Dropship headers have extra columns
        drop_headers = list(headers) + ["Delivery Address", "Phone"]
        ws_drop.append(drop_headers)

        has_std = False
        has_drop = False

        for po in self:
            is_dropship = bool(po.dest_address_id)

            for line in po.order_line:
                brand_name = self._sanitize(line.product_id.brand.display_name)
                sku = self._sanitize(self._baf_display_sku(line.product_id))

                row_data = [
                    self._sanitize(po.name),
                    brand_name,
                    sku,
                    line.product_qty or 0.0,
                    line.retail_price or 0.0,
                    line.surcharge or 0.0,
                    line.price_unit or 0.0,
                    line.price_subtotal or 0.0,
                ]

                if is_dropship:
                    addr = po.dest_address_id
                    if addr:
                        parts = [
                            addr.name,
                            addr.street,
                            addr.city,
                            addr.country_id.name
                        ]
                        address_str = ", ".join([str(p) for p in parts if p])
                        phone_val = addr.phone or ""
                    else:
                        address_str = ""
                        phone_val = ""

                    row_data.extend([address_str, phone_val])
                    ws_drop.append(row_data)
                    has_drop = True
                else:
                    ws_std.append(row_data)
                    has_std = True

        if not has_std and not has_drop:
            pass
        else:
            if not has_drop and "Dropship Orders" in wb.sheetnames:
                del wb["Dropship Orders"]
            if not has_std and "Standard Orders" in wb.sheetnames:
                if len(wb.sheetnames) > 1:
                    del wb["Standard Orders"]

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        file_content = base64.b64encode(output.read())
        output.close()

        safe_vendor_name = re.sub(r'[\\/*?:"<>|]', "", vendor.name or "Vendor")
        attachment_name = f"Orders_{safe_vendor_name}_{fields.Date.today()}.xlsx"

        # 7. Create Attachment
        attachment = self.env['ir.attachment'].create({
            'name': attachment_name,
            'type': 'binary',
            'datas': file_content,
            'res_model': 'purchase.order',
            'res_id': self[0].id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        })

        return attachment

    def _baf_vendor_email_from(self):
        """Route PO emails by vendor: Arnold / Euler / Brass / Kalkan go out of
        the Kalkan mailbox, everyone else out of the BAF mailbox. Matching is
        substring/case-insensitive against the vendor name and its commercial
        parent, so name variants ("Autohaus Arnold GmbH & Co. KG",
        "Hermann Arnold GmbH", …) all land on the same sender."""
        self.ensure_one()
        vendor = self.partner_id
        names = [vendor.name or '']
        if vendor.commercial_partner_id and vendor.commercial_partner_id != vendor:
            names.append(vendor.commercial_partner_id.name or '')
        haystack = ' '.join(names).lower()
        if any(k in haystack for k in BAF_KALKAN_VENDOR_KEYWORDS):
            return BAF_KALKAN_FROM
        return BAF_DEFAULT_FROM

    def _notify_get_recipients_groups(self, message, model_description, msg_vals=False):
        """Suppliers get no Odoo portal button in the email. The base
        implementation attaches a "View Quotation" / "View Order" access button
        to every recipient group; strip it so the outgoing mail is just our
        text plus the Excel attachment."""
        groups = super()._notify_get_recipients_groups(
            message, model_description, msg_vals=msg_vals)
        for group in groups:
            group_data = group[2]
            group_data['has_button_access'] = False
        return groups

    def action_send_grouped_po_email(self):
        """Bulk action: one Excel for the whole selection, one composer."""
        attachment = self._baf_po_excel_attachment()
        if not attachment:
            return
        self.write({'send_po_status': 'success'})

        # 8. Open Composer
        template_id = self.env.ref('purchase.email_template_edi_purchase').id
        ctx = {
            'default_model': 'purchase.order',
            'default_res_ids': self.ids,
            'default_use_template': bool(template_id),
            'default_template_id': template_id,
            'default_attachment_ids': [attachment.id],
            'default_composition_mode': 'comment',
            'default_email_from': self[0]._baf_vendor_email_from(),
            'force_email': True,
        }
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'mail.compose.message',
            'views': [(False, 'form')],
            'view_id': False,
            'target': 'new',
            'context': ctx,
        }

    def action_rfq_send(self):
        """Send RFQ / Send PO from the form: same Excel, never a PDF. The PDF
        is dropped at its source by clearing report_template_ids on the
        purchase mail templates (data/purchase_mail_template.xml)."""
        action = super().action_rfq_send()
        attachment = self._baf_po_excel_attachment()
        ctx = action.setdefault('context', {})
        ctx['default_email_from'] = self._baf_vendor_email_from()
        if attachment:
            ctx['default_attachment_ids'] = [attachment.id]
            self.write({'send_po_status': 'success'})
        return action
