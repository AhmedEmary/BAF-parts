from odoo import models, fields, api

class DiscountCode(models.Model):
    _name = 'discount.code'
    _description = 'Discount Code'

    name = fields.Char(string='Discount Code', required=True)
    
    value_ids = fields.One2many(
        'discount.code.value', 
        'code_id', 
        string='Supplier Values'
    )

    partner_ids = fields.Many2many(
        'res.partner',
        string='Suppliers',
        compute='_compute_partner_ids',
        store=True
    )
    _name_uniq_constraint=  models.Constraint('unique (name)', 'The Discount Code name must be unique!')
    

    @api.depends('value_ids.partner_id')
    def _compute_partner_ids(self):
        for record in self:
            record.partner_ids = record.value_ids.mapped('partner_id')

    @api.model
    def get_import_templates(self):
        return [{
            'label': self.env._('Import Template for Discount Codes'),
            'template': '/general_system_custom/static/xls/discount_codes_template.xlsx',
        }]
    @api.model_create_multi
    def create(self, vals_list):
        """
        Parent Upsert Logic:
        If Code Name exists, Write to it (which triggers child logic).
        If Code Name is new, Create it.
        """
        records = self.env['discount.code']
        to_create = []

        for vals in vals_list:
            name = vals.get('name')
            existing = self.env['discount.code']
            
            if name:
                existing = self.search([('name', '=', name)], limit=1)
            
            if existing:
                existing.write(vals)
                records |= existing
            else:
                to_create.append(vals)

        if to_create:
            records |= super().create(to_create)
            
        return records


class DiscountCodeValue(models.Model):
    _name = 'discount.code.value'
    _description = 'Discount Code Value per Supplier'

    code_id = fields.Many2one('discount.code', string='Discount Code', ondelete='cascade', required=True)
    partner_id = fields.Many2one('res.partner', string='Supplier')
    percentage = fields.Float(string='Discount Percentage')

    _partner_uniq_constraint = models.Constraint('unique(code_id, partner_id)', 'This Supplier already has a value for this Discount Code.')
    
    @api.model_create_multi
    def create(self, vals_list):
        """
        Child Upsert Logic:
        If (Code + Supplier) exists, Update Percentage.
        If (Code + Supplier) is new, Create new record.
        """
        records = self.env['discount.code.value']
        to_create = []

        for vals in vals_list:
            code_id = vals.get('code_id')
            partner_id = vals.get('partner_id')
            
            existing = self.env['discount.code.value']
            
            if code_id and partner_id:
                existing = self.search([
                    ('code_id', '=', code_id), 
                    ('partner_id', '=', partner_id)
                ], limit=1)
            
            if existing:
                if 'percentage' in vals:
                    existing.write({'percentage': vals['percentage']})
                records |= existing
            else:
                to_create.append(vals)

        if to_create:
            records |= super().create(to_create)
        
        return records
