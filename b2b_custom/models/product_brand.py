import logging

from odoo import _, models

_logger = logging.getLogger(__name__)


class ProductBrand(models.Model):
    _inherit = 'product.brand'

    def _baf_notify_price_update_recipients(self):
        """Approved B2B partners who should be notified about a price update
        for the brands in `self` — one email per commercial partner, addressed
        to the commercial partner's own email.

        A brand marked `is_public=True` is available to every approved B2B
        customer, matching `visible_brands()` in the pricefile controller.
        For non-public brands, we invert `visible_brand_ids` — a partner is
        eligible when their own list or their parent company's list holds any
        of the brands in `self`. The commercial partner de-duplication keeps a
        company from getting one mail per contact."""
        if not self:
            return self.env['res.partner']

        Partner = self.env['res.partner']
        # Approved B2B (or an internal user impersonated via the B2B group)
        approved_domain = [
            '|',
            ('baf_b2b_state', '=', 'approved'),
            ('user_ids.group_ids', '=', self.env.ref(
                'b2b_custom.group_b2b_customer').id),
        ]
        approved = Partner.search(approved_domain)
        if not approved:
            return Partner

        public_ids = {b.id for b in self if b.is_public}
        gated_ids = {b.id for b in self if not b.is_public}
        matched = set()
        for partner in approved:
            visible = set(partner.visible_brand_ids.ids)
            commercial = partner.commercial_partner_id
            if commercial and commercial != partner:
                visible |= set(commercial.visible_brand_ids.ids)
            if public_ids or (visible & gated_ids):
                # De-dup on the commercial partner so a company with several
                # contacts still gets exactly one email.
                matched.add((commercial or partner).id)

        recipients = Partner.browse(sorted(matched)).filtered(lambda p: p.email)
        return recipients

    def _baf_notify_price_update(self):
        """Send the "new prices available" email to every approved B2B
        customer with access to a brand in `self`. Returns the recipient
        recordset. Missing template = warning + no-op (won't fail the caller,
        so it stays safe to wire into completion hooks)."""
        template = self.env.ref(
            'b2b_custom.mail_template_pricefile_price_update',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "Pricefile notification skipped: template missing.")
            return self.env['res.partner']
        recipients = self._baf_notify_price_update_recipients()
        for partner in recipients:
            try:
                template.with_context(
                    lang=partner.lang or self.env.user.lang,
                    brand_names=", ".join(sorted(b.display_name for b in self)),
                ).send_mail(
                    partner.id,
                    force_send=False,
                    email_values={'email_to': partner.email},
                )
            except Exception:  # pragma: no cover — never break the caller
                _logger.exception(
                    "Failed to queue pricefile notification for partner %s",
                    partner.id,
                )
        _logger.info(
            "Pricefile notification queued for %d recipient(s) across brands %s",
            len(recipients), self.mapped('name'),
        )
        return recipients

    def action_notify_price_update(self):
        """Open the multi-brand wizard preselected with `self`. Called from
        the Brand form header button."""
        return {
            'type': 'ir.actions.act_window',
            'name': _("Notify customers of new prices"),
            'res_model': 'pricefile.notify.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_brand_ids': [(6, 0, self.ids)]},
        }
