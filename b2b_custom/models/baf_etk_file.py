from odoo import fields, models


class BafEtkFile(models.Model):
    _name = 'baf.etk.file'
    _description = 'BAF ETK File'
    _order = 'create_date desc, id desc'

    name = fields.Char(string='Label', required=True, default='ETK')
    file_data = fields.Binary(string='ETK File', required=True, attachment=True)
    file_name = fields.Char(string='File Name')
    active = fields.Boolean(default=True)

    def _baf_current(self):
        return self.sudo().search([('active', '=', True)], limit=1)
