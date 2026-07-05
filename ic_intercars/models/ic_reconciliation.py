"""IC reconciliation crons.

IC's search endpoints for deliveries and invoices both enforce a **max
2-day window** on the from/to date pair. To reconcile a longer stretch
we walk day-by-day, calling IC once per day inside the target range.

The crons only *fetch* data and post it to the related PO's chatter — a
production-ready billing bridge would import invoices onto ``account.move``
and deliveries into ``stock.picking``, but the customer's ordering-side
requirements are what this module owns first. The plumbing is here so
that follow-up work is small.
"""

import logging
from datetime import date, timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

_DAYS_LOOKBACK_DEFAULT = 3


class IcBackend(models.Model):
    _inherit = 'ic.backend'

    def _ic_iter_day_windows(self, days_back=_DAYS_LOOKBACK_DEFAULT):
        """Yield (from_str, to_str) pairs — one per day in the lookback.

        IC caps a search at a 2-day window, so we play safe with a 1-day
        window per iteration (from = to). Dates are ISO strings.
        """
        today = fields.Date.context_today(self)
        for delta in range(days_back, -1, -1):
            d = today - timedelta(days=delta)
            iso = d.isoformat()
            yield iso, iso

    # ── DELIVERIES ──────────────────────────────────────────────────────
    @api.model
    def _cron_reconcile_deliveries(self, days_back=_DAYS_LOOKBACK_DEFAULT):
        for backend in self.sudo().search([('active', '=', True)]):
            try:
                backend._reconcile_deliveries(days_back=days_back)
            except Exception as exc:  # noqa: BLE001 — cron must never break
                _logger.exception(
                    "IC delivery reconciliation failed for backend %s: %s",
                    backend.name, exc,
                )

    def _reconcile_deliveries(self, days_back=_DAYS_LOOKBACK_DEFAULT):
        self.ensure_one()
        client = self.get_client()
        for d_from, d_to in self._ic_iter_day_windows(days_back):
            offset = 0
            while True:
                page = client.search_deliveries(
                    creation_from=d_from, creation_to=d_to,
                    offset=offset, limit=50,
                )
                items = page if isinstance(page, list) else \
                    (page.get('items') or page.get('deliveries') or [])
                if not items:
                    break
                for delivery in items:
                    self._apply_delivery(delivery)
                if len(items) < 50:
                    break
                offset += 50

    def _apply_delivery(self, delivery):
        """Attach the delivery summary to the matching PO's chatter.

        Matching is done by IC ``orderId`` back to the PO's stored
        ``ic_requisition_id``.
        """
        order_id = delivery.get('orderId') or delivery.get('id')
        if not order_id:
            return
        PO = self.env['purchase.order'].sudo()
        po = PO.search([
            '|',
            ('ic_requisition_id', '=', str(order_id)),
            ('ic_requisition_uuid', '=', str(order_id)),
        ], limit=1)
        if not po:
            return
        po.message_post(body=_(
            "Inter Cars delivery received — id=%(id)s, method=%(m)s, "
            "created=%(c)s, lines=%(n)d."
        ) % {
            'id': delivery.get('id'),
            'm': delivery.get('deliveryMethod') or '?',
            'c': delivery.get('creationDate') or '?',
            'n': len(delivery.get('lines') or []),
        })

    # ── INVOICES ────────────────────────────────────────────────────────
    @api.model
    def _cron_reconcile_invoices(self, days_back=_DAYS_LOOKBACK_DEFAULT):
        for backend in self.sudo().search([('active', '=', True)]):
            try:
                backend._reconcile_invoices(days_back=days_back)
            except Exception as exc:  # noqa: BLE001
                _logger.exception(
                    "IC invoice reconciliation failed for backend %s: %s",
                    backend.name, exc,
                )

    def _reconcile_invoices(self, days_back=_DAYS_LOOKBACK_DEFAULT):
        self.ensure_one()
        client = self.get_client()
        for d_from, d_to in self._ic_iter_day_windows(days_back):
            offset = 0
            while True:
                page = client.search_invoices(
                    issue_from=d_from, issue_to=d_to,
                    ship_to=self.ship_to or None,
                    offset=offset, limit=200,
                )
                items = page if isinstance(page, list) else \
                    (page.get('items') or page.get('invoices') or [])
                if not items:
                    break
                for inv in items:
                    self._apply_invoice(inv)
                if len(items) < 200:
                    break
                offset += 200

    def _apply_invoice(self, invoice):
        """Log the invoice on the matching PO. Real posting to account.move
        is left as a follow-up — the required contract details (KSeF
        number, related invoice links, per-line delivery ids) are
        already available in the payload for that next step."""
        invoice_id = invoice.get('id') or invoice.get('techId')
        if not invoice_id:
            return
        _logger.info(
            "IC invoice fetched — id=%s, classification=%s, totalNet=%s.",
            invoice_id,
            invoice.get('invoiceClassification'),
            invoice.get('totalNet'),
        )
