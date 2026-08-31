import os
import re

from odoo import api, fields, models


class BafEtkFile(models.Model):
    _name = 'baf.etk.file'
    _description = 'BAF ETK File'
    _order = 'create_date desc, id desc'

    name = fields.Char(string='Label', required=True, default='ETK')
    family_id = fields.Many2one(
        'baf.brand.family',
        string='Brand Family',
        required=True,
        ondelete='cascade',
        index=True,
        help="The brand family this ETK belongs to. Exactly one file per "
             "family: re-upload by replacing the file on this record.",
    )
    file_data = fields.Binary(string='ETK File', required=True, attachment=True)
    file_name = fields.Char(string='File Name')
    active = fields.Boolean(default=True)

    _family_uniq = models.Constraint(
        'unique(family_id)',
        'An ETK file already exists for this brand family. Open it and '
        'replace the file instead of creating a second one.',
    )

    def _baf_stored_file_name(self):
        """`etk_<family name, underscored>` keeping the uploaded extension, so
        the customer always downloads a file named after its brand family."""
        self.ensure_one()
        slug = re.sub(
            r'[^a-z0-9]+', '_', (self.family_id.name or '').lower()).strip('_')
        return 'etk_%s%s' % (
            slug or 'file', os.path.splitext(self.file_name or '')[1])

    def _baf_rename_file(self):
        for record in self:
            name = record._baf_stored_file_name()
            if record.file_name != name:
                record.file_name = name

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._baf_rename_file()
        return records

    def write(self, vals):
        res = super().write(vals)
        if {'file_data', 'file_name', 'family_id'} & vals.keys():
            self._baf_rename_file()
        return res

    def _baf_current(self, family):
        """The active ETK file for one brand family, or an empty recordset."""
        if not family:
            return self.browse()
        return self.sudo().search([
            ('active', '=', True),
            ('family_id', '=', family.id),
        ], limit=1)
