"""IC drop-ship hook on purchase.order.

When a PO is confirmed and its vendor is the configured IC vendor, we
submit + confirm a requisition against Inter Cars in the same step. The
customer's SO -> drop-ship PO chain still owns delivery; IC just
fulfils the drop-ship leg.
"""

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    # IC bookkeeping — visible on the PO form so operations can
    # investigate. Populated on button_confirm for IC vendor POs.
    ic_requisition_id = fields.Char(
        string="IC Requisition ID", copy=False, readonly=True,
        help="Value of ``requisitionId`` returned by IC when the "
             "requisition was submitted.",
    )
    ic_requisition_uuid = fields.Char(
        string="IC Requisition UUID", copy=False, readonly=True,
        help="Internal ``id`` returned by IC — used to confirm/cancel "
             "the requisition.",
    )
    ic_requisition_status = fields.Char(
        string="IC Status", copy=False, readonly=True,
    )
    is_ic_dropship = fields.Boolean(
        string="Inter Cars Drop-ship",
        compute='_compute_is_ic_dropship', store=True,
    )

    @api.depends('partner_id')
    def _compute_is_ic_dropship(self):
        backends = self.env['ic.backend'].sudo().search([('active', '=', True)])
        vendor_ids = set(backends.mapped('vendor_id').ids)
        for order in self:
            order.is_ic_dropship = bool(
                order.partner_id and order.partner_id.id in vendor_ids
            )

    # ── Confirm hook ────────────────────────────────────────────────────
    def button_confirm(self):
        # Confirm the PO normally first. If IC submission then fails, the
        # user sees the error before any customer-visible state moves —
        # they can cancel or retry from the PO form.
        res = super().button_confirm()
        for order in self.filtered(lambda o: o.is_ic_dropship
                                             and not o.ic_requisition_uuid):
            order._ic_submit_and_confirm_requisition()
        return res

    # ── Requisition helpers ─────────────────────────────────────────────
    def _ic_backend(self):
        """Return the ic.backend whose vendor_id matches this PO's vendor."""
        self.ensure_one()
        return self.env['ic.backend'].sudo().search([
            ('active', '=', True),
            ('vendor_id', '=', self.partner_id.id),
        ], limit=1)

    def _ic_live_price_map(self, backend, client):
        """Live IC quote for every SKU on this PO → {sku: (net, gross)}.

        IC validates the submitted ``unitPriceNet`` against its current
        price ("placing an order with current prices"); sending a stale
        or wrong figure fails the requisition. So the submit flow asks
        IC first and sends IC's own numbers back. Soft-fails to an
        empty map — the builder then falls back to the PO line price.
        """
        self.ensure_one()
        skus = list({pol.product_id.ic_sku for pol in self.order_line
                     if pol.product_id.ic_sku})
        if not skus:
            return {}
        try:
            quote = client.get_price(
                lines=[{'sku': s, 'quantity': 1} for s in skus],
                ship_to=backend.ship_to or None,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "IC live price fetch failed for %s — falling back to PO "
                "line prices: %s", self.name, exc)
            return {}
        price_map = {}
        for line in (quote.get('lines') or []):
            price = line.get('price') or {}
            if line.get('sku') and price.get('customerPriceNet') is not None:
                price_map[line['sku']] = (
                    float(price['customerPriceNet']),
                    float(price.get('customerPriceGross') or 0.0),
                )
        return price_map

    def _ic_build_requisition_lines(self, price_map=None):
        """Translate PO lines into IC requisition line dicts.

        IC's schema uses ``requiredQuantity``, ``unitPriceNet`` and
        ``unitPriceGross`` (not ``quantity`` / ``price_unit``).

        When a live price is available for the SKU it wins over the PO
        line price — and the PO line is synced to it so the purchase
        document reflects what IC will actually invoice. Without a live
        price, gross is derived from the Odoo tax when one is set,
        otherwise from the backend's ``vat_rate_pct``.
        """
        self.ensure_one()
        price_map = price_map or {}
        lines = []
        for pol in self.order_line:
            sku = pol.product_id.ic_sku
            if not sku:
                raise UserError(_(
                    "Purchase line '%s' has no IC SKU. Only IC-materialised "
                    "products can be sent to Inter Cars."
                ) % (pol.product_id.display_name or ''))
            if sku in price_map:
                net, gross = price_map[sku]
                if not gross:
                    gross = self._ic_gross_from_net(net, pol)
                if pol.price_unit != net:
                    _logger.info(
                        "PO %s line %s: price synced to IC live quote "
                        "%.4f (was %.4f)", self.name, sku, net,
                        pol.price_unit)
                    pol.price_unit = net
            else:
                net = float(pol.price_unit or 0.0)
                gross = self._ic_gross_from_net(net, pol)
            lines.append({
                'sku': sku,
                'requiredQuantity': float(pol.product_qty or 0.0),
                'unitPriceNet': round(net, 4),
                'unitPriceGross': round(gross, 4),
            })
        if not lines:
            raise UserError(_(
                "Purchase order %s has no lines to send to Inter Cars."
            ) % self.name)
        return lines

    def _ic_gross_from_net(self, net, pol):
        """Compute a gross price from an Odoo tax when possible."""
        self.ensure_one()
        backend = self._ic_backend()
        tax = pol.tax_ids[:1]
        if tax and tax.amount_type == 'percent':
            return net * (1.0 + (tax.amount or 0.0) / 100.0)
        rate = backend.vat_rate_pct if backend else 19.0
        return net * (1.0 + (rate or 0.0) / 100.0)

    @staticmethod
    def _ic_first_item(response):
        """IC's ordering endpoints answer an array — take entry one."""
        if isinstance(response, list):
            return response[0] if response else {}
        return response if isinstance(response, dict) else {}

    def _ic_check_availability(self, backend, client):
        """Block the requisition when IC reports zero availability.

        Soft-fails on transport errors — availability is an advisory
        pre-flight; the authoritative rejection is IC's own ICF230,
        which the submit wrapper translates.
        """
        self.ensure_one()
        skus = [pol.product_id.ic_sku for pol in self.order_line
                if pol.product_id.ic_sku]
        if not skus:
            return
        try:
            rows = client.get_stock(
                skus=skus, ship_to=backend.ship_to or None,
            ) or []
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "IC availability pre-flight failed for %s — proceeding "
                "and letting IC decide: %s", self.name, exc)
            return
        availability = {}
        for row in rows:
            sku = row.get('sku')
            if sku:
                availability[sku] = availability.get(sku, 0) + int(
                    row.get('availability') or 0)
        unavailable = [s for s in skus if availability.get(s, 0) <= 0]
        if unavailable:
            raise UserError(_(
                "Inter Cars reports no availability for: %(skus)s. "
                "The requisition would be rejected (ICF230). Remove "
                "those lines or wait until IC restocks — the shop page "
                "shows live availability per alternative."
            ) % {'skus': ', '.join(unavailable)})

    def _ic_submit_and_confirm_requisition(self):
        """Two-phase: submit → verify ACCEPTED → confirm.

        Blocks if IC's finances endpoint reports orderingAllowed=false.
        """
        self.ensure_one()
        backend = self._ic_backend()
        if not backend:
            raise UserError(_(
                "No active Inter Cars backend maps to vendor '%s'. "
                "Configure one in Purchases → Configuration → Inter Cars."
            ) % self.partner_id.display_name)

        client = backend.get_client()

        # Optional pre-flight: refuse to submit when IC has blocked us.
        try:
            fin = client.get_finances() or {}
        except UserError as exc:
            _logger.warning("IC finances check failed for PO %s: %s",
                            self.name, exc)
            fin = {}
        if fin and fin.get('orderingAllowed') is False:
            raise UserError(_(
                "Inter Cars reports ordering is not allowed for this "
                "account (overdue balance %s %s). Clear the overdue "
                "invoices in the IC portal before confirming this PO."
            ) % (fin.get('overdueBalance'), fin.get('currencyCode') or ''))

        # Availability pre-flight: IC answers ICF230 ("All SKUs provided
        # in the request are invalid") for items with no stock / not
        # orderable via the API channel. Checking first gives the
        # operator an actionable message naming the SKUs.
        self._ic_check_availability(backend, client)

        price_map = self._ic_live_price_map(backend, client)
        lines = self._ic_build_requisition_lines(price_map)
        try:
            response = client.submit_requisition(
                lines=lines,
                ship_to=backend.ship_to or None,
                delivery_method=backend.delivery_method or None,
                payment_method=backend.payment_method or None,
                # deferredPayment is Polish-only; opt-out for other markets.
                deferred_payment=(
                    True if backend.market == 'pl' else None
                ),
                custom_number=self.name,
            )
        except UserError as exc:
            # Translate IC's opaque error codes into operator language.
            msg = str(exc)
            # Full payload to the server log — chatter would roll back
            # with the raise, the log survives for IC support tickets.
            _logger.error(
                "IC requisition failed for %s.\n  payload: %s\n  error: %s",
                self.name,
                json.dumps({'lines': lines,
                            'shipTo': backend.ship_to or None,
                            'deliveryMethod': backend.delivery_method or None,
                            'paymentMethod': backend.payment_method or None,
                            'customNumber': self.name},
                           ensure_ascii=False),
                msg,
            )
            if 'ICF299' in msg:
                raise UserError(_(
                    "Inter Cars reported an internal error (ICF299 — "
                    "'unknown error'). The exact request payload was "
                    "written to the server log for an IC support ticket "
                    "(quote the errorId below).\n\n"
                    "Things worth trying before contacting IC:\n"
                    "• clear 'Default Payment Method' on the IC backend "
                    "(profile codes are not always valid for ordering);\n"
                    "• clear 'Default shipTo' and retry;\n"
                    "• retry in a few minutes — ICF299 is sometimes "
                    "transient on IC's side.\n\n"
                    "Original error: %(err)s"
                ) % {'err': msg})
            if 'ICF230' in msg:
                raise UserError(_(
                    "Inter Cars rejected every SKU on this purchase order "
                    "(ICF230 — 'All SKUs provided in the request are "
                    "invalid'). This usually means the items have no "
                    "availability right now or are not orderable through "
                    "the API channel. SKUs sent: %(skus)s.\n\n"
                    "Original error: %(err)s"
                ) % {'skus': ', '.join(l['sku'] for l in lines),
                     'err': msg})
            if 'ICF209' in msg:
                raise UserError(_(
                    "Inter Cars rejected the delivery method "
                    "(ICF209). Clear the 'Default Delivery Method' field "
                    "on the IC backend to use your account default, or "
                    "ask IC which codes are valid for ordering.\n\n"
                    "Original error: %(err)s"
                ) % {'err': msg})
            raise
        item = self._ic_first_item(response)
        phase = (item.get('phaseCode') or '').upper()
        if phase != 'ACCEPTED':
            raise UserError(_(
                "Inter Cars did not ACCEPT the requisition (phase=%s). "
                "Response: %s"
            ) % (phase or '?', item))

        self.sudo().write({
            'ic_requisition_id': item.get('requisitionId') or '',
            'ic_requisition_uuid': item.get('id') or '',
            'ic_requisition_status': item.get('statusCode') or phase,
        })
        self.message_post(body=_(
            "Inter Cars requisition submitted — requisitionId=%s, id=%s, "
            "phase=%s. Confirming next."
        ) % (
            item.get('requisitionId') or '?',
            item.get('id') or '?', phase,
        ))

        # Phase 2: confirm. The confirm response mirrors the submit shape.
        confirm_res = client.confirm_requisition(
            item.get('id'), ship_to=backend.ship_to or None,
        )
        c_item = self._ic_first_item(confirm_res)
        self.sudo().write({
            'ic_requisition_status': (
                c_item.get('statusCode') or c_item.get('phaseCode') or 'CONFIRMED'
            ),
        })
        self.message_post(body=_(
            "Inter Cars requisition confirmed — status=%s."
        ) % (c_item.get('statusCode') or c_item.get('phaseCode') or '?'))
