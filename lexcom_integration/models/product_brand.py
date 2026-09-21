from odoo import fields, models

# Appendix A of the LexCom DMS Protocol Specification 3.1. These are the only
# values the ``brand`` element may carry, and they will not match BAF's own
# brand names, which are technical keys ("JLR"). Hence a dedicated code field
# rather than matching on name or display_label.
LEXCOM_BRAND_CODES = [
    "Abarth", "AlfaRomeo", "Alpine", "Audi", "Bentley", "BMW", "BMWMotorrad",
    "Bugatti", "Citroen", "CitroenDS", "Dacia", "Fiat", "Ford", "Hyundai",
    "Infiniti", "Iveco", "Jaguar", "Jeep", "Kia", "Lancia", "Landrover",
    "Lexus", "MAN", "Mercedes-Benz", "MINI", "Mitsubishi", "Nissan", "Opel",
    "Peugeot", "Polestar", "Porsche", "RAM", "Renault", "Seat", "Skoda",
    "Smart", "Subaru", "Suzuki", "Toyota", "Vauxhall", "Volkswagen", "Volvo",
    "VWTrucksAndBuses", "VWCommercial",
]


class ProductBrand(models.Model):
    _inherit = "product.brand"

    lexcom_code = fields.Char(
        string="LexCom Brand Code",
        index=True,
        help="The Appendix A brand code LexCom sends for this brand, e.g. "
             "'Mercedes-Benz' or 'VWTrucksAndBuses'. Matching happens on this "
             "field, never on the brand name, so renaming a brand cannot break "
             "incoming order resolution.",
    )

    _lexcom_code_uniq = models.Constraint(
        "unique(lexcom_code)",
        "Two brands cannot share the same LexCom brand code.",
    )
