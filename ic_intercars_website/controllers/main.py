"""Public aftermarket search: shop products first, then the IC cache.

The visitor flow the page implements:

1. the query is matched against already-materialised aftermarket
   products — those link straight to their product page;
2. remaining matches come from the 2M-row IC cache. Each carries a
   "check price & order" button which quotes IC live, materialises the
   product and lands the visitor on its (now published) product page.

Materialisation is the only write a visitor can trigger, so it is
rate-limited per session — enough for any human, useless for a bot
walking SKUs to inflate the catalog.
"""

import logging
import time

from urllib.parse import quote

from odoo import _
from odoo.exceptions import UserError
from odoo.http import Controller, request, route

_logger = logging.getLogger(__name__)

_MATERIALIZE_PER_HOUR = 30


class IcAftermarketWebsite(Controller):

    @route('/aftermarket/search', type='http', auth='public',
           website=True, sitemap=True, readonly=True)
    def aftermarket_search(self, q='', **kwargs):
        q = (q or '').strip()[:64]
        products = request.env['product.template']
        cache_rows = request.env['ic.product.info']
        if q:
            products = self._matching_shop_products(q)
            cache_rows = request.env['ic.product.info'].sudo(
            )._baf_website_search(q, limit=20)
            # A cache row whose product already shows above is noise.
            shown_skus = set(products.mapped('ic_sku'))
            cache_rows = cache_rows.filtered(
                lambda r: r.tow_kod not in shown_skus)
        return request.render('ic_intercars_website.search_page', {
            'q': q,
            'products': products,
            'cache_rows': cache_rows,
            'error': kwargs.get('error'),
        })

    @route('/aftermarket/get/<string:tow_kod>', type='http', auth='public',
           website=True, sitemap=False, methods=['POST'])
    def aftermarket_get(self, tow_kod, q='', **kwargs):
        error = self._throttle()
        if not error:
            row = request.env['ic.product.info'].sudo().search(
                [('tow_kod', '=', tow_kod)], limit=1)
            if not row:
                error = _("This part is no longer in the Inter Cars "
                          "catalog.")
        if not error:
            try:
                variant = row._baf_materialize_for_website()
            except UserError as exc:
                # Typically: IC declined to quote. Keep the operator
                # detail in the log, not in the customer's face.
                _logger.info("website materialisation refused for %s: %s",
                             tow_kod, exc)
                error = _(
                    "This part cannot be ordered right now — it may be "
                    "discontinued or temporarily unavailable."
                )
            else:
                return request.redirect(
                    variant.product_tmpl_id.website_url or '/shop')
        return request.redirect(
            '/aftermarket/search?q=%s&error=%s' % (quote(q), quote(error)))

    # ── helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _matching_shop_products(q):
        """Published aftermarket products matching an identifier or name."""
        domain = [
            ('part_quality', '=', 'aftermarket'),
            ('is_published', '=', True),
            ('sale_ok', '=', True),
            '|', '|', '|', '|',
            ('ic_sku', '=ilike', q),
            ('sku', '=ilike', q),
            ('default_code', '=ilike', q),
            ('barcode', '=', q),
            ('name', 'ilike', q),
        ]
        return request.env['product.template'].sudo().search(
            domain, limit=8)

    @staticmethod
    def _throttle():
        """Cap materialisations per visitor session; None when allowed."""
        now = time.time()
        stamps = [s for s in request.session.get('ic_materialize_log', [])
                  if now - s < 3600]
        if len(stamps) >= _MATERIALIZE_PER_HOUR:
            return _("Too many catalog requests from this session — "
                     "please try again later.")
        stamps.append(now)
        request.session['ic_materialize_log'] = stamps
        return None
