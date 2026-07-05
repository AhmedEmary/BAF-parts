"""Part manufacturers (Bosch, Valeo, Filtron, …).

BAF's ``product.brand`` means *car make* (BMW, Mercedes…) and drives
brand families, discount tables and partner brand permissions. An
aftermarket part has no car make — it has a manufacturer. Keeping the
two concepts apart stops IC imports from polluting the brand system
with hundreds of aftermarket labels.
"""

from odoo import fields, models


class ProductManufacturer(models.Model):
    _name = 'product.manufacturer'
    _description = 'Part Manufacturer'
    _order = 'name'

    name = fields.Char(required=True, index=True)

    _name_uniq = models.Constraint(
        'unique(name)',
        "This manufacturer already exists.",
    )
