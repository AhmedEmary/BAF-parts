from odoo import models, fields, api, _
from odoo.exceptions import UserError

class AccountDrawDownPaymentWizard(models.TransientModel):
    _name = 'account.draw.down.payment.wizard'
    _description = 'Draw Down Payment Wizard'

    invoice_id = fields.Many2one('account.move', string="Final Invoice", required=True)
    currency_id = fields.Many2one(related='invoice_id.currency_id')
    line_ids = fields.One2many('account.draw.down.payment.line', 'wizard_id', string="Available Down Payments")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        
        invoice_id = res.get('invoice_id') or self.env.context.get('default_invoice_id')
        if not invoice_id:
            return res
            
        invoice = self.env['account.move'].browse(invoice_id)
        
        domain = [
            ('partner_id', '=', invoice.partner_id.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ['paid', 'in_payment']),
            ('id', '!=', invoice.id)
        ]
        candidate_moves = self.env['account.move'].search(domain, order="invoice_date desc")
        
        lines_data = []
        for move in candidate_moves:
            if move.amount_available_to_draw > 0:
                lines_data.append((0, 0, {
                    'down_payment_move_id': move.id,
                    'date': move.invoice_date,
                    'doc_number': move.name,
                    'amount_total': move.amount_available_to_draw,
                }))
        
        res['line_ids'] = lines_data
        return res

    def action_apply_down_payments(self):
        self.ensure_one()
        to_draw = self.line_ids.filtered(lambda l: l.amount_to_draw > 0)
        
        if not to_draw:
            raise UserError(_("Please enter an amount to draw."))

        new_lines = []
        for line in to_draw:
            dp_invoice = line.down_payment_move_id
           
            if line.amount_to_draw > dp_invoice.amount_available_to_draw:
                 raise UserError(_(
                     "Balance insufficient on %s! Available: %s, Requested: %s"
                 ) % (dp_invoice.name, dp_invoice.amount_available_to_draw, line.amount_to_draw))

            # 1. Product/Account finding logic (Same as before)
            valid_source_lines = dp_invoice.invoice_line_ids.filtered(lambda l: l.product_id)
            source_line = valid_source_lines[0] if valid_source_lines else False
            
            product = source_line.product_id if source_line else self.env['product.product'].search([
                '|', ('name', 'ilike', 'Acconto'), ('name', 'ilike', 'Down Payment')
            ], limit=1)
            
            if not product:
                 raise UserError(_("Product not found for DP %s") % dp_invoice.name)

            account = product.property_account_income_id or product.categ_id.property_account_income_categ_id
            if not account and source_line:
                account = source_line.account_id
            
            description = _("Draw Down Payment ref: %s") % dp_invoice.name
            
            new_lines.append((0, 0, {
                'product_id': product.id,
                'name': description,
                'quantity': -1,
                'price_unit': line.amount_to_draw,
                'account_id': account.id, 
                'tax_ids': [(6, 0, source_line.tax_ids.ids)] if source_line else [],
                'is_down_payment': True,
                'down_payment_origin_id': dp_invoice.id,
                'sequence': 999,
            }))
        
        self.invoice_id.write({'invoice_line_ids': new_lines})
        return {'type': 'ir.actions.act_window_close'}


class AccountDrawDownPaymentLine(models.TransientModel):
    _name = 'account.draw.down.payment.line'
    _description = 'Draw Down Payment Line'
    
    wizard_id = fields.Many2one('account.draw.down.payment.wizard')
    down_payment_move_id = fields.Many2one('account.move', string="Invoice Ref")
    doc_number = fields.Char(string="Doc. Number")
    date = fields.Date(string="Date")
    
    currency_id = fields.Many2one(related='wizard_id.currency_id')
    amount_total = fields.Monetary(string="Available Balance")
    amount_to_draw = fields.Monetary(string="Net Amount to Draw")
