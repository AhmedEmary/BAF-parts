"""Cart controller for Inter Cars aftermarket alternatives.

The OEM product page renders IC alternatives via QWeb (see
``baf_oe_crossref_product_page`` in ``website_sale_templates.xml``);
this controller handles the ``Add to cart`` click on those cards.

Flow: the client posts ``template_id`` + ``ic_sku`` (from the cache
payload) + quantity. We resolve the IC catalog entry from the cache,
lazy-create (or find) the corresponding ``product.product``, then hand
off to the standard ``_cart_add()`` machinery. The customer never sees
IC's own price/cost — that's baked into the ``supplierinfo`` written
during lazy-create.
"""

import logging

from odoo import _
from odoo.exceptions import UserError
from odoo.http import Controller, request, route

_logger = logging.getLogger(__name__)


class BafAftermarketCart(Controller):

    @route(
        '/shop/cart/add_aftermarket',
        type='jsonrpc', auth='public', methods=['POST'],
        website=True, sitemap=False,
    )
    def add_aftermarket_to_cart(self, template_id, ic_sku, quantity=1,
                                **kwargs):
        template_id = int(template_id or 0)
        quantity = int(quantity or 1)
        if not template_id or not ic_sku:
            raise UserError(_("Missing template_id or ic_sku."))

        tmpl = request.env['product.template'].sudo().browse(template_id).exists()
        if not tmpl:
            raise UserError(_("Unknown product."))

        backend = request.env['ic.backend'].sudo()._get_default()
        if not backend:
            raise UserError(_(
                "Inter Cars is not configured — cannot add an aftermarket "
                "item to the cart."
            ))

        cards = tmpl.sudo().baf_ic_equivalents()
        card = next((c for c in cards if c.get('ic_sku') == ic_sku), None)
        if not card:
            # The cache may have expired between page-load and click.
            # Blow the cache and try one more time.
            key = tmpl.sudo()._baf_ic_cache_key(backend.default_language or 'de')
            entry = request.env['ic.article.cache'].sudo().search(
                [('key', '=', key)], limit=1,
            )
            if entry:
                entry.unlink()
            cards = tmpl.sudo().baf_ic_equivalents()
            card = next((c for c in cards if c.get('ic_sku') == ic_sku), None)
        if not card:
            raise UserError(_(
                "That aftermarket alternative is no longer available. "
                "Please refresh the page."
            ))

        # Live IC price pre-flight — raises a friendly UserError when
        # IC won't quote the SKU (discontinued, credit block, …), so a
        # zero-cost line can never reach the cart. Logic lives on
        # product.product so it is unit-testable.
        cost_net = request.env['product.product'].sudo()._baf_ic_live_cost(
            backend, ic_sku, quantity=quantity,
        )
        # Availability pre-flight — an item IC cannot deliver would
        # produce a drop-ship PO that IC rejects with ICF230. Better to
        # stop the customer here with a clear message. None = check
        # unavailable → let it pass, IC gets the final say at PO time.
        availability = request.env['product.product'].sudo() \
            ._baf_ic_live_availability(backend, ic_sku)
        if availability is not None and availability <= 0:
            raise UserError(_(
                "This alternative is currently out of stock at "
                "Inter Cars and cannot be ordered right now. Please "
                "pick another alternative or check back later."
            ))

        ic_data = {
            'sku': ic_sku,
            'index': card.get('ic_index'),
            'brand': card.get('brand'),
            'shortDescription': card.get('short_description'),
            'articleNumber': card.get('article_number'),
            'genericArticleReferences': [{
                'primary': True,
                'genericArticleId': card.get('ic_generic_article_id') or None,
            }] if card.get('ic_generic_article_id') else [],
        }
        product = request.env['product.product'].sudo()._baf_find_or_create_ic(
            backend, ic_data, price_net=cost_net,
        )
        # Persist the OEM ↔ aftermarket relation: fills the existing
        # link's aftermarket_template_id or creates a 'shop' link if a
        # customer bought an alternative that was only auto-discovered.
        request.env['baf.oe.link'].sudo()._record_materialisation(
            tmpl, ic_sku, product,
        )

        # Delegate to standard cart machinery.
        order_sudo = request.cart or request.website._create_cart()
        result = order_sudo.with_context(
            skip_cart_verification=True,
        )._cart_add(
            product_id=product.id, quantity=quantity,
        )
        return {
            'line_id': result.get('line_id'),
            'added_qty': result.get('added_qty'),
            'product_id': product.id,
            'product_template_id': product.product_tmpl_id.id,
            'is_aftermarket': True,
        }
