from odoo import _, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    lexcom_dealer_id = fields.Char(
        related="company_id.lexcom_dealer_id", readonly=False,
    )
    lexcom_username = fields.Char(
        related="company_id.lexcom_username", readonly=False,
    )
    lexcom_enabled = fields.Boolean(
        related="company_id.lexcom_enabled", readonly=False,
    )
    lexcom_country = fields.Char(
        related="company_id.lexcom_country", readonly=False,
    )

    # UI only - lives in the auto-vacuumed transient table and is never written
    # to res.company. Only its PBKDF2 hash is persisted.
    lexcom_password = fields.Char(string="LexCom Password")

    lexcom_password_set = fields.Boolean(
        string="Password Configured",
        compute="_compute_lexcom_password_set",
    )

    def _compute_lexcom_password_set(self):
        for rec in self:
            rec.lexcom_password_set = bool(
                rec.company_id.sudo().lexcom_password_hash
            )

    def action_lexcom_set_password(self):
        self.ensure_one()
        if not self.lexcom_password:
            raise UserError(_("Enter a password before saving it."))
        self.company_id._lexcom_set_password(self.lexcom_password)
        # Clear the transient input so the plaintext does not linger in the
        # transient table until the next vacuum.
        self.lexcom_password = False
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Password Saved"),
                "message": _("The LexCom password hash has been stored."),
                "type": "success",
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_lexcom_clear_password(self):
        self.ensure_one()
        self.company_id._lexcom_set_password(False)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Password Cleared"),
                "message": _("The LexCom endpoint can no longer authenticate."),
                "type": "warning",
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }
