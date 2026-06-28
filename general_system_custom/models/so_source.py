from odoo import fields, models


class SoSource(models.Model):
    _name = "so.source"
    _description = "Sale Order Source"
    _order = "name"

    name = fields.Char(string="Source", required=True, translate=True)
    active = fields.Boolean(default=True)
