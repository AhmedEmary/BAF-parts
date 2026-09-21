import { Interaction } from '@web/public/interaction';
import { registry } from '@web/core/registry';

/**
 * Drop the stale pager left behind after an AJAX cart update.
 *
 * On a quantity change, core's `updateCartNavBar` inserts the freshly rendered
 * cart-lines fragment (which now carries its own pager) before the old
 * `.js_cart_lines` and then removes only the old `.js_cart_lines` — the
 * previous pager, a sibling of the cart-lines div, is left in place, so two
 * pagers end up in the DOM. Core restarts public interactions on the cart after
 * the swap, so this runs at exactly the right moment: keep the pager next to the
 * current cart lines and remove any others.
 */
export class BafCartPager extends Interaction {
    static selector = '#cart_products';

    setup() {
        let sibling = this.el.nextElementSibling;
        let keptPager = false;
        const stale = [];
        while (sibling) {
            if (sibling.querySelector && sibling.querySelector('.pagination')) {
                if (keptPager) {
                    stale.push(sibling);
                } else {
                    keptPager = true;
                }
            }
            sibling = sibling.nextElementSibling;
        }
        stale.forEach((el) => el.remove());
    }
}

registry
    .category('public.interactions')
    .add('b2b_custom.baf_cart_pager', BafCartPager);
