/** @odoo-module **/

/**
 * Wires the "Add to cart" buttons rendered by the aftermarket-equivalents
 * block on OEM product pages.
 *
 *   .baf_ic_add_aftermarket_btn — posts to /shop/cart/add_aftermarket
 *     with { template_id, ic_sku, quantity }.  The server lazy-creates
 *     the product.product for that IC SKU, wires up the drop-ship
 *     route, and delegates to Odoo's standard _cart_add() so the line
 *     lands in the same sale.order as everything else.
 *
 *   .baf_ic_add_oem_btn — shortcut for the OEM card. The customer is
 *     already on the OEM's own product page, so instead of re-implementing
 *     the whole cart-add contract we submit the main product form.
 *
 * On success we send the customer straight to /shop/cart so they see the
 * line landed. A silent notification would be nicer, but a redirect is
 * bulletproof across every website skin BAF might apply.
 */

import publicWidget from "@web/legacy/js/public/public_widget";
import { rpc } from "@web/core/network/rpc";

publicWidget.registry.BafAftermarketAddToCart = publicWidget.Widget.extend({
    selector: ".baf_ic_add_aftermarket_btn",
    events: { "click": "_onClick" },

    async _onClick(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        const btn = ev.currentTarget;
        const template_id = parseInt(btn.dataset.templateId, 10);
        const ic_sku = btn.dataset.icSku;
        if (!template_id || !ic_sku) {
            return;
        }
        // Prevent double-submits and give the user a spinner while
        // the round-trip to IC's pricing endpoint completes.
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i>';
        try {
            await rpc("/shop/cart/add_aftermarket", {
                template_id,
                ic_sku,
                quantity: 1,
            });
            window.location.href = "/shop/cart";
        } catch (err) {
            const msg = (err && (err.data && err.data.message)) || err.message
                || "Add to cart failed";
            alert(msg);
            btn.disabled = false;
            btn.innerHTML = original;
        }
    },
});

publicWidget.registry.BafOemCardAddToCart = publicWidget.Widget.extend({
    selector: ".baf_ic_add_oem_btn",
    events: { "click": "_onClick" },

    _onClick(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        // The OEM product page already contains the standard cart-add
        // form (with all the correct hidden inputs — csrf token, uom,
        // variant, custom attribute values …). We just trigger it.
        const form = document.querySelector("#product_details form")
                  || document.querySelector(".js_product form")
                  || document.querySelector("form.js_add_cart_json");
        if (form) {
            const qty = form.querySelector('input[name="add_qty"]');
            if (qty) {
                qty.value = 1;
            }
            form.submit();
            return;
        }
        // Fallback: the OEM product's own /shop/product/<slug> page will
        // always let them add it manually, so as a last resort just
        // scroll to the main product form.
        const el = document.getElementById("product_details");
        if (el) {
            el.scrollIntoView({ behavior: "smooth" });
        }
    },
});
