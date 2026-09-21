import logging

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)

# Bodies are stored for debugging, not archiving. A pathological payload should
# not turn the log table into the biggest table in the database.
_MAX_STORED_BODY = 64000


class LexcomRequestLog(models.Model):
    """Audit trail for every inbound LexCom request.

    Rows are written on their OWN cursor (see ``_lexcom_log``). The request
    cursor is not usable for this: Odoo wraps the handler in
    ``service_model.retrying`` (odoo/http.py), so a serialization failure or a
    rolled-back business error would take the log row with it - losing exactly
    the events the log exists to explain.
    """

    _name = "lexcom.request.log"
    _description = "LexCom Request Log"
    _order = "id desc"
    _rec_name = "command"

    command = fields.Char(index=True, readonly=True)
    dealer_id = fields.Char(index=True, readonly=True)
    remote_addr = fields.Char(readonly=True)
    outcome = fields.Selection(
        selection=[
            ("ok", "Accepted"),
            ("business_error", "Rejected"),
            ("auth_failed", "Authentication Failed"),
            ("internal_error", "Internal Error"),
        ],
        index=True,
        readonly=True,
        help="Rejected is a normal business outcome answered with HTTP 200. "
             "Internal Error is a bug on our side, answered with 5xx.",
    )
    http_status = fields.Integer(readonly=True)
    duration_ms = fields.Integer(string="Duration (ms)", readonly=True)
    message = fields.Char(
        readonly=True,
        help="The feedback message returned to LexCom, if any.",
    )
    request_body = fields.Text(readonly=True)
    response_body = fields.Text(readonly=True)
    sale_order_id = fields.Many2one(
        "sale.order", string="Resulting Order", readonly=True, ondelete="set null"
    )
    # The spec allows up to five arbitrary custom headers on any command. No
    # command consumes them yet, but they are LexCom's documented escape hatch,
    # so capture them rather than discard them.
    header_01 = fields.Char(string="X_LC_HEADER_01", readonly=True)
    header_02 = fields.Char(string="X_LC_HEADER_02", readonly=True)
    header_03 = fields.Char(string="X_LC_HEADER_03", readonly=True)
    header_04 = fields.Char(string="X_LC_HEADER_04", readonly=True)
    header_05 = fields.Char(string="X_LC_HEADER_05", readonly=True)

    @api.model
    def _lexcom_truncate(self, body):
        if not body:
            return False
        if len(body) <= _MAX_STORED_BODY:
            return body
        return body[:_MAX_STORED_BODY] + "\n... [truncated]"

    @api.model
    def _lexcom_log(self, vals):
        """Write one row on an independent cursor that commits by itself.

        Never raises: a logging failure must not turn a served request into a
        failed one. The credential is never part of ``vals`` - the controller
        does not pass the Authorization header.
        """
        vals = dict(vals)
        vals["request_body"] = self._lexcom_truncate(vals.get("request_body"))
        vals["response_body"] = self._lexcom_truncate(vals.get("response_body"))
        try:
            with self.env.registry.cursor() as cr:
                # Cursor.__exit__ commits when no exception escapes the block
                # (odoo/sql_db.py), which is exactly the durability we need.
                env = api.Environment(cr, SUPERUSER_ID, {})
                env["lexcom.request.log"].create(vals)
        except Exception:
            _logger.exception(
                "LexCom: could not write the request log row for command %s",
                vals.get("command"),
            )
