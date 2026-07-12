import { Interaction } from '@web/public/interaction';
import { registry } from '@web/core/registry';

export class BafAltVendorCart extends Interaction {
    static selector = '.baf-alt-vendors';
    dynamicContent = {
        'button.baf-alt-add': { 't-on-click': this.addAltVendorProduct },
    };

    /**
     * Add the product sourced from a chosen alternative direct vendor. Goes
     * through the standard cart service so it raises the same cart
     * notification as a normal add-to-cart.
     *
     * @param {Event} ev
     */
    addAltVendorProduct(ev) {
        const dataset = ev.currentTarget.dataset;
        const qtyInput = this.el.querySelector(`#baf_qty_${dataset.vendorId}`);
        const quantity = parseInt(qtyInput?.value) || 1;
        this.services['cart'].add({
            productTemplateId: parseInt(dataset.productTemplateId),
            productId: parseInt(dataset.productId),
            quantity: quantity,
            baf_alt_vendor_id: parseInt(dataset.vendorId),
        });
    }
}

registry
    .category('public.interactions')
    .add('b2b_custom.baf_alt_vendor_cart', BafAltVendorCart);
