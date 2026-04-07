from odoo import models, fields, api, _
from odoo.exceptions import UserError

class CreditNoteWizard(models.TransientModel):
    _name = 'credit.note.wizard'
    _description = 'Selective Credit Note Wizard'

    invoice_id = fields.Many2one('account.move', string="Invoice", required=True)
    reason = fields.Char(string="Reason")
    journal_id = fields.Many2one('account.journal', string="Journal", required=True)
    date = fields.Date(string="Credit Note Date", default=fields.Date.context_today)
    
    line_ids = fields.One2many('credit.note.wizard.line', 'wizard_id', string="Items to Credit")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)

        active_id = self.env.context.get('active_id')
        if active_id:
            invoice = self.env['account.move'].browse(active_id)
            res['invoice_id'] = invoice.id
            res['journal_id'] = invoice.journal_id.id
            
            lines = []
            for line in invoice.invoice_line_ids:
                if line.display_type == 'product': 
                    lines.append((0, 0, {
                        'move_line_id': line.id,
                        'product_id': line.product_id.id,
                        'quantity': line.quantity,
                        'price_unit': line.price_unit,
                        'is_selected': False,
                    }))
            res['line_ids'] = lines
        return res

    def action_create_credit_note(self):
        self.ensure_one()
        selected_lines = self.line_ids.filtered(lambda l: l.is_selected)
        
        if not selected_lines:
            raise UserError(_("Please select at least one item to credit."))

        # 1. Prepare Header
        move_vals = {
            'move_type': 'out_refund',
            'partner_id': self.invoice_id.partner_id.id,
            'invoice_date': self.date,
            'journal_id': self.journal_id.id,
            'ref': self.reason,
            'reversed_entry_id': self.invoice_id.id,
            'invoice_origin': self.invoice_id.name,
            'currency_id': self.invoice_id.currency_id.id,
        }

        # 2. Prepare Lines
        new_lines = []
        for w_line in selected_lines:
            original = w_line.move_line_id
            
            # Validation to prevent "Missing Account" error
            if not original:
                raise UserError(_("Original invoice line data is missing. Please try closing and reopening the wizard."))
            if not original.account_id:
                raise UserError(_("The original invoice line for %s has no account set.") % original.product_id.name)

            line_vals = {
                'product_id': original.product_id.id,
                'name': original.name, 
                'quantity': w_line.quantity,
                'price_unit': original.price_unit,
                'account_id': original.account_id.id, # Must be present
                'tax_ids': [(6, 0, original.tax_ids.ids)],
                'sale_line_ids': [(6, 0, original.sale_line_ids.ids)],
                
                # Custom Fields
                'origin_country_id': original.origin_country_id.id if original.origin_country_id else False,
                'hs_code': original.hs_code,
                'supplier_delivery_ref': original.supplier_delivery_ref,
                'linked_so_id': original.linked_so_id.id if original.linked_so_id else False,
                'linked_po_id': original.linked_po_id.id if original.linked_po_id else False,
            }
            new_lines.append((0, 0, line_vals))

        move_vals['invoice_line_ids'] = new_lines

        # 3. Create
        credit_note = self.env['account.move'].create(move_vals)

        return {
            'name': 'Credit Note',
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': credit_note.id,
        }

class CreditNoteWizardLine(models.TransientModel):
    _name = 'credit.note.wizard.line'
    _description = 'Credit Note Item Selection'

    wizard_id = fields.Many2one('credit.note.wizard')
    move_line_id = fields.Many2one('account.move.line', string="Original Line")
    
    is_selected = fields.Boolean(string="Select")
    product_id = fields.Many2one('product.product', string="Product", readonly=True)
    quantity = fields.Float(string="Credit Qty")
    price_unit = fields.Float(string="Price", readonly=True)

    @api.onchange('is_selected')
    def _onchange_selected(self):
        if self.is_selected and self.quantity == 0 and self.move_line_id:
            self.quantity = self.move_line_id.quantity
