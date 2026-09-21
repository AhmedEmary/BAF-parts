from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # contact_number is BAF's own auto-assigned sequence, so a customer-number
    # BAF does not recognise cannot go there; it is kept here instead, which is
    # what lets the garage's next order find this contact again.
    lexcom_customer_number = fields.Char(
        string="LexCom Customer Number",
        copy=False,
        index=True,
        help="The customer-number LexCom sent when this contact was created "
             "from an order. Matched after BAF's own Contact Number.",
    )

    _lexcom_customer_number_uniq = models.Constraint(
        "unique(lexcom_customer_number)",
        "Two contacts cannot share the same LexCom customer number.",
    )
