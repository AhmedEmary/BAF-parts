from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    baf_b2b_notify_emails = fields.Char(
        string="B2B Application Notification Emails",
        config_parameter='b2b_custom.registration_notification_email',
        help="Comma-separated list of email addresses that receive a "
             "notification whenever someone submits a B2B registration. "
             "Leave empty to fall back to the company's email.",
    )
