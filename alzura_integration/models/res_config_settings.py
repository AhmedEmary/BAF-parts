import base64
import logging
from datetime import datetime, timedelta

import requests
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # UI only — held in the auto-vacuumed transient table, never written to res.company
    alzura_id = fields.Char(string="Alzura ID")
    alzura_password = fields.Char(string="Alzura Password")

    # UI-only toggle to reveal the credential inputs while connected.
    alzura_show_credentials = fields.Boolean(default=False)

    alzura_country = fields.Char(
        related="company_id.alzura_country",
        string="Alzura Country",
        readonly=False,
    )

    # Read from company for display
    alzura_token_expiry = fields.Datetime(
        related="company_id.alzura_token_expiry",
        string="Token Expiry",
        readonly=True,
    )
    alzura_token_status = fields.Selection(
        selection=[
            ("none", "No Token"),
            ("valid", "Token Active"),
            ("expired", "Token Expired"),
        ],
        string="Token Status",
        compute="_compute_token_status",
    )

    @api.depends("company_id.alzura_token", "company_id.alzura_token_expiry")
    def _compute_token_status(self):
        now = fields.Datetime.now()
        for rec in self:
            if not rec.company_id.alzura_token:
                rec.alzura_token_status = "none"
            elif (
                rec.company_id.alzura_token_expiry
                and rec.company_id.alzura_token_expiry < now
            ):
                rec.alzura_token_status = "expired"
            else:
                rec.alzura_token_status = "valid"

    def action_get_alzura_token(self):
        if not self.alzura_id or not self.alzura_password:
            return self._notify(
                title="Missing Credentials",
                message="Please enter both Alzura ID and Password.",
                notif_type="warning",
            )

        raw = f"{self.alzura_id}:{self.alzura_password}"
        encoded = base64.b64encode(raw.encode()).decode()

        try:
            response = requests.get(
                "https://api-b2b.alzura.com/common/login",
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Accept": "application/vnd.saitowag.api+json;version=1.0",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )

            if response.status_code == 401:
                return self._notify(
                    title="Login Failed",
                    message="Invalid Alzura ID or Password. Please check your credentials.",
                    notif_type="danger",
                )

            response.raise_for_status()

            # Alzura returns the token nested in the JSON body:
            #   {"data": {"token": "...", "expire_date": "YYYY-MM-DD"}}
            try:
                body = response.json()
            except ValueError:
                body = {}
            data = body.get("data") if isinstance(body, dict) else None
            data = data if isinstance(data, dict) else {}

            token = data.get("token")
            if not token:
                _logger.warning(
                    "Alzura: login %s but no token found. headers=%s body=%s",
                    response.status_code,
                    list(response.headers.keys()),
                    response.text[:500],
                )
                return self._notify(
                    title="No Token Returned",
                    message="Login succeeded but no token was returned. Contact Alzura support.",
                    notif_type="warning",
                )

            # Prefer the expiry the API reports; fall back to 24h if absent/unparseable.
            expiry = fields.Datetime.now() + timedelta(hours=24)
            expire_date = data.get("expire_date")
            if expire_date:
                try:
                    expiry = datetime.strptime(expire_date, "%Y-%m-%d")
                except (ValueError, TypeError):
                    _logger.warning(
                        "Alzura: could not parse expire_date %r, using 24h default",
                        expire_date,
                    )

            # Save token + expiry to current company
            self.env.company.sudo().write(
                {
                    "alzura_token": token,
                    "alzura_token_expiry": expiry,
                }
            )

            _logger.info("Alzura: token saved for company %s", self.env.company.name)

            return self._notify(
                title="Token Saved",
                message="Alzura token fetched successfully. Valid until %s."
                % fields.Datetime.to_string(expiry),
                notif_type="success",
                reload=True,
            )

        except requests.exceptions.Timeout:
            return self._notify(
                title="Connection Timeout",
                message="Alzura API did not respond. Check your internet connection.",
                notif_type="danger",
            )
        except requests.exceptions.ConnectionError:
            return self._notify(
                title="Connection Error",
                message="Could not reach Alzura API. Check your network.",
                notif_type="danger",
            )
        except Exception as e:
            _logger.error("Alzura token fetch failed: %s", str(e))
            return self._notify(
                title="Unexpected Error",
                message=f"Something went wrong: {str(e)}",
                notif_type="danger",
            )

    def action_delete_alzura_token(self):
        if not self.env.company.sudo().alzura_token:
            return self._notify(
                title="No Token",
                message="There is no token to delete.",
                notif_type="warning",
            )

        self.env.company.sudo().write(
            {
                "alzura_token": False,
                "alzura_token_expiry": False,
            }
        )
        _logger.info("Alzura: token deleted for company %s", self.env.company.name)

        return self._notify(
            title="Token Deleted",
            message="Alzura token removed.",
            notif_type="success",
            reload=True,
        )

    def action_fetch_alzura_orders(self):
        company = self.env.company.sudo()
        if not company.alzura_token:
            return self._notify(
                title="No Token",
                message="Fetch an Alzura token before importing orders.",
                notif_type="warning",
            )
        try:
            result = self.env["sale.order"]._alzura_fetch_orders(company)
        except UserError as e:
            return self._notify(
                title="Fetch Failed",
                message=str(e),
                notif_type="danger",
            )
        except Exception as e:
            _logger.error("Alzura order fetch failed: %s", str(e))
            return self._notify(
                title="Fetch Failed",
                message="Could not fetch orders: %s" % str(e),
                notif_type="danger",
            )

        return self._notify(
            title="Orders Fetched",
            message=(
                "Imported %(created)s new order(s); %(skipped)s already present; "
                "%(rejected)s rejected (missing product)." % result
            ),
            notif_type="warning" if result.get("rejected") else "success",
            reload=True,
        )

    def _notify(self, title, message, notif_type="info", reload=False):
        action = {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": notif_type,
                "sticky": notif_type == "danger",
            },
        }
        if reload:
            # soft_reload re-reads the form (status badge / expiry) while keeping
            # the toast alive, unlike a full page reload.
            action["params"]["next"] = {
                "type": "ir.actions.client",
                "tag": "soft_reload",
            }
        return action
