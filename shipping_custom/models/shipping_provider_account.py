from odoo import models, fields, _
from odoo.exceptions import UserError

from .fedex_client_api import FedexAPIClient
from .dhl_client_api import DhlAPIClient

FEDEX_STOCK_TYPE = [
    ('PAPER_4X6', 'PAPER_4X6'),
    ('PAPER_4X8', 'PAPER_4X8'),
    ('PAPER_4X9', 'PAPER_4X9'),
    ('PAPER_7X4.75', 'PAPER_7X4.75'),
    ('PAPER_8.5X11_BOTTOM_HALF_LABEL', 'PAPER_8.5X11_BOTTOM_HALF_LABEL'),
    ('PAPER_8.5X11_TOP_HALF_LABEL', 'PAPER_8.5X11_TOP_HALF_LABEL'),
    ('PAPER_LETTER', 'PAPER_LETTER'),
]


class ShippingProviderAccount(models.Model):
    _name = 'shipping.provider.account'
    _description = 'Shipping Provider Account Credentials'

    name = fields.Char(string="Account Name", required=True,
                       help="e.g. 'Main US Account' or 'Europe Account'")
    company_id = fields.Many2one(
        'res.company', string="Company", default=lambda self: self.env.company,
    )

    provider = fields.Selection([
        ('fedex', 'FedEx'),
        ('dhl', 'DHL'),
    ], string="Provider", required=True, default='fedex')

    prod_environment = fields.Boolean(string="Production Environment", default=False)
    account_number = fields.Char(string="Account Number", required=True)

    fedex_client_id = fields.Char(string="FedEx API Key (Client ID)")
    fedex_client_secret = fields.Char(string="FedEx API Secret")
    fedex_rest_access_token = fields.Char(string="FedEx Access Token", copy=False)
    fedex_label_stock_type = fields.Selection(
        FEDEX_STOCK_TYPE,
        string="FedEx Label Stock Type",
        default='PAPER_8.5X11_TOP_HALF_LABEL',
    )

    dhl_api_key = fields.Char(string="DHL API Key")
    dhl_api_secret = fields.Char(string="DHL API Secret")
    dhl_label_format = fields.Selection([
        ('ECOM26_84_001', 'Thermal 4x6 (100x150mm)'),
        ('ECOM26_A4_001', 'A4 Label'),
        ('ARCH_8X4_A4_001', '8x4 on A4'),
    ], string="DHL Label Format", default='ECOM26_A4_001')

    def get_api_client(self):
        self.ensure_one()
        if self.provider == 'fedex':
            return FedexAPIClient(self)
        if self.provider == 'dhl':
            return DhlAPIClient(self)
        raise UserError(_("API Client not implemented for %s", self.provider))

    def action_test_connection(self):
        self.ensure_one()
        client = self.get_api_client()
        return client.test_connection()
